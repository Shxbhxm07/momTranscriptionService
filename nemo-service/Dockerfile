# Speaker diarization service (NeMo TitaNet + ClusteringDiarizer).
#
# ONE Dockerfile for every target; the base image is a build argument:
#   docker build --build-arg BASE_IMAGE=<image> -t offline-mom-nemo ./nemo-service
#
# The base MUST supply a PyTorch built for the target GPU. torch is never installed from PyPI
# (see requirements.txt) — that would replace the vendor build and lose GPU support.
#
# WHY NOT A SMALLER BASE (tried 2026-09-11). NVIDIA's NeMo image is 47 GB, 46 GB of it LLM-training
# tooling this service never touches, so the vLLM base Whisper already uses (19 GB) was tried. It
# built and started, but diarization failed on this GPU three ways in succession: a torchaudio built
# for a different CUDA, a 13 KB STUB cuFFT (vLLM never computes an FFT), and cuDNN finding no
# convolution engine for sm_121. The first two are handled below and are no-ops on the NeMo base;
# the third is a GPU-support gap in that torch build. NVIDIA's PyTorch image (same torch build as
# the NeMo base, full CUDA libraries) is the better route to a smaller image — untested so far.
ARG BASE_IMAGE=nvcr.io/nvidia/nemo:26.04
FROM ${BASE_IMAGE}

WORKDIR /app

# Audio I/O: ffmpeg for main.py's conversion step, libsndfile1 for soundfile, sox for resampling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

# EVERYTHING BELOW IS ONE LAYER, deliberately. The NeMo base already has 120 layers and Docker's
# overlay storage refuses to start a container past ~125 ("max depth exceeded"), so each guard
# cannot have its own RUN. Measured: split into four RUNs the image built fine and would not start.
RUN set -e; \
    python3 -c "import torch; print(torch.__version__)" > /tmp/torch_before; \
    python3 -m pip install --no-cache-dir -r /app/requirements.txt; \
    # GUARD — pip must not replace the vendor torch: a PyPI torch lacks this GPU's kernels and
    # diarization silently drops to CPU (measured: 34 s becomes well over 5 minutes per meeting).
    python3 -c "import torch, sys; b = open('/tmp/torch_before').read().strip(); \
sys.exit(0 if torch.__version__ == b else f'torch was replaced by pip: {b} -> {torch.__version__}')"; \
    # torchaudio RAISES on import if built for a different CUDA than torch, and NeMo imports it for
    # a metric diarization never uses. Absent is fine — the NeMo base has none. No-op there.
    python3 -m pip uninstall -y torchaudio >/dev/null 2>&1 || true; \
    # Replace a STUB cuFFT with NVIDIA's real build — only if the installed one is a stub (< 1 MB).
    # No-op on the NeMo base, which ships the real library.
    CUFFT=/usr/local/cuda/lib64/libcufft.so.12; \
    if [ -e "$CUFFT" ] && [ "$(stat -Lc %s "$CUFFT")" -lt 1000000 ]; then \
        python3 -m pip install --no-cache-dir "nvidia-cufft==12.2.*"; \
        REAL=$(find /usr/local/lib/python3*/dist-packages/nvidia -name libcufft.so.12 -size +1M | head -1); \
        test -n "$REAL"; \
        for lib in libcufft.so.12 libcufftw.so.12; do \
            [ -f "$(dirname "$REAL")/$lib" ] && cp -f "$(dirname "$REAL")/$lib" "$(readlink -f /usr/local/cuda/lib64/$lib)"; \
        done; \
        echo "replaced stub cuFFT with $REAL"; \
    fi; \
    # Import exactly what the service uses. These are lazy at runtime, so a broken image would only
    # fail on its first real meeting; forcing them here makes it fail to BUILD instead.
    python3 -c "from nemo.collections.asr.models import EncDecSpeakerLabelModel; \
from nemo.collections.asr.models.clustering_diarizer import ClusteringDiarizer; \
import nemo, torch; assert torch.version.cuda, 'CPU-only torch'; \
print('nemo', nemo.__version__, '| torch', torch.__version__, '| cuda', torch.version.cuda)"

COPY . /app

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
