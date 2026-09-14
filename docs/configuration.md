# Configuration reference

All settings are environment variables: non-secret ones from the overlay's `offline-mom.env` (built into
the `offline-mom-config` ConfigMap), credentials from the `offline-mom-secrets` Secret — see
`deploy/openshift/OVERLAYS.md`. Only the ones marked **set** normally need changing.

## LLM — IBM watsonx (llama-service)
| variable | default | |
|---|---|---|
| `VLLM_API_BASE` | — | **set**: full watsonx URL incl. `?version=`; `/ml/v1/text/chat` or `/ml/v1/text/generation` |
| `LLM_MODEL_PATH` | — | **set**: e.g. `meta-llama/llama-3-3-70b-instruct` |
| `WATSONX_PROJECT_ID` | — | **set** |
| `CP4D_AUTH_URL` | — | **set**: `https://<cluster>/icp4d-api/v1/authorize` |
| `CP4D_USERNAME`, `CP4D_API_KEY` | — | **secret** |
| `LLM_AUTH_MODE` | auto | `cp4d` (set automatically when `CP4D_AUTH_URL` is present), `iam`, or `bearer` |
| `CP4D_TOKEN_TTL` | 3600 | seconds between re-authorizations |
| `LLM_VERIFY_SSL` | true | `false` for a self-signed cluster certificate |
| `LLM_CONCURRENCY` | 2 | parallel LLM calls per meeting |
| `MODEL_CONTEXT_LIMIT` | 32768 | the model's context window |
| `WATSONX_API_KEY` | — | the IBM Cloud API key, for `LLM_AUTH_MODE=iam`. Not needed on Cloud Pak for Data, which uses `CP4D_USERNAME` / `CP4D_API_KEY`. `LLM_API_KEY` and the historical `GROQ_API_KEYS` are read after it, in that order |

## Accuracy features (leave as shipped)
`MOM_WINDOW_KEY_POINTS=true`, `MOM_MERGE_ITEMS=true`, `MOM_VERIFY_DECISIONS=false` (built, not yet
measured on its own), `ENABLE_TRANSCRIBE_RETRY=true`, `ENABLE_TRANSCRIPT_CORRECTION=true`,
`ENABLE_SPEAKER_NAMING=true`, `FILTER_HALLUCINATIONS=true`. `ENABLE_DIARIZATION` defaults to true in
the code but the shipped env files set it to **false** — diarization is off until further notice, so
nemo-service is not deployed and attendees come only from names said aloud.

## Kafka / MinIO / Elasticsearch (transcribe-api, mom-consumer)
| variable | default | |
|---|---|---|
| `KAFKA_BOOTSTRAP` | kafka:9092 | **set** |
| `KAFKA_JOB_TOPIC` / `KAFKA_ACK_TOPIC` | mom.jobs / mom.acks | **set** to the agreed topics |
| `KAFKA_GROUP_ID` | mom-consumer | |
| `MINIO_ENDPOINT` | minio:9000 | **set** (host:port) |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | minioadmin | **secret — the defaults must not reach any shared environment** |
| `MINIO_SECURE` | false | `true` for TLS |
| `MINIO_INPUT_BUCKET` | (empty) | empty = first segment of `file_urls` is the bucket |
| `MINIO_SUMMARY_BUCKET` | summaries | **set** |
| `ELASTIC_URL` | http://elasticsearch:9200 | **set** |
| `ELASTIC_USER`, `ELASTIC_PASSWORD` | (empty) | **secret** |
| `ELASTIC_INDEX_ATTACHED` / `_INGESTED` | mom-attached / mom-ingested | **set** to the agreed names |
| `ELASTIC_CREATE_INDICES` | true | `false` if your team owns the mappings |

