"""Kafka consumer for the MoM ingestion path.

Runs as its own long-lived compose service, NOT as `docker exec`. Every orphaning problem during
development came from a process attached to a terminal that went away: the client died, the server
carried on transcribing for a job nobody was waiting for, and the API eventually wedged. A
supervised service is restarted on failure, survives disconnection, and is visible in `docker ps`.

ONE MESSAGE AT A TIME, DELIBERATELY. whisper-server serialises on the GPU and the API answers a
concurrent request with 503 rather than queueing it — measured twice, once by accidentally running
two jobs at once. So max_poll_records=1 and no worker pool: throughput here is bounded by the GPU,
not by the consumer.

THE POLL-INTERVAL TRAP. A meeting takes 6-10 minutes to process and Kafka's default
max_poll_interval_ms is 5 minutes. Left alone the broker decides this consumer has died, ejects it
mid-job and hands the message to someone else — every meeting processed twice, visible only on the
bill. The interval below is raised past the worst case, and the Elasticsearch write is idempotent
on a stable document id so a genuine redelivery overwrites instead of duplicating.
"""
import hashlib
import json
import logging
import signal
import sys
import time

import requests
from kafka import KafkaConsumer, KafkaProducer

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

    while _running:
        for _tp, records in (consumer.poll(timeout_ms=5000) or {}).items():
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
                ack = process(job, store, index)
                producer.send(KAFKA_ACK_TOPIC, ack)
                producer.flush()
                consumer.commit()
                logger.info(f"[JOB {job.conversation_id}] acked {ack['message']}, offset committed")

    consumer.close(); producer.close()
    logger.info("stopped cleanly")


if __name__ == "__main__":
    main()
