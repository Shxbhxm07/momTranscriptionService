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
import json
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import CommitFailedError

sys.path.insert(0, "/app")
from config import (KAFKA_ACK_TOPIC, KAFKA_BOOTSTRAP, KAFKA_GROUP_ID,  # noqa: E402
                    KAFKA_JOB_TOPIC, KAFKA_MAX_POLL_INTERVAL_MS, MOM_API_URL, MOM_TIMEOUT)
from core.kafka_contract import build_ack, parse_job, summary_object_key  # noqa: E402
from core.search_index import MomIndex  # noqa: E402
from core.storage import ObjectStore  # noqa: E402
from utils.docx_export import build_mom_docx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("consumer")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_running = True


def _stop(signum, _frame):
    """Finish the message in hand, then exit — killing mid-job is what strands work on the GPU."""
    global _running
    logger.info(f"signal {signum} — finishing current message then stopping")
    _running = False


def process(job, store: ObjectStore, index: MomIndex) -> dict:
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

        docx_bytes = build_mom_docx(mom)
        bucket, key = store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest()),
                                   docx_bytes, DOCX_MIME)
        index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key)
        logger.info(f"[JOB {job.conversation_id}] done in {time.time()-t0:.0f}s — {bucket}/{key}")
        return build_ack(job, success=True, bucket=bucket, object_key=key,
                         description=(mom.get("summary") or "")[:300])
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        return build_ack(job, success=False, description=f"{type(e).__name__}: {e}")


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    store, index = ObjectStore(), MomIndex()
    index.ensure_indices()
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
    logger.info(f"listening on {KAFKA_JOB_TOPIC!r} → acking to {KAFKA_ACK_TOPIC!r} "
                f"(group={KAFKA_GROUP_ID}, poll interval {KAFKA_MAX_POLL_INTERVAL_MS/60000:.0f}m)")

    worker = ThreadPoolExecutor(max_workers=1)
    while _running:
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
                pending = worker.submit(process, job, store, index)
                while not pending.done():
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
