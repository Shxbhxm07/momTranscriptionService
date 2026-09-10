# Offline English transcription API — the thin HTTP layer only.
#
# THE MODEL IS NOT IN THIS IMAGE, and that is the whole design: ggml-large-v3 (2.9 GB)
# is held in GPU memory by the whisper-server container, loaded once at its startup.
# This image is a few hundred MB of Python that forwards audio to it, so it rebuilds in
# seconds and can be scaled or restarted without touching the model.
#
# No CUDA base, no torch: this process never touches the GPU. GPU acceleration lives
# where the inference lives — in whisper-server, which is built with CUDA for six GPU
# generations (see ../whisper-service/Dockerfile).
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ffmpeg is REQUIRED, not optional. whisper-server decodes with miniaudio, which on this
# build handles WAV/MP3/FLAC and rejects m4a and webm/opus outright (measured) — i.e. the
# formats phones and browsers actually record. ffmpeg here is what makes those work.
# curl is for the HEALTHCHECK below.
#
# tesseract + its Hindi and English data are what make SCANNED PDFs work. Hindi
# government and legal documents are very often image-only scans, and without OCR those
# extract to zero characters. The language data is installed HERE, at build time, so
# inference stays air-gapped — nothing is downloaded on first use.
#
# libreoffice-writer is the only fully-offline converter that reads the legacy binary .doc
# format with Devanagari intact; antiword and catdoc both predate proper Unicode support
# and mangle it. It is the largest thing in this image by a wide margin, so if legacy .doc
# is not a required input, dropping this one package is the cheapest way to shrink the
# build (uploads of .doc then fail with a message saying exactly that).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        tesseract-ocr \
        tesseract-ocr-hin \
        tesseract-ocr-eng \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code edit does not re-run pip.
# NOTE: this is the ONLY step that needs a network, and it is BUILD time. Once the image
# exists, inference is fully air-gapped.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# consumer.py is the Kafka ingestion entrypoint; it shares this image and runs as its own
# compose service rather than inside the API process.
COPY main.py config.py constants.py consumer.py ./
COPY core/ ./core/
COPY utils/ ./utils/

EXPOSE 8000

# Probe "/" (a static route), NOT "/health" — the same lesson llama-service already learned.
# /health is a SYNC handler that round-trips to whisper AND to llama-service, whose own /health
# round-trips to the LLM. Each probe therefore parks a threadpool thread for up to 30s. Docker
# probes every 30s, so during a long job the probes pile up faster than they drain, exhaust the
# pool, and the API stops answering ANYTHING — measured 2026-09-10, unreachable for 54 minutes
# after a single 29-minute meeting. Container liveness means "is uvicorn serving", nothing more.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
