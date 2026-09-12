"""Configuration for the offline transcription + MoM API.

THREE knobs that matter: WHISPERCPP_URL, NEMO_URL and LLAMA_URL — where the three local
model servers live. Everything else has a measured default (see the comments) and should only be
changed with a measurement to justify it.

There is deliberately NO api-key and NO cloud endpoint here. The MoM service had an
online/offline mode because it could fall back to Groq; this service cannot, by
requirement. Removing the switch is what makes "100% offline" checkable by reading the
file instead of by trusting an environment variable.

ONE CAVEAT, stated plainly: llama-service is reused unmodified and it DOES still carry a
Groq code path, selected by its own VLLM_API_BASE. docker-compose.yml pins that to the
local vLLM. If you run llama-service some other way and forget that variable, it defaults
to Groq — so /health surfaces the backend it actually resolved to rather than assuming.
"""
import os

# ── whisper.cpp server ───────────────────────────────────────────────────────
# The whisper-server binary holds ggml-large-v3 in GPU memory; this process only POSTs
# audio to /inference. That split is what satisfies "load the model once": the model is
# loaded at whisper-server startup and reused for every request for the life of the
# container, no matter how many API workers sit in front of it.
WHISPERCPP_URL = os.getenv("WHISPERCPP_URL", "http://whisper-server:8080").rstrip("/")

# Long meetings are legitimately slow on a single GPU. whisper.cpp streams the whole file
# in one call (no 25 MB cloud limit to chunk around), so the only guard needed is a
# generous ceiling.
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "3600"))

# ── speaker diarization (nemo-service → TitaNet) ─────────────────────────────
# Tells the MoM writer WHO said each line. Optional in the sense that the pipeline still
# produces minutes without it — but measured on real conversational audio, without it the
# model invents speakers and swaps attributions, so leave it on unless you have a reason.
NEMO_URL = os.getenv("NEMO_URL", "http://nemo-service:8003").rstrip("/")
ENABLE_DIARIZATION = os.getenv("ENABLE_DIARIZATION", "true").lower() == "true"

# ~70 s for a 24-minute meeting on GPU; hours on CPU. The ceiling is generous because a
# slow diarization should still finish rather than silently drop speaker labels.
DIARIZE_TIMEOUT = int(os.getenv("DIARIZE_TIMEOUT", "3600"))

# ── MoM writer (llama-service → vLLM → gpt-oss-120b) ─────────────────────────
LLAMA_URL = os.getenv("LLAMA_URL", "http://llama-service:8001").rstrip("/")

# Matches the MoM gateway's SUMMARIZATION_TIMEOUT. A long transcript is chunked and each
# chunk is a separate reasoning-model call, so this is minutes, not seconds.
MOM_TIMEOUT = int(os.getenv("MOM_TIMEOUT", "3600"))

# 0.05 is llama-service's own default for MoM generation. Minutes should be reproducible
# and faithful to the transcript, not creative — this is not a knob to raise.
MOM_TEMPERATURE = float(os.getenv("MOM_TEMPERATURE", "0.05"))

# A transcript this short cannot produce meaningful minutes; below it the API returns the
# transcription with mom=null and a note, rather than letting the model invent a meeting
# out of one sentence.
MIN_TRANSCRIPT_CHARS = int(os.getenv("MIN_TRANSCRIPT_CHARS", "80"))

# Two REFINEMENT passes between transcription and the MoM. Both are pure improvements with a
# no-op failure path, and both are separately switchable so a bad clip can be re-run without
# them to see what they were contributing.
#   ENABLE_SPEAKER_NAMING     — resolve [Speaker_N] to real names read from the words
#   ENABLE_TRANSCRIPT_CORRECTION — repair obvious ASR garble before the LLM builds facts on it
# Each costs one model call. On this hardware that is roughly 20-60s on a long meeting, which
# is why they are flags rather than unconditional.
# Whisper's <|translate|> task is correct for Hindi/Hinglish and LOSSY on English — measured
# 90.2% word agreement against <|transcribe|> on English audio, with two proper nouns dropped.
# So probe the language first and pick the task. Set ENABLE_LANG_DETECT=false to go back to
# translating everything unconditionally.
ENABLE_LANG_DETECT = os.getenv("ENABLE_LANG_DETECT", "true").lower() == "true"
LANG_DETECT_SECONDS = int(os.getenv("LANG_DETECT_SECONDS", "30"))
# Below this confidence the probe is ignored and translate is used — the safe default, since
# translate is never wrong for a non-English meeting.
LANG_DETECT_MIN_PROB = float(os.getenv("LANG_DETECT_MIN_PROB", "0.80"))

ENABLE_SPEAKER_NAMING = os.getenv("ENABLE_SPEAKER_NAMING", "true").lower() == "true"
ENABLE_TRANSCRIPT_CORRECTION = os.getenv("ENABLE_TRANSCRIPT_CORRECTION", "true").lower() == "true"
REFINE_TIMEOUT = int(os.getenv("REFINE_TIMEOUT", "900"))

