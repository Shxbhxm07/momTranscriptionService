"""Write the finished MoM into Elasticsearch.

TWO INDICES, SPLIT BY HOW THE JOB ARRIVED — attachments (file_urls) go to one, already-ingested
files (document_ids / file_fids) to the other. That split comes from the backend contract, not from
anything this code needs; indexing by delivery route rather than by content is unusual and worth
revisiting with them, so it lives in config rather than being assumed here.

THE MAPPING BELOW IS OUR BEST GUESS and is meant to be replaced. Their index may already exist with
its own mapping, in which case ensure_indices() should not run at all — set ELASTIC_CREATE_INDICES
to false. It is written out explicitly rather than relying on dynamic mapping because dynamic
mapping would make every string a text+keyword pair and silently turn `decisions` into a full-text
field nobody can aggregate on.
"""
import logging
from typing import Any, Dict, List, Optional

from elasticsearch import Elasticsearch

from config import (CHUNK_INDEX, CHUNK_OVERLAP_WORDS, CHUNK_WORDS, ELASTIC_CREATE_INDICES,
                    ELASTIC_INDEX_ATTACHED, ELASTIC_INDEX_INGESTED,
                    ELASTIC_PASSWORD, ELASTIC_URL, ELASTIC_USER, ENABLE_CHUNK_INDEX, MAX_CHUNK_CHARS,
                    MIN_CHUNK_WORDS)

logger = logging.getLogger(__name__)

