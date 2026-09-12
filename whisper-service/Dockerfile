# whisper.cpp server WITHOUT a GPU — THE DEFAULT BUILD, for clusters that have none.
# The GPU build is Dockerfile.gpu, and is what the GB10 compose file uses.
#
# WHY THIS EXISTS: the GPU image needs a GPU at runtime, and on a node without one the container
# dies with "libcuda.so.1: cannot open shared object file". The client's test cluster has no GPU,
# so speech there either runs on a remote GPU box or on the processor. This is the processor build.
#
# Measured on the GB10 (20 cores, arm64), 2026-09-12:
#   build      1 min 49 s from scratch, 458 MB of disk, ~250 MB of memory, no NVIDIA download
#   image      107 MB
#   transcribe 7.6 min for a 9.3-minute meeting on 8 threads, 6.5 min on 16 — same text each run
# The GPU build, for contrast: ~40 GB of disk (NVIDIA's 19 GB compiler image), and 40 s per meeting.
#
# Accuracy is the same model file and the same decoding settings; only the arithmetic backend
# differs, so transcripts differ slightly from the GPU's (2141 vs 2164 words on that meeting).
#
#   docker build -f whisper-service/Dockerfile.cpu -t offline-mom-whisper:cpu ./whisper-service
ARG BASE=ubuntu:24.04

FROM ${BASE} AS builder
ARG WHISPER_VERSION=v1.8.4
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${WHISPER_VERSION} https://github.com/ggml-org/whisper.cpp .
# GGML_NATIVE=OFF: build for a generic processor, not the one that happens to compile it. Without
# it the binary can fault with an illegal instruction on a different node.
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
        -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON \
    && cmake --build build --config Release -j"$(nproc)" --target whisper-server

FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# cp -a in one RUN, not COPY: COPY dereferences symlinks and would triple the library files.
RUN --mount=type=bind,from=builder,source=/src/build,target=/wb \
    set -e; mkdir -p /opt/whisper/bin /opt/whisper/lib; \
    cp -a /wb/bin/whisper-server /opt/whisper/bin/; \
    cp -a /wb/src/libwhisper.so* /opt/whisper/lib/; \
    cp -a /wb/ggml/src/libggml*.so* /opt/whisper/lib/

ENV LD_LIBRARY_PATH=/opt/whisper/lib
ENV PATH=/opt/whisper/bin:$PATH
ENV WHISPER_MODEL=/models/ggml-large-v3.bin
ENV WHISPER_VAD_MODEL=/models/ggml-silero-v6.2.0.bin

EXPOSE 8080
# Threads matter here in a way they never did on the GPU: 8 threads took 7.6 min for a 9.3-minute
# meeting, 16 took 6.5. Give the pod the cores to match whatever this is set to.
CMD ["sh", "-c", "exec whisper-server \
  -m ${WHISPER_MODEL} \
  -vm ${WHISPER_VAD_MODEL} \
  --host 0.0.0.0 --port 8080 \
  --inference-path /inference \
  -l auto -t ${WHISPER_THREADS:-8}"]
