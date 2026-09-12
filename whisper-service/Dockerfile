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
# BASE IMAGE CHOICE
# nvcr.io/nvidia/vllm:26.04-py3 is used for BOTH stages because it is already on this machine
# (vllm-local runs from it) and already carries nvcc 13.2 plus the matching cuBLAS. So this
# builds with no registry pull at all — which matters on a network that filters package
# downloads — and because the runtime stage shares every layer with an image that is already
# present, the real added disk cost is the ~4 MB of binaries, not the base's size.
#
# THE MODEL IS NOT IN THE IMAGE. ggml-large-v3.bin is 2.9 GB and stays a mounted volume, the
# same split already used for vLLM and Chatterbox: images carry code, volumes carry weights.

ARG BASE_IMAGE=nvcr.io/nvidia/vllm:26.04-py3

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
FROM ${BASE_IMAGE}

# Only the server and the shared libraries it links against. Flattened into one directory so
# the rpath games the hand build needed (mounting the repo at an identical path so its
# baked-in rpath resolved) are gone — LD_LIBRARY_PATH below is explicit and self-contained.
COPY --from=builder /src/build/bin/whisper-server            /opt/whisper/bin/
COPY --from=builder /src/build/src/libwhisper.so*            /opt/whisper/lib/
COPY --from=builder /src/build/ggml/src/libggml*.so*         /opt/whisper/lib/
COPY --from=builder /src/build/ggml/src/ggml-cuda/libggml-cuda.so* /opt/whisper/lib/

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
