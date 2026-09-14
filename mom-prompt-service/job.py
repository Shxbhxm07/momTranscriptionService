"""One job → one acknowledgement: the user's prompt and documents in, JSSD minutes out.

The same steps, in the same order, as the audio service's consumer.process (../api/consumer.py), with
the recording replaced by text:

    file_urls ─▶ MinIO ─▶ text (text layer; OCR for scanned pages) ─┐
                                              prompt ───────────────┴─▶ llama-service /summarize
    minutes ─▶ JSSD .docx ─▶ MinIO {tenant}/summaries/ ─▶ Elasticsearch (record + chunks) ─▶ ack

Both ways in use it: the Kafka consumer (consumer.py) and POST /v1/mom-prompt (main.py).
"""
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import MAX_DOC_MB, MAX_SOURCE_CHARS, MIN_SOURCE_CHARS
from core.kafka_contract import KafkaJob, build_ack, summary_object_key
from core.mom import MomGenerator, to_mom_response
from core.search_index import ChunkIndex, MomIndex
from core.storage import ObjectStore
from utils.docx_export import build_mom_docx
from utils.documents import SUPPORTED_EXTENSIONS, _sniff, extract_text_blocks

logger = logging.getLogger("job")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass
class Clients:
    store: ObjectStore
    index: MomIndex
    chunks: ChunkIndex
    llm: MomGenerator


_clients: Optional[Clients] = None
_clients_lock = threading.Lock()


def clients() -> Clients:
    """MinIO, Elasticsearch and llama-service clients, made once, on first use.

    Not at startup: an Elasticsearch that is down must not stop the service starting, and the first job
    to need it reports the problem in its acknowledgement. Raises if the indices cannot be ensured.
    """
    global _clients
    with _clients_lock:
        if _clients is None:
            index = MomIndex()
            index.ensure_indices()
            _clients = Clients(ObjectStore(), index, ChunkIndex(), MomGenerator())
        return _clients


def compose_source(prompt: str, documents: List[Tuple[str, str]]) -> str:
    """The text llama-service writes the minutes from.

    /summarize was built for transcripts, so each part is labelled: the prompt is the user's request
    (what the meeting was, what to cover), each document is source material. Unlabelled, the model can
    take "focus on the budget" for something a participant said.
    """
    parts = []
    if prompt:
        parts.append(f"USER'S REQUEST FOR THESE MINUTES:\n{prompt}")
    for i, (name, text) in enumerate(documents, start=1):
        parts.append(f"SOURCE DOCUMENT {i} ({name}):\n{text}")
    return "\n\n".join(parts)


def _metadata(mom_meta: Dict) -> Dict[str, str]:
    """The job's stated date, time and venue, in the names /summarize takes. Empty ones are left out."""
    pairs = (("date", "meeting_date"), ("time", "meeting_time"), ("venue", "venue"))
    return {ours: str(mom_meta[theirs]).strip() for ours, theirs in pairs
            if str(mom_meta.get(theirs) or "").strip()}


def _read_documents(job: KafkaJob, store: ObjectStore) -> List[Tuple[str, str]]:
    documents = []
    for i, path in enumerate(job.file_urls):
        raw = store.download(path)
        name = (job.document_names[i] if i < len(job.document_names) else "") or path.rsplit("/", 1)[-1]
        size_mb = len(raw) / 1048576
        if size_mb > MAX_DOC_MB:
            raise ValueError(f"{name} is {size_mb:.0f} MB; the limit is {MAX_DOC_MB} MB.")
        # The extractor reads any unrecognised file as plain text (right for a .txt with an odd name).
        # Here that would turn an image or a spreadsheet into gibberish minutes, so refuse it unless
        # the name or the file's own magic bytes say PDF, DOCX, DOC or TXT.
        if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS and not _sniff(raw):
            raise ValueError(f"{name}: unsupported file type. Send PDF, DOCX, DOC or TXT.")
        extraction = extract_text_blocks(raw, name)
        text = "\n\n".join(b for b in extraction.blocks if b.strip())
        if not text.strip():
            logger.warning(f"[JOB {job.conversation_id}] {name}: no text found, even with OCR")
        logger.info(f"[JOB {job.conversation_id}] read {name} ({size_mb:.1f} MB, {len(text)} chars)")
        documents.append((name, text))
    return documents


def process(job: KafkaJob, c: Clients) -> dict:
    """One job → an acknowledgement. Never raises: a crash here would lose the ack."""
    t0 = time.time()
    try:
        if not job.prompt and not job.file_urls:
            raise ValueError("Nothing to write minutes from: the job has no prompt and no file_urls.")
        documents = _read_documents(job, c.store)
        chars = len(job.prompt) + sum(len(t) for _, t in documents)
        if chars < MIN_SOURCE_CHARS:
            raise ValueError(f"Too little to write minutes from: {chars} characters of prompt and document "
                             "text. Describe the meeting in the prompt, or attach its notes.")
        source = compose_source(job.prompt, documents)
        if len(source) > MAX_SOURCE_CHARS:
            raise ValueError(f"The documents are too long: {len(source)} characters, the limit is "
                             f"{MAX_SOURCE_CHARS}.")

        mom = to_mom_response(c.llm.generate(source, _metadata(job.mom_meta)))
        if not any(mom.get(k) for k in ("summary", "key_points", "decisions", "action_items")):
            raise RuntimeError("no minutes produced")

        docx_bytes = build_mom_docx(mom, job.mom_meta)
        bucket, key = c.store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest()),
                                     docx_bytes, DOCX_MIME)
        c.index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key)
        # Last, and it never raises: the minutes are stored by now, and a failed search copy must not
        # turn a finished job into a failed one. Does nothing until ENABLE_CHUNK_INDEX and CHUNK_INDEX.
        c.chunks.index_mom(job, mom, summary_bucket=bucket, summary_object_key=key)
        logger.info(f"[JOB {job.conversation_id}] done in {time.time()-t0:.0f}s — {bucket}/{key}")
        return build_ack(job, success=True, bucket=bucket, object_key=key,
                         description=(mom.get("summary") or "")[:300])
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        return build_ack(job, success=False, description=f"{type(e).__name__}: {e}")