# ── decoding parameters (all measured on this stack — see comments in core/engine.py) ──
BEAM_SIZE = int(os.getenv("BEAM_SIZE", "5"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
# 32, unchanged — see the re-measurement in core/engine.py. Set MAX_CONTEXT to an empty string to
# send nothing at all, which is only useful for measuring; it is not better at the beam we use.
_max_context = os.getenv("MAX_CONTEXT", "32").strip()
MAX_CONTEXT = int(_max_context) if _max_context else None

# ── audio preprocessing ──────────────────────────────────────────────────────
# Normalisation to 16 kHz mono PCM is NOT optional. whisper-server decodes audio with
# miniaudio (it is built without WHISPER_FFMPEG and started without --convert), which
# MEASURED on this build covers WAV/MP3/FLAC and rejects everything else with a bare
# "Invalid request" — m4a and webm/opus both fail. Those are exactly what phones and
# browsers record, so ffmpeg in this container is what makes real-world uploads work.
# Denoising is optional and OFF by default — it is a quality trade, not a correctness one.
ENABLE_NOISE_REDUCTION = os.getenv("ENABLE_NOISE_REDUCTION", "false").lower() == "true"

# Whisper's own repetition/YouTube-artifact scrubbing. On by default, matching the MoM
# file pipeline. See the limitation note in README.md before turning it off.
FILTER_HALLUCINATIONS = os.getenv("FILTER_HALLUCINATIONS", "true").lower() == "true"

# Re-transcribe ONCE when Whisper skipped too much speech. whisper.cpp occasionally falls into a
# degenerate state for minutes at a time and leaves a hole in every 30 s window. Measured
# 2026-09-10, four runs of one 29-minute meeting: diarized speech falling in Whisper's gaps was
# 5.3 s, 7.5 s and 18.6 s on the good runs and 154.3 s on the bad one — which lost an attendee's
# self-introduction outright. The limit is the larger of the two values below: 45 s sits 2.4x
# above the worst good run and 3.4x below the bad one.
ENABLE_TRANSCRIBE_RETRY = os.getenv("ENABLE_TRANSCRIBE_RETRY", "true").lower() == "true"
TRANSCRIBE_RETRY_LOST_S = float(os.getenv("TRANSCRIBE_RETRY_LOST_S", "45"))
TRANSCRIBE_RETRY_LOST_FRACTION = float(os.getenv("TRANSCRIBE_RETRY_LOST_FRACTION", "0.02"))

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))

# ── document translation (Hindi ⇄ English) ───────────────────────────────────
# Reuses llama-service's /translate_batch — the same vLLM, no second model. See
# core/translate_doc.py for why the batch endpoint is used rather than /translate.

# Documents are small next to audio, and OCR makes a big scan expensive in CPU time
# rather than bytes, so this ceiling is much lower than MAX_UPLOAD_MB.
MAX_DOC_MB = int(os.getenv("MAX_DOC_MB", "50"))

# How much source text goes into one model call, and how many pieces it is cut into.
#
# BOTH are bounded by llama-service's OUTPUT budget, which is the real constraint here.
# generate_translation_batch sizes it as min(max(total_chars // 2, 500) * 3, 6000) — so
# the budget grows with input until 4000 characters and is FLAT above it. Past that point
# a longer batch gets no more room to answer in, and gpt-oss-120b is a reasoning model
# that spends the same budget on its own thinking before it writes a word: overrun it and
# the call returns content=None, which llama-service can only retry once before giving up.
#
# 2500 keeps a third of the ceiling in reserve. MEASURED on this stack: 1873 chars in one
# batch of 10 blocks → 10/10 valid in 43 s, and 970 chars over 6 blocks → 6/6 in 24 s.
# Throughput was ~43 chars/s in both, i.e. bounded by output tokens, not by round trips —
# which is exactly why raising this buys speed only up to the cap and risks truncation
# past it. Do not raise DOC_BATCH_CHARS above 4000.
DOC_BATCH_CHARS = int(os.getenv("DOC_BATCH_CHARS", "2500"))
DOC_BATCH_ITEMS = int(os.getenv("DOC_BATCH_ITEMS", "10"))

# A single block longer than this is split on sentence boundaries before translating.
# Keeping blocks well under DOC_BATCH_CHARS is what lets a batch hold several of them.
DOC_CHUNK_CHARS = int(os.getenv("DOC_CHUNK_CHARS", "1200"))

# ~43 chars/s measured, so a 40k-character document is ~15 minutes of model time. The
# ceiling is generous for the same reason MOM_TIMEOUT is: a slow document should finish.
TRANSLATE_TIMEOUT = int(os.getenv("TRANSLATE_TIMEOUT", "3600"))

# Retries for chunks llama-service could not translate. It reports WHY per item, and the
# two classes deserve different budgets: 'transport' is a network/5xx failure and worth
# retrying generously, while a validation failure ('echo', 'wrong_script', 'mixed_script')
# means the model answered but answered wrongly — re-asking a deterministic model the same
# question is low-yield, so it gets one attempt on its own before the source is kept.
DOC_TRANSPORT_RETRIES = int(os.getenv("DOC_TRANSPORT_RETRIES", "3"))