## Speech (whisper-server, nemo-service)
| variable | value | |
|---|---|---|
| `WHISPER_MODEL` | /models/whisper/ggml-large-v3.bin | local file |
| `TRANSCRIBE_API_URL` (transcribe-api) | empty | use a media-transcription service INSTEAD of our Whisper: its full endpoint, e.g. `http://transcription-teamsync.apps.lab.ocp.lan/v1/media/audio-video/transcription`. A stopgap for a cluster where our Whisper cannot run yet. Measured 2026-09-13: about 3.5x real time plus ~1 min per request, and it writes the language spoken (no translate task, no language id), so Hindi meetings reach the minutes writer as Hindi/Hinglish — unmeasured for minutes accuracy. Translation is unaffected: it wants the spoken language anyway |
| `WHISPER_VAD_MODEL` | /models/whisper/ggml-silero-v6.2.0.bin | local file |
| `MAX_CONTEXT` (transcribe-api) | 32 | text context sent to Whisper per request. Leave it alone: 0 and 64 both failed when it was measured, and a 2026-09-12 re-measurement at the beam size we use found no reason to change it |
| `DIARIZATION_MODEL_PATH` | /models/nemo/titanet-l.nemo | local file — by name it downloads |
| `VAD_MODEL_PATH` | /models/nemo/vad_multilingual_marblenet.nemo | local file — by name it downloads |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | 1 | |
| `SPEAKER_CLEANUP` | false | optional cluster clean-up (merge over-splits, drop noise). **Off is the behaviour every accuracy figure was measured on** — enable only after an A/B on the test meetings |
| `ENABLE_SPEAKER_ENROLLMENT` | false | recognising pre-registered voices; needs Qdrant. Diarization never does |

## Document translation (transcribe-api)
`ENABLE_OCR=true`, `OCR_LANGS=hin+eng`, `MAX_DOC_MB=50`. Formats: PDF (text or scanned), DOCX, DOC, TXT.

## The searchable copy (transcribe-api / mom-consumer)

Their platform's search reads an index of CHUNKS with embeddings, written by their own ingestion
service. Our per-meeting document cannot be found by it, so the minutes are written there too —
one document per chunk, in their exact shape (`fId`, `text`, `pageNo`, `para`, `fileName`,
`username`, `path`, `in_trash`), with ids built from the conversation id so a re-run replaces
rather than doubles. The index is never created here: theirs carries the synonym analyser, the
vector mapping and the embedding pipeline, and one created by us would accept writes and return
nothing from their search.

| variable | value | |
|---|---|---|
| `ENABLE_CHUNK_INDEX` | false | off until the index name is known |
| `CHUNK_INDEX` | — | the index their search reads, e.g. `teamsync_v1` |
| `CHUNK_WORDS` | 120 | mirrors their doc_ingest; above this the embedding model truncates silently |
| `CHUNK_OVERLAP_WORDS` | 30 | so a sentence across a boundary is whole in one chunk |
| `MIN_CHUNK_WORDS` | 15 | below this a chunk is a heading, not material |
| `MAX_CHUNK_CHARS` | 1200 | backstop for text short on words but long on tokens |

## Translation of audio and video

| variable | where | value | |
|---|---|---|---|
| `JOB_KIND` | consumer | `mom` | `translate` makes this consumer a translation consumer: same image, own topics |
| `TRANSLATE_API_URL` | consumer | `http://transcribe-api:8000/translate-media` | where a translation consumer sends the audio |
| `SELF_API_URL` | transcribe-api | `http://127.0.0.1:8000` | where `POST /v1/mom` and `/v1/translate` reach this API's own endpoints. Loopback, so the audio never leaves the pod |
| `TRANSLATE_CHUNK_INDEX` | transcribe-api | *(empty)* | the searchable-chunk index `POST /v1/translate` writes to (e.g. `translate_v1`). Empty = translation chunks skipped rather than mixed into `CHUNK_INDEX` |
| `ELASTIC_INDEX_TRANSLATIONS` | translate consumer | `translations` | the structured record of each translated file, created on startup like the minutes index |
| `MAX_MEDIA_MB` | transcribe-api | 4096 | largest audio or video `/translate-media` accepts. The upload is streamed to disk, so this caps disk, not memory |