# keyword  → exact match, filterable, aggregatable (ids, tenant, owners)
# text     → full-text searchable prose (summary, key points)
# date     → range queries
MOM_MAPPING: Dict[str, Any] = {
    "properties": {
        "tenant_id":       {"type": "keyword"},
        "user_id":         {"type": "keyword"},
        "conversation_id": {"type": "keyword"},
        "document_ids":    {"type": "keyword"},
        "file_fids":       {"type": "keyword"},
        "document_names":  {"type": "keyword"},
        "source":          {"type": "keyword"},   # "attached" | "ingested"
        "indexed_at":      {"type": "date"},
        "title":           {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "summary":         {"type": "text"},
        "key_points":      {"type": "text"},
        "decisions":       {"type": "text"},
        "key_figures":     {"type": "text"},
        "purpose":         {"type": "text"},
        "agenda":          {"type": "text"},
        "attendees": {"type": "nested", "properties": {
            "name": {"type": "keyword"}, "role": {"type": "keyword"}}},
        "action_items": {"type": "nested", "properties": {
            "task":        {"type": "text"},
            "assigned_to": {"type": "keyword"},
            "assigned_by": {"type": "keyword"},
            "due":         {"type": "keyword"}}},
        # Where the generated .docx landed, mirroring the acknowledgement fields.
        "summary_bucket":     {"type": "keyword"},
        "summary_object_key": {"type": "keyword"},
    }
}


class MomIndex:
    def __init__(self):
        auth = (ELASTIC_USER, ELASTIC_PASSWORD) if ELASTIC_USER else None
        self.client = Elasticsearch(ELASTIC_URL, basic_auth=auth, request_timeout=30)

    def is_ready(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception as e:
            logger.warning(f"[ELASTIC] not reachable at {ELASTIC_URL}: {e}")
            return False

    def ensure_indices(self):
        """Create both indices if missing. No-op when they own the schema."""
        if not ELASTIC_CREATE_INDICES:
            return
        for name in (ELASTIC_INDEX_ATTACHED, ELASTIC_INDEX_INGESTED):
            if not self.client.indices.exists(index=name):
                self.client.indices.create(index=name, mappings=MOM_MAPPING)
                logger.info(f"[ELASTIC] created index {name!r}")

    def index_mom(self, job, mom: Dict[str, Any], *, source: str,
                  summary_bucket: str = "", summary_object_key: str = "") -> tuple:
        """Write one MoM. Returns (index, _id).

        The document id is the conversation id where there is one, otherwise the joined
        document_ids. Kafka is at-least-once, so a redelivered message MUST overwrite rather than
        create a second copy — a stable id is what makes the write idempotent.
        """
        import datetime
        index = ELASTIC_INDEX_ATTACHED if source == "attached" else ELASTIC_INDEX_INGESTED
        doc_id = job.conversation_id or "-".join(job.document_ids) or None
        body = {
            "tenant_id": job.tenant_id, "user_id": job.user_id,
            "conversation_id": job.conversation_id,
            "document_ids": job.document_ids, "file_fids": job.file_fids,
            "document_names": job.document_names, "source": source,
            "indexed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": mom.get("title", ""), "summary": mom.get("summary", ""),
            "key_points": mom.get("key_points", []), "decisions": mom.get("decisions", []),
            "key_figures": mom.get("key_figures", []), "purpose": mom.get("purpose", ""),
            "agenda": mom.get("agenda", []), "attendees": mom.get("attendees", []),
            "action_items": mom.get("action_items", []),
            "summary_bucket": summary_bucket, "summary_object_key": summary_object_key,
        }
        self.client.index(index=index, id=doc_id, document=body, refresh=True)
        logger.info(f"[ELASTIC] indexed {index}/{doc_id} ({source})")
        return index, doc_id


# ── the searchable copy, in their doc-ingest shape ───────────────────────────────────────────────
# Their ingestion service (doc_ingest.py, kept out of this repo) writes one document per chunk:
#
#     {fId, text, pageNo, para, fileName, username, path, in_trash}
#
# with the embedding filled in by the index's default_pipeline, and ids built from
# (fId, pageNo, para) so a re-run overwrites rather than doubles. Minutes written the same way are
# found by the search their product already has; the per-meeting document above is what answers
# "list the open action items", which a chunk index cannot.
#
# The MINUTES are chunked, never the transcript — we do not store transcripts, and the minutes are
# what is worth finding. Each section becomes a "page", so a hit can cite Decisions or Action Items
# rather than an offset, and the chunk ids stay stable when an unrelated section changes.

def _chunk(text: str) -> List[str]:
    """One section's text as overlapping word windows — the rules doc_ingest.py measured.

    Words, not characters, because a character window cuts a word in half and the fragment is noise
    in both the embedding and the keyword index. The character budget is the backstop for text that
    is short on words but long on tokens, like a table row or a run of identifiers.
    """
    size, overlap = CHUNK_WORDS, CHUNK_OVERLAP_WORDS
    if overlap >= size:
        overlap = max(size // 4, 1)
    words = (text or "").split()
    chunks: List[str] = []
    start = 0
    while start < len(words):
        end, length = start, 0
        while end < len(words) and (end - start) < size:
            addition = len(words[end]) + (1 if end > start else 0)
            if length + addition > MAX_CHUNK_CHARS and end > start:
                break
            length += addition
            end += 1
        if end == start:
            end = start + 1
        window = words[start:end]
        # A tail shorter than the minimum is a heading or a stray line, unless it is all there is.
        if len(window) < MIN_CHUNK_WORDS and chunks:
            break
        chunks.append(" ".join(window))
        if end >= len(words):
            break
        start += max(len(window) - overlap, 1)
    return chunks


def _sections(mom: Dict[str, Any]) -> List[tuple]:
    """The minutes as (section name, text) — the order a reader would go through them."""
    def joined(key):
        return " ".join(str(v) for v in (mom.get(key) or []) if str(v).strip())

    items = " ".join(
        " ".join(x for x in (a.get("task", ""), a.get("assigned_to", ""), a.get("due", "")) if x)
        for a in (mom.get("action_items") or []) if isinstance(a, dict)
    )
    people = " ".join(
        " ".join(x for x in (a.get("name", ""), a.get("role", "")) if x)
        for a in (mom.get("attendees") or []) if isinstance(a, dict)
    )
    return [(name, text) for name, text in (
        ("Title", mom.get("title", "")),
        ("Purpose", mom.get("purpose", "")),
        ("Agenda", joined("agenda")),
        ("Attendees", people),
        ("Summary", mom.get("summary", "")),
        ("Key points", joined("key_points")),
        ("Key figures", joined("key_figures")),
        ("Decisions", joined("decisions")),
        ("Action items", items),
    ) if str(text).strip()]


class ChunkIndex:
    """Writes the minutes into their doc-ingest index, one document per chunk.

    Deliberately does NOT create the index. Theirs carries a synonym analyser, a dense_vector
    mapping and the default_pipeline that computes the embedding; an index created here would have
    none of that, would accept writes, and would return nothing from their search — a failure that
    looks like success. If it is missing, say so and skip.
    """

    def __init__(self, index: Optional[str] = None):
        """`index` defaults to CHUNK_INDEX; the API's /v1/translate passes TRANSLATE_CHUNK_INDEX."""
        auth = (ELASTIC_USER, ELASTIC_PASSWORD) if ELASTIC_USER else None
        self.client = Elasticsearch(ELASTIC_URL, basic_auth=auth, request_timeout=30)
        self.index = CHUNK_INDEX if index is None else index

    def is_configured(self) -> bool:
        return bool(ENABLE_CHUNK_INDEX and self.index)

    def index_mom(self, job, mom: Dict[str, Any], *, summary_bucket: str = "",
                  summary_object_key: str = "") -> int:
        """Chunk the minutes and write them, section by section. Returns chunks written, 0 if skipped."""
        name = (job.document_names[0] if job.document_names else "") or mom.get("title") or ""
        return self._write(job, _sections(mom), name, summary_bucket, summary_object_key)

    def _write(self, job, sections: List[tuple], name: str, summary_bucket: str,
               summary_object_key: str) -> int:
        """Write (section name, text) pairs as chunks keyed by the job. Returns chunks written.

        Never raises: the result is already stored and about to be acknowledged by the time this
        runs, so a search copy that fails must not turn a finished job into a failed one.
        """
        if not self.is_configured():
            return 0
        fid = job.conversation_id or "-".join(job.document_ids)
        if not fid:
            logger.warning("[CHUNKS] no conversation id or document ids — nothing to key chunks by")
            return 0
        try:
            if not self.client.indices.exists(index=self.index):
                logger.error(f"[CHUNKS] index {self.index!r} does not exist. It is created by their "
                             "ingestion service, with the analyser, vector mapping and embedding "
                             "pipeline their search needs — not created here. Skipping.")
                return 0

            # Chunk ids are deterministic, so an identical re-run overwrites itself. This is for the
            # run that is NOT identical — a re-processed meeting with different sections leaves old
            # chunks behind under ids this run never writes.
            self.client.delete_by_query(index=self.index, query={"term": {"fId": fid}},
                                        refresh=True, conflicts="proceed")

            name = name or fid
            path = f"{summary_bucket}/{summary_object_key}" if summary_bucket and summary_object_key else ""
            operations: List[Dict[str, Any]] = []
            for page_no, (section, text) in enumerate(sections, start=1):
                for para, chunk in enumerate(_chunk(text)):
                    operations.append({"index": {"_index": self.index,
                                                 "_id": f"{fid}_{page_no}_{para}",
                                                 "routing": fid}})
                    operations.append({"fId": fid, "text": f"{section}. {chunk}",
                                       "pageNo": page_no, "para": para,
                                       "fileName": name, "username": job.user_id or "",
                                       "path": path, "in_trash": False})
            if not operations:
                logger.warning(f"[CHUNKS] {fid} produced no text to index")
                return 0

            reply = self.client.bulk(operations=operations, refresh=True)
            written = len(operations) // 2
            if reply.get("errors"):
                failed = [i["index"] for i in reply.get("items", []) if i.get("index", {}).get("error")]
                logger.error(f"[CHUNKS] {len(failed)} of {written} chunk(s) rejected by "
                             f"{self.index!r}: {failed[:2]}")
                return written - len(failed)
            logger.info(f"[CHUNKS] ↑ {written} chunk(s) for {fid} into {self.index!r}")
            return written
        except Exception as e:
            logger.error(f"[CHUNKS] could not write the search copy for {fid}: {e}")
            return 0


# ── translations: the structured record ─────────────────────────────────────────────────────────────
# The counterpart of MomIndex for a translated audio or video file: one document per file, both texts
# and the languages, so the product can list a user's translations and open one without MinIO.
