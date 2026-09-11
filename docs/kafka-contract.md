# Kafka contract — MoM service

One consumer, one job at a time. The offset is committed only **after** the ack is published, so a
crash mid-job re-delivers the meeting instead of losing it.

## Job (topic `KAFKA_JOB_TOPIC`, default `mom.jobs`)

```json
{
  "tenant_id": "68beb985d61e19744cbc7a30",
  "userId": "user@example.com",
  "conversationId": "conv-0001",
  "document_ids": ["meeting1"],
  "file_urls": ["docutalk/doccomparecheck1/Weekly Package Team Meeting.mp3"],
  "document_names": ["Weekly Package Team Meeting.mp3"]
}
```

| field | required | meaning |
|---|---|---|
| `conversationId` | yes | stable job id. Also the Elasticsearch document id, so a re-delivered job overwrites instead of duplicating |
| `tenant_id` | yes | used in the summary object path |
| `document_ids` | yes | echoed back in the ack as `fileIds` — the correlation key |
| `file_urls` | yes* | MinIO **object keys**, not URLs. The first path segment is the bucket unless `MINIO_INPUT_BUCKET` is set |
| `document_names` | no | original file name; used for the audio's content type and the logs |
| `file_fids` | — | the "already ingested" path. **Not implemented** — see below |

Both spellings are accepted for every field (`tenant_id`/`tenantId`, `conversationId`/`conversation_id`,
`file_urls`/`fileUrls`…), because the reference messages mix them.

\* A job with no `file_urls` is acknowledged as FAILURE with
`NotImplementedError: no file_urls — the already-ingested path is not implemented`. That is deliberate:
acknowledging SUCCESS for work that never happened would be worse. It needs the backend team to
confirm how a file id resolves to a readable object.

Audio: anything ffmpeg can decode (mp3, wav, m4a, mp4, webm…), up to `MAX_UPLOAD_MB` (500).

## Acknowledgement (topic `KAFKA_ACK_TOPIC`, default `mom.acks`)

Success:
```json
{
  "fileIds": ["meeting1"],
  "tenantId": "68beb985d61e19744cbc7a30",
  "compareMode": "",
  "action": "save",
  "message": "SUCCESS",
  "description": "<first 300 characters of the meeting summary>",
  "summaryBucketName": "summaries",
  "summaryObjectKey": "68beb985d61e19744cbc7a30/summaries/<md5>.docx"
}
```

Failure — `summaryBucketName`/`summaryObjectKey` are **omitted**, never sent empty:
```json
{
  "fileIds": ["meeting1"],
  "tenantId": "68beb985d61e19744cbc7a30",
  "compareMode": "",
  "action": "save",
  "message": "FAILURE",
  "description": "HTTPError: 500 Server Error: ... (one line, at most 400 characters)"
}
```

## What a successful job produces

1. **MinIO**: `<MINIO_SUMMARY_BUCKET>/<tenantId>/summaries/<md5>.docx` — the formatted minutes
2. **Elasticsearch**: document `<conversationId>` in `ELASTIC_INDEX_ATTACHED`, with the structured
   minutes: `title`, `summary`, `key_points`, `decisions`, `action_items` (nested: task, assigned_to,
   assigned_by, due), `attendees`, `agenda`, `key_figures`, plus the MinIO bucket and key
3. **Kafka**: the SUCCESS ack above

## Timing

About 5-20 minutes per meeting depending on length and LLM latency (measured: 9-minute audio ≈ 4-7
min, 29-minute audio ≈ 13-22 min). A job longer than `KAFKA_MAX_POLL_INTERVAL_MS` is safe — the
consumer keeps polling a paused partition while it works, so Kafka does not eject it mid-job.
