# mom-prompt-service

Minutes of Meeting in the client's **JSSD format** from a user's **prompt** and, optionally, a
**document** (PDF, DOCX, DOC or TXT). **No audio.** The user either describes the meeting in the prompt,
or attaches notes/a report and says what they want, or both.

It is the sibling of `../api` (offline-mom-api: audio → MoM, and Hindi⇄English translation) and works
the same way: **Kafka in, MinIO + Elasticsearch, Kafka ack out**, with the same message and ack. The only
new input field is `prompt`. Read `../docs/kafka-contract.md` for the full contract of the audio service.

## How a job runs

```
Kafka mom-prompt.jobs ─┐                                   ┌─▶ Kafka mom-prompt.acks
                       ├─▶ job.process ─▶ same ack ─────────┤
POST /v1/mom-prompt ───┘                                   └─▶ HTTP response body

job.process:  file_urls ─▶ MinIO ─▶ text (PDF text layer; OCR for scanned pages) ─┐
                                                          prompt ─────────────────┴─▶ llama-service /summarize
              minutes ─▶ JSSD .docx ─▶ MinIO {tenant}/summaries/{md5}.docx
                      ─▶ Elasticsearch: record in ELASTIC_INDEX_ATTACHED (id = conversationId)
                                        + chunks in CHUNK_INDEX (their doc-ingest index, never created here)
```

One pod: uvicorn serves HTTP, and `main.py` starts the Kafka consumer as a thread (`ENABLE_KAFKA`).
`GET /` fails if that thread dies, so the pod gets restarted.

## Files

| file | what |
|---|---|
| `main.py` | FastAPI: `/`, `/health`, `POST /v1/mom-prompt`; starts the consumer thread |
| `consumer.py` | Kafka loop: one job at a time, paused-partition polling, commit after the ack |
| `job.py` | the job: read documents, build the text, get minutes, render, store, index, ack |
| `config.py` | every setting, from env. Shared names/defaults match `../api/config.py` |
| `core/mom.py` | `MomGenerator` (calls /summarize) + `to_mom_response` (**copied unchanged** from `../api/core/mom.py`) |
| `core/kafka_contract.py` | copied from `../api`, **adapted**: `prompt` field; `path` is NOT a file fallback here |
| `core/storage.py` | copied unchanged (MinIO) |
| `core/search_index.py` | copied, minutes only (translation index removed) |
| `utils/docx_export.py` | **the JSSD renderer**, copied unchanged |
| `utils/documents.py` | PDF/DOCX/DOC/TXT text extraction with OCR, copied unchanged |
| `deploy/openshift.yaml` | Deployment + Service + Route (with the 3600 s timeout) |

The copied files are copies, not imports, because Jenkins builds each service from its own folder. When
one of them changes in `../api` (especially `docx_export.py`), copy the change here too.

## The contract

**In** (Kafka message or HTTP body), the same fields as `mom.jobs`:

- `prompt`: the user's words. Optional if there is a document.
- `file_urls`: MinIO keys `"bucket/path/file.pdf"`, optional if there is a prompt. Blank entries are skipped.
- `document_names`, `document_ids`, `tenant_id`, `conversation_id` / `conversationId`.
- `mom_meta` (optional): JSSD details no text contains: classification, file_ref, meeting_date,
  meeting_time, venue, secretary, distribution… (see `../docs/kafka-contract.md`).
- Returned **exactly as sent** (same value and JSON type): `accessVar`, `userId`, `isUser`, `user`,
  `path`, `conversationId`, `clientSessionId`, `queryId`, `metaData`, `uploadType`, `grading`, `data`,
  `themes`. `conversationId` is filled from the job; `path` becomes `bucket/key` on success.

A job needs a prompt or a file, at least `MIN_SOURCE_CHARS` (80) characters of text in total (a bare
"make the MoM of yesterday's meeting" is refused; the model would invent the meeting), and at most
`MAX_SOURCE_CHARS`.

**Out**: `fileIds`, `tenantId`, `compareMode`, `action: "save"`, `message: SUCCESS|FAILURE`,
`description` (first 300 characters of the summary, or the reason it failed), and on success
`summaryBucketName` + `summaryObjectKey`. HTTP: 200 SUCCESS; 422 nothing to process; 503 MinIO/Elastic
unreachable; 500 the job failed. The body is always the ack.

## Status (2026-09-14)

- Built and tested **locally only**, inside the `offline-mom-api:latest` image (same dependencies):
  all outcomes with MinIO/Elastic faked, plus the live server with Kafka and Elastic unreachable.
