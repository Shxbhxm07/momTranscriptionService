# Speaker diarization + speaker identification service (NeMo TitaNet + ClusteringDiarizer).
#
# ONE Dockerfile for every target. The base image is a build argument, so the same file
# builds for the GB10 (arm64/Blackwell, offline), OCP (x86_64 GPU), or a cloud/CPU build:
#
#   docker build --build-arg BASE_IMAGE=<image> -t mom-ai-nemo-service ./nemo-service
#
# The base MUST supply a PyTorch built for the target GPU. We deliberately never install
# torch from PyPI (see requirements.txt) — that would replace the vendor build and lose
# GPU support.
#
# Replaces the previous Dockerfile, which `git clone`d NeMo's main branch at build time
# (unreproducible + required internet, so it could never build air-gapped) and installed
# NeMo's entire `[all]` extra just to use two ASR classes.
ARG BASE_IMAGE=nvcr.io/nvidia/nemo:26.04
FROM ${BASE_IMAGE}

WORKDIR /app

# Audio I/O. ffmpeg is genuinely absent from the NeMo base (pydub warns on import) and is
# required by main.py's conversion step; libsndfile1 backs soundfile; sox is used for
# resampling. No `|| true` here — if these fail the image is broken and should fail loudly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

# Dependencies before app code, so editing the service doesn't re-run the slow install.
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

# compose bind-mounts ./nemo-service:/app at runtime; this COPY keeps the image usable standalone.
COPY . /app

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
