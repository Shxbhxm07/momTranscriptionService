# mom-prompt-service

JSSD-format Minutes of Meeting from a prompt and, optionally, a document (PDF, DOCX, DOC, TXT). No audio.
Same Kafka / MinIO / Elasticsearch flow and the same message and acknowledgement as the audio MoM
service (`../docs/kafka-contract.md`), plus one field: `prompt`.

- **Build:** `docker build -t mom-prompt-service .` from this folder.
- **Run:** one container; it serves HTTP on 8000 and consumes Kafka in the same process.
- **Kafka:** jobs on `mom-prompt.jobs`, acknowledgements on `mom-prompt.acks` (`KAFKA_JOB_TOPIC`, `KAFKA_ACK_TOPIC`).
- **HTTP:** `POST /v1/mom-prompt` with the same JSON as a Kafka job returns the acknowledgement. `GET /docs` shows examples.
- **Needs:** llama-service (`LLAMA_URL`), MinIO (`MINIO_*`), Elasticsearch (`ELASTIC_*`, `CHUNK_INDEX`). All settings are in `config.py`.
- **OpenShift:** `deploy/openshift.yaml`. Keep the route's 3600 s timeout, because a job holds the request open until the minutes are written.

See `CLAUDE.md` for how it works, its status and what is next.
