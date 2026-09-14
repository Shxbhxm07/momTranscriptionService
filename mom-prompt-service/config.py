"""Settings, all from the environment. Where a setting is shared with the audio service, the name and
the default are the same as in ../api/config.py, so one set of cluster values serves both."""
import os

# ── the minutes writer ─────────────────────────────────────────────────────────
LLAMA_URL = os.getenv("LLAMA_URL", "http://llama-service:8001").rstrip("/")
MOM_TIMEOUT = int(os.getenv("MOM_TIMEOUT", "3600"))
MOM_TEMPERATURE = float(os.getenv("MOM_TEMPERATURE", "0.05"))

# ── what a job may carry ───────────────────────────────────────────────────────
# Below this many characters of prompt + document text there is no meeting to write up, and the model
# would invent one. "Make the minutes of yesterday's meeting" is a request, not a source.
MIN_SOURCE_CHARS = int(os.getenv("MIN_SOURCE_CHARS", "80"))
# Above this the job is refused rather than sent: llama-service splits long text and calls the model per
# part, so a 300-page file would be dozens of calls for minutes nobody asked for. ~75k tokens.
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "300000"))
MAX_DOC_MB = int(os.getenv("MAX_DOC_MB", "50"))

# ── reading documents (utils/documents.py) ─────────────────────────────────────
ENABLE_OCR = os.getenv("ENABLE_OCR", "true").lower() == "true"
MIN_PAGE_TEXT_CHARS = int(os.getenv("MIN_PAGE_TEXT_CHARS", "40"))
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_LANGS = os.getenv("OCR_LANGS", "hin+eng")
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "200"))
SOFFICE_TIMEOUT = int(os.getenv("SOFFICE_TIMEOUT", "180"))

# ── MinIO ──────────────────────────────────────────────────────────────────────
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
# Empty: the first part of each file_urls entry is the bucket ("mom/mom-docs/notes.pdf").
MINIO_INPUT_BUCKET = os.getenv("MINIO_INPUT_BUCKET", "").strip()
# Where the Word minutes go, as {tenant}/summaries/{hash}.docx; returned as summaryBucketName.
MINIO_SUMMARY_BUCKET = os.getenv("MINIO_SUMMARY_BUCKET", "summaries")

# ── Elasticsearch ──────────────────────────────────────────────────────────────
ELASTIC_URL = os.getenv("ELASTIC_URL", "http://elasticsearch:9200")
ELASTIC_USER = os.getenv("ELASTIC_USER", "").strip()
ELASTIC_PASSWORD = os.getenv("ELASTIC_PASSWORD", "").strip()
# The same indices as the audio minutes, so a search over "all minutes" finds these too.
ELASTIC_INDEX_ATTACHED = os.getenv("ELASTIC_INDEX_ATTACHED", "mom-attached")
ELASTIC_INDEX_INGESTED = os.getenv("ELASTIC_INDEX_INGESTED", "mom-ingested")
ELASTIC_CREATE_INDICES = os.getenv("ELASTIC_CREATE_INDICES", "true").lower() == "true"
# Their doc-ingest chunk index (e.g. mom_v1). Never created here; see core/search_index.py.
ENABLE_CHUNK_INDEX = os.getenv("ENABLE_CHUNK_INDEX", "false").lower() == "true"
CHUNK_INDEX = os.getenv("CHUNK_INDEX", "").strip()
CHUNK_WORDS = int(os.getenv("CHUNK_WORDS", "120"))
CHUNK_OVERLAP_WORDS = int(os.getenv("CHUNK_OVERLAP_WORDS", "30"))
MIN_CHUNK_WORDS = int(os.getenv("MIN_CHUNK_WORDS", "15"))
MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "1200"))

# ── Kafka ──────────────────────────────────────────────────────────────────────
# The consumer runs as a thread in the web process. ENABLE_KAFKA=false leaves only the HTTP endpoint.
ENABLE_KAFKA = os.getenv("ENABLE_KAFKA", "true").lower() == "true"
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_JOB_TOPIC = os.getenv("KAFKA_JOB_TOPIC", "mom-prompt.jobs")
KAFKA_ACK_TOPIC = os.getenv("KAFKA_ACK_TOPIC", "mom-prompt.acks")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "mom-prompt-consumer")
KAFKA_MAX_POLL_INTERVAL_MS = int(os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", str(20 * 60 * 1000)))