- One **real** run through the local llama-service (OpenRouter, Llama 3.3 70B), on
  `../Meeting 2026-07-21 09_51 UTC_report.pdf` plus a prompt: SUCCESS in 198 s. Title, date, all 5
  agenda items and the key points matched the PDF; the prompt did not leak into the minutes; decisions
  and action items were empty, which is right for that source (a one-speaker talk).
- **Not yet**: built as its own image, deployed, run against real Kafka/MinIO/Elastic, or tried on
  watsonx (the cluster's model, `ibm/granite-4-h-small`).

## Next steps

1. Jenkins: build this folder's `Dockerfile` (build context = this folder).
2. Kafka UI: create topics `mom-prompt.jobs` and `mom-prompt.acks` (1 partition, replication 1, like the others).
3. OCP (trino project): Deployment + Service + Route from `deploy/openshift.yaml`. Env values are the
   same ones mom-consumer uses; passwords from a Secret.
4. Test: upload a PDF to MinIO `mom/mom-docs/`, produce a message on `mom-prompt.jobs` (example below),
   check the ack, the .docx in MinIO, and `GET mom-attached/_doc/<conversationId>` in Kibana.
5. Quality: /summarize's prompt was written for **transcripts**. `job.compose_source` labels the parts
   ("USER'S REQUEST FOR THESE MINUTES", "SOURCE DOCUMENT 1 (name)"). If minutes from documents come out
   wrong on watsonx, the next step is a dedicated endpoint in `../llama-service` with a prompt for
   notes/reports. That costs a llama-service rebuild, so measure first.

```json
{
  "tenant_id": "test", "conversationId": "momp-001", "document_ids": ["momp-001"],
  "prompt": "Prepare the minutes of this meeting. Focus on the decisions and who owns each action.",
  "file_urls": ["mom/mom-docs/notes.pdf"], "document_names": ["notes.pdf"],
  "metaData": "{}", "uploadType": "", "grading": "", "data": null, "themes": "",
  "path": "AsItIs", "user": true, "clientSessionId": "sess-1", "queryId": "q-1"
}
```

## Open questions for the backend developer

- Topic names (`mom-prompt.jobs` / `mom-prompt.acks` are our defaults, settable by env).
- Which of `metaData`, `uploadType`, `grading`, `data`, `themes`, `user` we should fill, and with what
  (today they come back unchanged).
- Whether `path` should be filled with the stored file (current behaviour, as agreed for the audio
  service) or returned unchanged.

## The test cluster (OpenShift, web console)

- Our deployments are in project **trino**: transcribe-api, mom-consumer, translate-consumer, whisper
  (pod label `app=whisper`, Service `whisper-server:8080`), llama (Service `llama-service:8001`, on
  IBM watsonx). **No GPU.**
- Kafka, MinIO and Elasticsearch are in project **teamsync**:
  `kafka-service.teamsync.svc.cluster.local:9092`, `minio-service.teamsync.svc.cluster.local:9000`
  (bucket `mom`), `elasticsearch-service.teamsync.svc.cluster.local:9200` (single node, so replicas 0;
  `mom_v1` was created by hand in Kibana with their `e5-teamsync-faq-pipeline`).
- Images are built by Jenkins from GitHub `Shxbhxm07/momTranscriptionService`, branch main.
- Test messages are produced in **Kafka UI**. Pod logs are in UTC; the user is on IST (UTC+5:30).
- An OpenShift route cuts a request off at its timeout (30 s by default); ours set 3600 s.

## Working with this user

- Plain, simple English, short. Give exact values and **UI click paths** (OCP console, Kafka UI, MinIO
  console), not CLI commands. Give times in IST.
- Before a fix: a short pros/cons table, pick one, apply it in one go. One change at a time.
- They often ask for a one-line Zoho Sprint entry or a Teams update: keep those to one or two lines.
- Commit and push to GitHub when a piece of work is done; they rebuild from it in Jenkins.
- **Never put credentials in files.** The MinIO and Elastic passwords and the IBM key have been pasted
  in chat before; they belong in OpenShift Secrets. Do not send the user's email to any service.

## Testing locally

The `offline-mom-api:latest` image has every dependency (tesseract, LibreOffice, minio, elasticsearch,
kafka-python), so mount this folder into it:

```bash
docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -v "$PWD:/app" -w /app \
  --entrypoint python offline-mom-api:latest your_check.py
```

The local llama-service is the container `offline-mom-llama` on network `offline-mom-api_default`
(`LLAMA_URL=http://offline-mom-llama:8001`). It calls OpenRouter, which costs credits, so check with fakes
first and make real calls deliberately.
