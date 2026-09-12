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
| `path` | yes* | the same thing as a single string, which is what the newer backend sends. Used when `file_urls` is absent |
| `accessVar`, `userId`, `isUser` | no | returned in the ack exactly as sent, same value and same type. Never parsed or normalised |
| `document_names` | no | original file name; used for the audio's content type and the logs |
| `file_fids` | — | the "already ingested" path. **Not implemented** — see below |

Both spellings are accepted for every field (`tenant_id`/`tenantId`, `conversationId`/`conversation_id`,
`file_urls`/`fileUrls`…), because the reference messages mix them.

**Fields the backend sends come back untouched.** `accessVar`, `userId`, `isUser`, `path` and
`conversationId` are copied into every acknowledgement, success or failure, with the same value and
the same JSON type — `isUser: true` returns as a boolean, `isUser: "true"` as that string. Our own
fields never collide with them: the minutes' location is `summaryBucketName` / `summaryObjectKey`.

**Where the .docx is written** is `{tenantId}/summaries/{hash}.docx`. With no `tenant_id` the
`conversationId` takes its place, then `userId`, so a key never begins with a slash.

\* A job with no `file_urls` is acknowledged as FAILURE with
`NotImplementedError: no file_urls — the already-ingested path is not implemented`. That is deliberate:
acknowledging SUCCESS for work that never happened would be worse. It needs the backend team to
confirm how a file id resolves to a readable object.

Audio: anything ffmpeg can decode (mp3, wav, m4a, mp4, webm…), up to `MAX_UPLOAD_MB` (500).

### Optional `mom_meta`: details for the Word minutes

The .docx follows the client's JSSD minutes layout (Vol I Part 2, Appendix AD). A recording cannot
supply the parts of that layout listed below, so they come from this optional object. Anything missing
is left out, or left blank for the secretary to fill in. **The security classification is never
guessed.** Without `classification` the minutes carry no marking, which is how JSSD treats an
unclassified document.

```json
"mom_meta": {
  "classification": "CONFIDENTIAL",
  "precedence": "PRIORITY",
  "copy_no": "2/7",
  "telephone": "011-23012345",
  "address": ["Headquarters Integrated Defence Staff", "Kashmir House, Rajaji Marg", "New Delhi 110011"],
  "file_ref": "A/12345/DOT/MoM",
  "issue_date": "2026-09-12",
  "venue": "Conference Room, HQ IDS",
  "meeting_date": "2026-09-10",
  "meeting_time": "10:30",
  "secretary": {"name": "SK Rao", "rank": "Maj"},
  "amendments_by": "2026-09-20",
  "distribution": [{"addressee": "DCIDS (DOT)", "copies": "One", "copy_no": "1", "remarks": "By hand"}],
  "draft": false
}
```

| field | effect |
|---|---|
| `classification` | TOP SECRET, SECRET, CONFIDENTIAL or RESTRICTED (TOPSEC, CONFD and RESTD are accepted). Printed in full at the head and foot of every page, on every item, and as a diagonal watermark. CONFIDENTIAL and above also show the number of pages on page 1 |
| `precedence`, `copy_no`, `telephone`, `address`, `file_ref` | the superscription above the title. For SECRET and above the copy number also goes into the watermark |
| `issue_date` | printed after "dt". JSSD leaves it blank for the signatory to write in ink |
| `venue`, `meeting_date`, `meeting_time` | the title. When absent, whatever the meeting itself states is used |
| `secretary` | the signature block. Otherwise it reads "(Initials and Name)", "Rank", "Secretary" |
| `amendments_by` | the date in the closing line "agreement with the minutes will be assumed unless amendments are received by …" |
| `distribution` | the distribution table. The office file copy is always added |
| `draft` | marks page 1 DRAFT and uses 1.5 line spacing, as JSSD requires for drafts |

Dates in any common form are written the JSSD way (`10 Sep 26`), and times as `1030 Hr`. Both
`mom_meta` and `momMeta` are accepted.

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

1. **MinIO**: `<MINIO_SUMMARY_BUCKET>/<tenantId>/summaries/<md5>.docx` — the minutes in the JSSD
   Appendix AD layout (`api/utils/docx_export.py` cites the rule behind each element)
2. **Elasticsearch**: document `<conversationId>` in `ELASTIC_INDEX_ATTACHED`, with the structured
   minutes: `title`, `summary`, `key_points`, `decisions`, `action_items` (nested: task, assigned_to,
   assigned_by, due), `attendees`, `agenda`, `key_figures`, plus the MinIO bucket and key
3. **Kafka**: the SUCCESS ack above

## Timing

About 5-20 minutes per meeting depending on length and LLM latency (measured: 9-minute audio ≈ 4-7
min, 29-minute audio ≈ 13-22 min). A job longer than `KAFKA_MAX_POLL_INTERVAL_MS` is safe — the
consumer keeps polling a paused partition while it works, so Kafka does not eject it mid-job.