# ── OCR (scanned PDFs) ───────────────────────────────────────────────────────
# Hindi PDFs from government and legal sources are very often scans with no text layer.
# Without OCR those extract to nothing; the decision is made per PAGE, so a scanned
# appendix in an otherwise digital report is handled without penalising the clean pages.
ENABLE_OCR = os.getenv("ENABLE_OCR", "true").lower() == "true"

# Below this many characters, a PDF page is treated as scanned and sent to OCR. Not zero:
# scanners stamp headers and page numbers into an otherwise imageonly page, so a page with
# a text layer of 12 characters is still a scan.
MIN_PAGE_TEXT_CHARS = int(os.getenv("MIN_PAGE_TEXT_CHARS", "40"))

# 300 dpi is tesseract's documented sweet spot and Devanagari needs it more than Latin
# does: the matras that distinguish ो from ा are one or two pixels tall at 150 dpi, so a
# lower setting does not just blur the text, it changes the words.
OCR_DPI = int(os.getenv("OCR_DPI", "300"))

# Tesseract language data baked into the image (tesseract-ocr-hin / -eng). When a request
# declares its source language the caller narrows this to that one language, which is more
# accurate than the combined model; this is the fallback for an undeclared source.
OCR_LANGS = os.getenv("OCR_LANGS", "hin+eng")

# OCR is CPU-bound at seconds per page. A 500-page scan would hold a worker for most of an
# hour, so pages past this budget are skipped and the response says so.
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "200"))

# LibreOffice converts legacy .doc to .docx. Generous because a cold LibreOffice builds its
# user profile on first run, which dominates the time for a small file.
SOFFICE_TIMEOUT = int(os.getenv("SOFFICE_TIMEOUT", "180"))

# ── protecting code from the translator ──────────────────────────────────────
# A translation model cannot tell a shell command from a sentence. MEASURED on a real
# technical document: `tail -3` came back as `पूंछ -3` ("tail" as in an animal's), a file
# path was renamed into Hindi, and four CONSTANT_CASE identifiers were translated. With
# this on, code-like spans are masked before the model sees them and restored afterwards
# (utils/code_spans.py). Turn it off only to reproduce that behaviour.
PROTECT_CODE_SPANS = os.getenv("PROTECT_CODE_SPANS", "true").lower() == "true"

# gpt-oss intermittently renders numbers in Devanagari digits — measured as ०.८६ for a
# 0.86 threshold, and section headings running "1. 2. 3. ४. ५. 6." in one document. Modern
# Hindi uses ASCII digits, so this is a consistency fix, not a translation choice.
NORMALIZE_DIGITS = os.getenv("NORMALIZE_DIGITS", "true").lower() == "true"


# ── MinIO object storage (Kafka ingestion path) ──────────────────────────────
# Audio arrives as an object key and the generated minutes go back as one. Defaults point at the
# dev stack in docker-compose.dev.yml; IAF's endpoint is a change of these four values.
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
# Empty means "the first path segment of the object path IS the bucket" — see storage.py, where
# the reference message's ambiguity is explained. Set it to pin every input to one bucket instead.
MINIO_INPUT_BUCKET = os.getenv("MINIO_INPUT_BUCKET", "").strip()
# Where generated minutes are written; echoed back as summaryBucketName in the acknowledgement.
MINIO_SUMMARY_BUCKET = os.getenv("MINIO_SUMMARY_BUCKET", "summaries")


# ── Elasticsearch (Kafka ingestion path) ─────────────────────────────────────
# Two indices, split by how the job arrived — see core/search_index.py. Set
# ELASTIC_CREATE_INDICES=false when the indices already exist with their own mapping.
ELASTIC_URL = os.getenv("ELASTIC_URL", "http://elasticsearch:9200")
ELASTIC_USER = os.getenv("ELASTIC_USER", "").strip()
ELASTIC_PASSWORD = os.getenv("ELASTIC_PASSWORD", "").strip()
ELASTIC_INDEX_ATTACHED = os.getenv("ELASTIC_INDEX_ATTACHED", "mom-attached")
ELASTIC_INDEX_INGESTED = os.getenv("ELASTIC_INDEX_INGESTED", "mom-ingested")
ELASTIC_CREATE_INDICES = os.getenv("ELASTIC_CREATE_INDICES", "true").lower() == "true"


# ── Kafka ingestion ──────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_JOB_TOPIC = os.getenv("KAFKA_JOB_TOPIC", "mom.jobs")
KAFKA_ACK_TOPIC = os.getenv("KAFKA_ACK_TOPIC", "mom.acks")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "mom-consumer")
# 20 minutes. A meeting takes 6-10 to process and the Kafka default is 5, at which point the broker
# assumes the consumer died and redelivers the message to someone else — every meeting processed
# twice, visible only on the bill.
KAFKA_MAX_POLL_INTERVAL_MS = int(os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", str(20 * 60 * 1000)))
MOM_API_URL = os.getenv("MOM_API_URL", "http://transcribe-api:8000/transcribe-and-generate-mom")
