# llama-service is a pure FastAPI/httpx PROXY to the Groq API — no local GPU/PyTorch needed.
# python:3.11-slim (~50MB) pulls fast and reliably; the old nvcr.io NVIDIA PyTorch base (~20GB)
# caused flaky "connection reset" pulls in CI.
FROM python:3.11-slim

WORKDIR /app

# Full English wordlist (104,334 words) at /usr/share/dict/american-english.
#
# This is the term-corrector's OVER-CORRECTION GUARD, and it must be IN THE IMAGE. It used to come
# only from a docker-compose bind mount, which meant it existed on the GB10 and nowhere else — so on
# OCP the guard fell back to the 293-word list in lexicon/common_words.txt and the conservative
# phonetic matcher stopped being conservative.
#
# Measured against the real lexicon: with the mount, 0 of 72,255 English words are altered. Without
# it, 102 ordinary words get rewritten into product names — "coherent" -> "Qdrant", "catholic" ->
# "Docutalk", "derision" -> "diarization". Those are words that appear in real meetings, and the
# corruption lands in the transcript AND the minutes with nothing logged.
RUN apt-get update && apt-get install -y --no-install-recommends wamerican \
    && rm -rf /var/lib/apt/lists/* \
    && test -s /usr/share/dict/american-english

# Install dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
    httpx \
    fastapi \
    uvicorn[standard] \
    python-multipart
COPY . /app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
