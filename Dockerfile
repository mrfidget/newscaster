# =============================================================================
# Stage 1 — builder
# Installs build tools, compiles llama-cpp-python from source (CPU-only),
# and collects all Python dependencies into /opt/venv.
# Nothing from this stage leaks into the final image.
# =============================================================================
FROM python:3.12-slim AS builder

# Build tools needed to compile llama-cpp-python from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so we can copy it cleanly to the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# llama-cpp-python must be built CPU-only.
# We deliberately omit AVX2/AVX-512 flags so the image runs on any x86_64
# host (including the target Lenovo IdeaPad S340).
# -DGGML_AVX=OFF  — disable AVX entirely for maximum portability
# -DGGML_AVX2=OFF — same
# The model will use the generic GGML backend; still fast enough for a 1.5B
# GGUF on a modern CPU at Q4_K_M.
ENV CMAKE_ARGS="-DLLAMA_CUBLAS=OFF -DLLAMA_METAL=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF"
ENV FORCE_CMAKE=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt


# =============================================================================
# Stage 2 — runtime
# Starts from a clean slim image.  Only the venv, piper, and application
# source are copied in — no compilers, no cmake, no build headers.
# =============================================================================
FROM python:3.12-slim AS runtime

# Runtime-only system packages:
#   ffmpeg  — required by pydub for WAV → MP3 conversion
#   curl    — used by Makefile download targets (not needed inside container
#             at runtime, but harmless and keeps make targets working when
#             exec-ing into the container)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built Python venv from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Install piper TTS from the official GitHub release tarball.
#
# The tarball bundles the piper binary AND its shared libraries
# (libpiper_phonemize.so.1, libespeak-ng.so.1, libonnxruntime.so, etc.)
# We extract into /opt/piper and symlink the binary onto PATH.
# LD_LIBRARY_PATH tells the binary where to find its bundled .so files.
# ---------------------------------------------------------------------------
ARG PIPER_VERSION=2023.11.14-2
ARG PIPER_ARCH=x86_64

# curl is needed here for the download; install temporarily then clean up
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && mkdir -p /opt/piper \
 && curl -fsSL \
    "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_${PIPER_ARCH}.tar.gz" \
    | tar -xz -C /opt/piper --strip-components=1 \
 && ln -s /opt/piper/piper /usr/local/bin/piper \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

ENV LD_LIBRARY_PATH=/opt/piper:${LD_LIBRARY_PATH}

# ---------------------------------------------------------------------------
# Application source
# ---------------------------------------------------------------------------
WORKDIR /app
COPY src/ ./src/
COPY scripts/ ./scripts/

CMD ["python", "-m", "src.main"]