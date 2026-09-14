"""The Kafka side: jobs from KAFKA_JOB_TOPIC, acknowledgements to KAFKA_ACK_TOPIC.

Runs as a thread inside the web process (main.py starts it), not as a second deployment: one pod,
one set of settings. The loop is the audio consumer's (../api/consumer.py), with its lessons kept:

  * ONE JOB AT A TIME, and the offset is committed only after the ack is published, so a crash
    mid-job redelivers the job instead of losing it.
  * THE JOB RUNS ON A WORKER THREAD WHILE THIS ONE KEEPS POLLING A PAUSED PARTITION. Kafka ejects a
    consumer that stops polling for max_poll_interval_ms; a long document must not trip that.

Two changes. Messages are parsed here rather than by a value_deserializer, so one message that is not
JSON is logged and skipped instead of failing every poll. And a lost connection is retried in place,
since there is no separate container for the orchestrator to restart.
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import CommitFailedError

from config import (KAFKA_ACK_TOPIC, KAFKA_BOOTSTRAP, KAFKA_GROUP_ID, KAFKA_JOB_TOPIC,
                    KAFKA_MAX_POLL_INTERVAL_MS)
from core.kafka_contract import build_ack, parse_job
import job as jobs

logger = logging.getLogger("consumer")

stop = threading.Event()
_thread = None


def start():
    global _thread
    _thread = threading.Thread(target=_run_forever, name="kafka-consumer", daemon=True)
    _thread.start()


def is_alive() -> bool:
    return _thread is not None and _thread.is_alive()


def _run_forever():
    while not stop.is_set():
        try:
            _loop()
        except Exception as e:
            logger.error(f"consumer stopped ({type(e).__name__}: {e}) — reconnecting in 15 s")
            stop.wait(15)


def _handle(job) -> dict:
    try:
        c = jobs.clients()
    except Exception as e:
        return build_ack(job, success=False, description=f"Storage not ready — {type(e).__name__}: {e}")
    return jobs.process(job, c)


def _loop():
    consumer = KafkaConsumer(
        KAFKA_JOB_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
        group_id=KAFKA_GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=KAFKA_MAX_POLL_INTERVAL_MS,
    )
    producer = KafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
                             value_serializer=lambda v: json.dumps(v).encode())
    worker = ThreadPoolExecutor(max_workers=1)
    logger.info(f"listening on {KAFKA_JOB_TOPIC!r} → acking to {KAFKA_ACK_TOPIC!r} (group={KAFKA_GROUP_ID})")
    try:
        while not stop.is_set():
            for tp, records in (consumer.poll(timeout_ms=5000) or {}).items():
                for rec in records:
                    try:
                        msg = json.loads(rec.value.decode())
                        job = parse_job(msg)
                    except Exception as e:
                        # No ids to acknowledge against. Commit it so it cannot block the partition.
                        logger.error(f"unparseable message at offset {rec.offset}: {e}")
                        consumer.commit()
                        continue
                    logger.info(f"[JOB {job.conversation_id}] received at offset {rec.offset}")
                    consumer.pause(tp)
                    pending = worker.submit(_handle, job)
                    while not pending.done():
                        consumer.poll(timeout_ms=1000)   # paused: no records, but the membership stays alive
                    consumer.resume(tp)
                    ack = pending.result()               # _handle never raises
                    producer.send(KAFKA_ACK_TOPIC, ack)
                    producer.flush()
                    try:
                        consumer.commit()
                        logger.info(f"[JOB {job.conversation_id}] acked {ack['message']}, offset committed")
                    except CommitFailedError as e:
                        logger.error(f"[JOB {job.conversation_id}] acked {ack['message']} but the commit failed "
                                     f"({type(e).__name__}) — Kafka will redeliver this job once")
    finally:
        worker.shutdown(wait=True)
        consumer.close()
        producer.close()
