"""Kafka consumer for the MoM ingestion path.

Runs as its own long-lived compose service, NOT as `docker exec`. Every orphaning problem during
development came from a process attached to a terminal that went away: the client died, the server
carried on transcribing for a job nobody was waiting for, and the API eventually wedged. A
supervised service is restarted on failure, survives disconnection, and is visible in `docker ps`.

ONE MESSAGE AT A TIME, DELIBERATELY. whisper-server serialises on the GPU and the API answers a
concurrent request with 503 rather than queueing it — measured twice, once by accidentally running
two jobs at once. So max_poll_records=1 and no worker pool: throughput here is bounded by the GPU,
not by the consumer.

THE POLL-INTERVAL TRAP. Kafka ejects a consumer that has not called poll() within
max_poll_interval_ms. The first fix raised that interval "past the worst case" (20 min), and the
worst case was wrong: measured 2026-09-10, a 29-minute meeting on a slow provider day took 22 min.
The consumer was ejected at minute 20, sent its SUCCESS ack, crashed on the offset commit, restarted
and processed the same meeting again — and would have looped forever, re-acking and re-billing on
every pass. No fixed interval is safe when a two-hour meeting is a normal input.

So the job runs on a worker thread while this thread keeps calling poll() with the partition
paused. A paused poll() returns nothing, but it proves the consumer is alive: a job can take as long
as it needs, and a dead process is still noticed within the session timeout. The Elasticsearch write
stays idempotent on a stable document id, so a genuine redelivery overwrites instead of duplicating.
"""
import hashlib
import tempfile
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import CommitFailedError

sys.path.insert(0, "/app")
from config import (JOB_KIND, KAFKA_ACK_TOPIC, KAFKA_BOOTSTRAP, KAFKA_GROUP_ID,  # noqa: E402
                    KAFKA_JOB_TOPIC, KAFKA_MAX_POLL_INTERVAL_MS, MOM_API_URL, MOM_TIMEOUT,
                    TRANSLATE_API_URL)
from core.kafka_contract import build_ack, parse_job, summary_object_key  # noqa: E402
from core.search_index import ChunkIndex, MomIndex, TranslationIndex  # noqa: E402
from core.storage import ObjectStore  # noqa: E402
from core.translate_doc import describe_media_translation  # noqa: E402
from utils.audio_processing import media_file_to_wav  # noqa: E402
from utils.docx_export import build_mom_docx, build_translation_docx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("consumer")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_running = True

# LIVENESS. This service runs no web server, so the API image's HTTP healthcheck can never pass here
# and was switched off — which left an orchestrator nothing to watch, and a hung consumer would sit
# forever holding the partition. The poll loop touches this file on every pass: at most every 5 s
# when idle, every 1 s while a job runs (the loop keeps polling a paused partition, see main()). So a
# file older than a couple of minutes means the loop itself is stuck, not that a meeting is long:
#   livenessProbe: exec: ["sh", "-c", "test $(( $(date +%s) - $(stat -c %Y /tmp/consumer-alive) )) -lt 120"]
HEARTBEAT_FILE = os.getenv("CONSUMER_HEARTBEAT_FILE", "/tmp/consumer-alive")


def _beat():
    try:
        Path(HEARTBEAT_FILE).touch()
    except OSError as e:       # a read-only filesystem must not take the consumer down with it
        logger.warning(f"heartbeat not written ({e}) — the liveness probe will fail")


def _stop(signum, _frame):
    """Finish the message in hand, then exit — killing mid-job is what strands work on the GPU."""
    global _running
    logger.info(f"signal {signum} — finishing current message then stopping")
    _running = False


def process(job, store: ObjectStore, index: MomIndex, chunks: ChunkIndex) -> dict:
    """One job → an acknowledgement. Never raises: a crash here would lose the ack."""
    t0 = time.time()
    try:
        if not job.file_urls:
            # The ingested-file path (document_ids / file_fids) is not built yet; say so plainly
            # rather than acknowledging success for work that never happened.
            raise NotImplementedError("no file_urls — the already-ingested path is not implemented")

        audio = store.download(job.file_urls[0])
        name = job.document_names[0] if job.document_names else job.file_urls[0].rsplit("/", 1)[-1]
        logger.info(f"[JOB {job.conversation_id}] downloaded {name} ({len(audio)/1048576:.1f} MB)")

        r = requests.post(MOM_API_URL, files={"audio": (name, audio, "audio/mpeg")}, timeout=MOM_TIMEOUT)
        r.raise_for_status()
        body = r.json()
        mom = body.get("mom")
        if not mom:
            raise RuntimeError(body.get("note") or "no minutes produced")

        docx_bytes = build_mom_docx(mom, job.mom_meta)
        bucket, key = store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest()),
                                   docx_bytes, DOCX_MIME)
        index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key)
        # The searchable copy, in their doc-ingest shape. Last, and it never raises: by this point
        # the minutes are stored and about to be acknowledged, so a failure here must not undo a
        # finished job. Does nothing until ENABLE_CHUNK_INDEX and CHUNK_INDEX are set.
        chunks.index_mom(job, mom, summary_bucket=bucket, summary_object_key=key)
        logger.info(f"[JOB {job.conversation_id}] done in {time.time()-t0:.0f}s — {bucket}/{key}")
        return build_ack(job, success=True, bucket=bucket, object_key=key,
                         description=(mom.get("summary") or "")[:300])
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        return build_ack(job, success=False, description=f"{type(e).__name__}: {e}")


