# whisper.cpp server — English ASR for the offline pipeline, built from source.
#
# WHY THIS EXISTS
# The whisper-server container used to run a binary compiled BY HAND on the GB10 and
# bind-mounted in from /home/admin/whisper.cpp. Nothing was in the image, so whisper only
# worked on that one machine: it could not move to a second box, could not run on OpenShift,
# and could not ship to a customer. Every other service was already a real image; this was
# the one that was not.
#
# WHAT WAS WRONG WITH THE HAND BUILD (from its CMakeCache):
#   CMAKE_CUDA_ARCHITECTURES=121  GPU kernels for GB10 Blackwell ONLY. An A100 (sm_80) or
#                                 H100 (sm_90) would find no code to run.
#   GGML_NATIVE=ON                -march=native, tuned to that exact CPU. A different CPU can
#                                 fault on an illegal instruction.
# Both are fixed below. Copying the prebuilt binary into an image would have preserved both,
# which is why that shortcut was rejected — it looks like portability without being it.
#
# BASE IMAGE CHOICE — TWO DIFFERENT BASES, DELIBERATELY
# BUILD on nvcr.io/nvidia/vllm:26.04-py3: it is already on the GB10, and carries nvcc 13.2 with the
# matching cuBLAS, so the compile needs no registry pull on a filtered network.
# RUN on plain ubuntu:24.04. Building and running on the vLLM base produced a 19.8 GB image, 19.1 GB
# of which is Python, PyTorch and training tooling whisper-server never loads — and it had to be
# shipped to the customer through a pipeline that has no job for it. whisper-server is one C++
# binary needing three CUDA libraries, which are copied from the build stage so they are exactly the
# ones it was compiled against. Result measured 2026-09-12: 1.03 GB, byte-identical transcripts.
# Ubuntu 24.04 is not a free choice — it is the build base's own release (glibc 2.39).
#
# ONE MEASURED TRADE: the vLLM base also carries a newer forward-compatibility GPU driver, which the
# container runtime used in place of the node's. With it a 9-minute meeting took 35 s, without it
# 41 s, same text. It is left out: 6 s on a job that takes minutes is not worth carrying 396 MB and
# risking a node whose own driver is newer than the copy.
#
# THE MODEL IS NOT IN THE IMAGE. ggml-large-v3.bin is 2.9 GB and stays a mounted volume, the
# same split already used for vLLM and Chatterbox: images carry code, volumes carry weights.

ARG BASE_IMAGE=nvcr.io/nvidia/vllm:26.04-py3
ARG RUNTIME_BASE=ubuntu:24.04

# ── build stage ──────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS builder

ARG WHISPER_VERSION=v1.8.4

# Every GPU generation the binary should run on. Each one is compiled separately, so this is
# what makes the build take tens of minutes rather than a few — it buys portability with time.
#   80  A100          86  A10/A40/3090     89  L4/L40S/Ada
#   90  H100/H200     120 RTX 50xx         121 GB10 / DGX Spark (this machine)
ARG CUDA_ARCHS="80;86;89;90;120;121"

RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Shallow clone of an immutable TAG, not a branch — two builds a month apart produce the same
# software. v1.8.4 is the exact version the hand build used (commit 9386f23).
RUN git clone --depth 1 --branch ${WHISPER_VERSION} https://github.com/ggml-org/whisper.cpp .

RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS}" \
        -DGGML_NATIVE=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_EXAMPLES=ON \
    && cmake --build build --config Release -j"$(nproc)" --target whisper-server

# ── runtime stage ────────────────────────────────────────────────────────────
FROM ${RUNTIME_BASE}

# libgomp is whisper.cpp's only system dependency beyond libc/libstdc++.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# cp -a inside one RUN, not COPY, for two reasons: COPY dereferences symlinks, so the old image held
# three identical 239 MB copies of libggml-cuda.so and would hold two 607 MB copies of libcublasLt;
# and a bind mount adds no layer. LD_LIBRARY_PATH below is explicit, so no rpath games are needed.
# libcuda.so.1 is absent on purpose: the NVIDIA container runtime injects the node's driver.
RUN --mount=type=bind,from=builder,source=/src/build,target=/wb \
    --mount=type=bind,from=builder,source=/usr/local/cuda/lib64,target=/wc \
    set -e; mkdir -p /opt/whisper/bin /opt/whisper/lib /usr/local/cuda/lib64; \
    cp -a /wb/bin/whisper-server /opt/whisper/bin/; \
    cp -a /wb/src/libwhisper.so* /opt/whisper/lib/; \
    cp -a /wb/ggml/src/libggml*.so* /opt/whisper/lib/; \
    cp -a /wb/ggml/src/ggml-cuda/libggml-cuda.so* /opt/whisper/lib/; \
    cp -a /wc/libcudart.so.13* /wc/libcublas.so.13* /wc/libcublasLt.so.13* /usr/local/cuda/lib64/; \
    MISSING=$(LD_LIBRARY_PATH=/opt/whisper/lib:/usr/local/cuda/lib64 ldd /opt/whisper/bin/whisper-server \
        | grep -i "not found" | grep -v "libcuda.so.1" || true); \
    [ -z "$MISSING" ] || { echo "whisper-server is missing libraries on this base: $MISSING"; exit 1; }; \
    echo "whisper-server links cleanly (libcuda.so.1 comes from the node's driver at runtime)"

ENV LD_LIBRARY_PATH=/opt/whisper/lib:/usr/local/cuda/lib64
ENV PATH=/opt/whisper/bin:$PATH

# Where the mounted model is expected. Overridable so a customer can mount elsewhere.
ENV WHISPER_MODEL=/models/ggml-large-v3.bin
ENV WHISPER_VAD_MODEL=/models/ggml-silero-v6.2.0.bin

EXPOSE 8080

# -l auto: detect the language rather than forcing one. Forcing "hi" on English audio was
# measured to transliterate it into Devanagari and trigger hallucination loops, so auto is
# load-bearing, not a default.
# -mc 0: DO NOT carry text context between decodes. Load-bearing, measured 2026-09-12 on this
# machine: with the default (-1, unlimited) the server's FIRST request is clean and every request
# after it degenerates into a repetition loop — the same sentence 296, 248 and 140 times on three
# consecutive runs of one 9-minute meeting. The pipeline always sends a short language-detect clip
# first, so in production every real transcription was one of the degraded ones. With -mc 0 the
# same audio returns byte-identical text every time, no loops, in half the time (34 s vs 70-92 s),
# and it recovered a passage the old setting dropped. -mc 64 was also tried and is far worse: it
# returned a quarter of the meeting.
CMD ["sh", "-c", "exec whisper-server \
  -m ${WHISPER_MODEL} \
  -vm ${WHISPER_VAD_MODEL} \
  --host 0.0.0.0 --port 8080 \
  --inference-path /inference \
  -l auto -t ${WHISPER_THREADS:-8} \
  -mc ${WHISPER_MAX_CONTEXT:-0}"]
