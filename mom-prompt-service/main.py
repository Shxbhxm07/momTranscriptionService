"""mom-prompt-service: Minutes of Meeting in the JSSD format from a user's prompt and, optionally, a
document (PDF, DOCX, DOC or TXT). No audio.

Two ways in, one job (job.py):
  * Kafka: a message on KAFKA_JOB_TOPIC, the acknowledgement on KAFKA_ACK_TOPIC (consumer.py, started here).
  * HTTP:  POST /v1/mom-prompt with the same JSON; the response body is the same acknowledgement.

The message and the acknowledgement are the audio service's (../docs/kafka-contract.md) plus one field,
`prompt`. The body is taken as a plain JSON object, not a typed model, so the backend's fields go back
exactly as sent: "{}" stays a string, null stays null.
"""
import logging
from typing import Any, Dict

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

import consumer
import job as jobs
from config import ENABLE_KAFKA, KAFKA_ACK_TOPIC, KAFKA_JOB_TOPIC, LLAMA_URL
from core.kafka_contract import build_ack, parse_job
from core.mom import MomGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="MoM from prompt",
              description="Minutes of Meeting in the JSSD format from a prompt and, optionally, a document.")


@app.on_event("startup")
def _startup():
    if ENABLE_KAFKA:
        consumer.start()
    logger.info(f"✓ Ready | kafka={'on' if ENABLE_KAFKA else 'off'} | minutes writer {LLAMA_URL}")


@app.on_event("shutdown")
def _shutdown():
    consumer.stop.set()


@app.get("/")
def root():
    """Liveness. Static, except that a dead consumer thread fails it, so the pod is restarted."""
    if ENABLE_KAFKA and not consumer.is_alive():
        return JSONResponse(status_code=503, content={"service": "mom-prompt-service",
                                                      "status": "kafka consumer thread stopped"})
    return {"service": "mom-prompt-service", "status": "ok"}


@app.get("/health")
def health():
    llm_ok = MomGenerator().is_ready()
    return {
        "status": "healthy" if llm_ok and (consumer.is_alive() or not ENABLE_KAFKA) else "degraded",
        "minutes_writer": {"url": LLAMA_URL, "reachable": llm_ok},
        "kafka": {"enabled": ENABLE_KAFKA, "consumer_running": consumer.is_alive(),
                  "jobs": KAFKA_JOB_TOPIC, "acks": KAFKA_ACK_TOPIC},
    }


_EXAMPLE = {
    "document_ids": ["momp-001"],
    "tenant_id": "test",
    "conversation_id": "momp-001",
    "prompt": "Minutes of the quarterly budget review held on 12 Sep 2026 at HQ. Chaired by the Director "
              "Finance; attended by the heads of IT, Admin and Procurement. Focus on the approved "
              "allocations and who does what by when.",
    "file_urls": ["mom/mom-docs/budget-review-notes.pdf"],
    "document_names": ["budget-review-notes.pdf"],
    "metaData": "{}", "uploadType": "", "grading": "", "data": None, "themes": "",
    "path": "AsItIs", "user": True, "clientSessionId": "sess-8f2c1a", "queryId": "q-41c9",
}


@app.post("/v1/mom-prompt")
def mom_prompt(payload: Dict[str, Any] = Body(..., openapi_examples={
        "prompt_and_pdf": {"summary": "A prompt and a PDF already in MinIO", "value": _EXAMPLE},
        "prompt_only": {"summary": "A prompt describing the meeting, no document",
                        "value": {k: v for k, v in _EXAMPLE.items()
                                  if k not in ("file_urls", "document_names")}}})):
    """Minutes from a prompt and/or documents: the Kafka job, over HTTP.

    200 carries the SUCCESS acknowledgement; 422 (no prompt and no file), 503 (MinIO or Elasticsearch
    unreachable) and 500 (the job failed) carry a FAILURE acknowledgement saying why.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="The body must be a JSON object, like a Kafka job.")
    job = parse_job(payload)
    if not job.prompt and not job.file_urls:
        return JSONResponse(status_code=422, content=build_ack(
            job, success=False, description="No prompt and no file_urls: nothing to write minutes from."))
    try:
        c = jobs.clients()
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] MinIO / Elasticsearch not ready: {e}")
        return JSONResponse(status_code=503, content=build_ack(
            job, success=False, description=f"Storage not ready — {type(e).__name__}: {e}"))
    ack = jobs.process(job, c)
    return JSONResponse(status_code=200 if ack.get("message") == "SUCCESS" else 500, content=ack)
