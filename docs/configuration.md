# Configuration reference

All settings are environment variables, supplied by `manifests/01-configmap.yaml` (non-secret) and
the `mom-secrets` Secret. Only the ones marked **set** normally need changing.

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
| `GROQ_API_KEYS` | — | legacy name; must be non-empty for the service to start |

## Accuracy features (leave as shipped)
`MOM_WINDOW_KEY_POINTS=true`, `MOM_MERGE_ITEMS=true`, `MOM_VERIFY_DECISIONS=false` (built, not yet
measured on its own), `ENABLE_TRANSCRIBE_RETRY=true`, `ENABLE_TRANSCRIPT_CORRECTION=true`,
`ENABLE_SPEAKER_NAMING=true`, `ENABLE_DIARIZATION=true`, `FILTER_HALLUCINATIONS=true`.

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
| `WHISPER_VAD_MODEL` | /models/whisper/ggml-silero-v6.2.0.bin | local file |
| `DIARIZATION_MODEL_PATH` | /models/nemo/titanet-l.nemo | local file — by name it downloads |
| `VAD_MODEL_PATH` | /models/nemo/vad_multilingual_marblenet.nemo | local file — by name it downloads |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | 1 | |
| `SPEAKER_CLEANUP` | false | optional cluster clean-up (merge over-splits, drop noise). **Off is the behaviour every accuracy figure was measured on** — enable only after an A/B on the test meetings |
| `ENABLE_SPEAKER_ENROLLMENT` | false | recognising pre-registered voices; needs Qdrant. Diarization never does |

## Document translation (transcribe-api)
`ENABLE_OCR=true`, `OCR_LANGS=hin+eng`, `MAX_DOC_MB=50`. Formats: PDF (text or scanned), DOCX, DOC, TXT.
