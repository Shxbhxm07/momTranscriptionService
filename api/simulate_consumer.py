"""Run the Kafka job flow WITHOUT Kafka — a stand-in for the consumer that doesn't exist yet.

Does exactly what the real consumer will do, in order: pull the audio out of MinIO by object key,
push it through the existing HTTP pipeline, write the MoM to Elasticsearch, upload the .docx, and
print the acknowledgement that would go back on mom.acks. Kept as a script so the flow can be
watched end-to-end before any broker wiring exists.

  docker exec offline-mom-api python /app/simulate_consumer.py '<json message>'
"""
import hashlib
import json
import sys
import time

import requests

sys.path.insert(0, "/app")
from core.kafka_contract import build_ack, parse_job, summary_object_key   # noqa: E402
from core.search_index import MomIndex                                      # noqa: E402
from core.storage import ObjectStore                                        # noqa: E402
from utils.docx_export import build_mom_docx                                # noqa: E402

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def main(msg: dict):
    job = parse_job(msg)
    store, index = ObjectStore(), MomIndex()
    index.ensure_indices()
    t0 = time.time()
    print(f"[1/5] job tenant={job.tenant_id} conversation={job.conversation_id} "
          f"attachments={len(job.file_urls)}")

    try:
        path = job.file_urls[0]
        audio = store.download(path)
        print(f"[2/5] downloaded {path} — {len(audio)/1048576:.1f} MB")

        name = job.document_names[0] if job.document_names else path.rsplit("/", 1)[-1]
        r = requests.post("http://localhost:8000/transcribe-and-generate-mom",
                          files={"audio": (name, audio, "audio/mpeg")}, timeout=3600)
        r.raise_for_status()
        body = r.json()
        mom = body.get("mom")
        if not mom:
            raise RuntimeError(body.get("note") or "no minutes produced")
        print(f"[3/5] minutes generated — {len(mom['key_points'])} key points, "
              f"{len(mom['decisions'])} decisions, {len(mom['action_items'])} actions "
              f"({time.time()-t0:.0f}s)")

        docx_bytes = build_mom_docx(mom)
        digest = hashlib.md5(docx_bytes).hexdigest()
        bucket, key = store.upload(summary_object_key(job, digest), docx_bytes, DOCX_MIME)
        print(f"[4/5] uploaded minutes — {bucket}/{key} ({len(docx_bytes)} bytes)")

        idx, doc_id = index.index_mom(job, mom, source="attached",
                                      summary_bucket=bucket, summary_object_key=key)
        print(f"[5/5] indexed — {idx}/{doc_id}")

        ack = build_ack(job, success=True, bucket=bucket, object_key=key,
                        description=(mom.get("summary") or "")[:300])
    except Exception as e:
        ack = build_ack(job, success=False, description=f"{type(e).__name__}: {e}")
        print(f"[!] FAILED — {type(e).__name__}: {e}")

    print(f"\nACK (would publish to mom.acks) after {time.time()-t0:.0f}s:")
    print(json.dumps(ack, indent=2)[:1100])


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