def process_translation(job, store: ObjectStore, tindex: TranslationIndex, chunks: ChunkIndex) -> dict:
    """One translation job → an acknowledgement. Never raises: a crash here would lose the ack.

    The media is streamed from MinIO to disk and reduced to its audio track HERE, before it is sent:
    requests builds a multipart body in memory, so posting a 2 GB video would hold 2 GB, while the
    same hour of 16 kHz mono speech is about 115 MB. The API converts again, which on a WAV is cheap.
    """
    t0 = time.time()
    media = wav = None
    try:
        if not job.file_urls:
            raise NotImplementedError("no file_urls or path — nothing to translate")
        name = job.document_names[0] if job.document_names else job.file_urls[0].rsplit("/", 1)[-1]
        fd, media = tempfile.mkstemp(suffix=os.path.splitext(name)[1] or ".bin")
        os.close(fd)
        size = store.download_to_file(job.file_urls[0], media)
        logger.info(f"[JOB {job.conversation_id}] downloaded {name} ({size/1048576:.1f} MB)")

        wav = media_file_to_wav(media)
        os.unlink(media)
        media = None
        with open(wav, "rb") as fh:
            r = requests.post(TRANSLATE_API_URL, files={"file": (os.path.splitext(name)[0] + ".wav", fh, "audio/wav")},
                              timeout=MOM_TIMEOUT)
        if r.status_code != 200:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
            raise RuntimeError(f"translate-media answered {r.status_code}: {str(detail)[:300]}")
        result = r.json()
        # Built here, not taken from the API: only this side knows the file's real name (the API
        # was sent the audio track as .wav, so it would call a video "audio"), and an API image
        # older than the consumer sends no description at all.
        result["description"] = describe_media_translation(name, result)

        docx_bytes = build_translation_docx(result, name)
        bucket, key = store.upload(
            summary_object_key(job, hashlib.md5(docx_bytes).hexdigest(), folder="translations"),
            docx_bytes, DOCX_MIME)
        # The same three destinations as the minutes: the file (above), the structured record, and
        # the searchable chunks. The chunk copy never raises, so it cannot fail a finished job.
        tindex.index_translation(job, result, summary_bucket=bucket, summary_object_key=key)
        chunks.index_translation(job, result, summary_bucket=bucket, summary_object_key=key)
        logger.info(f"[JOB {job.conversation_id}] {result.get('source_lang')} → {result.get('target_lang')} "
                    f"done in {time.time()-t0:.0f}s — {bucket}/{key}")
        return build_ack(job, success=True, bucket=bucket, object_key=key,
                         description=result["description"])
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        return build_ack(job, success=False, description=f"{type(e).__name__}: {e}")
    finally:
        for path in (media, wav):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if JOB_KIND not in ("mom", "translate"):
        raise SystemExit(f"JOB_KIND must be 'mom' or 'translate', not {JOB_KIND!r}")
    store = ObjectStore()
    if JOB_KIND == "mom":
        index, chunks = MomIndex(), ChunkIndex()
        index.ensure_indices()
        handle = lambda job: process(job, store, index, chunks)  # noqa: E731
    else:
        tindex, chunks = TranslationIndex(), ChunkIndex()
        tindex.ensure_index()
        handle = lambda job: process_translation(job, store, tindex, chunks)  # noqa: E731
    consumer = KafkaConsumer(
        KAFKA_JOB_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
        group_id=KAFKA_GROUP_ID,
        value_deserializer=lambda b: json.loads(b.decode()),
        # Commit only after the ack is published, so a crash mid-job redelivers rather than
        # silently dropping the meeting.
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=KAFKA_MAX_POLL_INTERVAL_MS,
    )
    producer = KafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
                             value_serializer=lambda v: json.dumps(v).encode())
    logger.info(f"[{JOB_KIND}] listening on {KAFKA_JOB_TOPIC!r} → acking to {KAFKA_ACK_TOPIC!r} "
                f"(group={KAFKA_GROUP_ID}, poll interval {KAFKA_MAX_POLL_INTERVAL_MS/60000:.0f}m)")

    worker = ThreadPoolExecutor(max_workers=1)
    while _running:
        _beat()
        for tp, records in (consumer.poll(timeout_ms=5000) or {}).items():
            for rec in records:
                try:
                    job = parse_job(rec.value)
                except Exception as e:
                    # A message we cannot even parse has no ids to acknowledge against. Commit it
                    # so it does not block the partition forever, and say so loudly.
                    logger.error(f"unparseable message at offset {rec.offset}: {e}")
                    consumer.commit()
                    continue
                logger.info(f"[JOB {job.conversation_id}] received at offset {rec.offset}")
                consumer.pause(tp)
                pending = worker.submit(handle, job)
                while not pending.done():
                    _beat()
                    consumer.poll(timeout_ms=1000)   # paused → no records, but keeps the membership alive
                consumer.resume(tp)
                ack = pending.result()               # process() never raises
                producer.send(KAFKA_ACK_TOPIC, ack)
                producer.flush()
                try:
                    consumer.commit()
                    logger.info(f"[JOB {job.conversation_id}] acked {ack['message']}, offset committed")
                except CommitFailedError as e:
                    # Should not happen now that the loop keeps polling. But an uncaught failure here
                    # is exactly what turned one slow job into an endless reprocess loop, so log it
                    # and carry on: the job is redelivered once, and Elasticsearch overwrites.
                    logger.error(f"[JOB {job.conversation_id}] acked {ack['message']} but the offset commit "
                                 f"failed ({type(e).__name__}) — Kafka will redeliver this job once")

    worker.shutdown(wait=True)
    consumer.close(); producer.close()
    logger.info("stopped cleanly")


if __name__ == "__main__":
    main()
