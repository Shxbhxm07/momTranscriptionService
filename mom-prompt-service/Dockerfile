# Minutes of Meeting from a user's prompt and, optionally, a document. No audio, no speech model:
# the minutes are written by llama-service (watsonx on the test cluster), reached over HTTP.
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# tesseract + Hindi and English data: scanned PDFs have no text layer, and without OCR they read
# as empty. libreoffice-writer: reads legacy .doc, and counts the laid-out pages of the JSSD minutes
# (a CONFIDENTIAL document states its page count on page 1). curl: the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tesseract-ocr \
        tesseract-ocr-hin \
        tesseract-ocr-eng \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py job.py consumer.py ./
COPY core/ ./core/
COPY utils/ ./utils/

EXPOSE 8000

# "/" is static; /health calls llama-service and must not be the liveness probe (see ../api/Dockerfile).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
