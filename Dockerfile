###############################################################################
# Builder stage — resolve & install dependencies into a self-contained venv.
#
# We use the official uv image which bundles `uv` and a managed CPython.
# Torch is forced to the CPU backend here so the image stays lean (the default
# resolution in uv.lock pulls the multi-GB CUDA wheels, which are useless on
# CPU-only Azure Container Instances). Local/GPU installs are unaffected — they
# keep using the lockfile's default (CUDA) resolution.
###############################################################################
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# uv configuration:
#   - compile bytecode for faster cold starts
#   - copy (not symlink) packages so the venv is relocatable to the final stage
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached layer) using only the lock + manifest, so
# changes to source code don't bust the heavy dependency layer.
#
# NOTE: uv.lock pins the CUDA torch build (the correct default for local/GPU
# installs). We honor the lockfile here, then swap torch to the CPU build as the
# final builder step. We do NOT edit pyproject.toml/uv.lock, so a plain
# `uv sync` on a GPU host still gets CUDA support automatically.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy the project source and install the project itself.
# `debug/` is declared as a hatchling wheel package target in pyproject.toml, so
# it must be present for the build to succeed (it is NOT carried into runtime).
COPY src ./src
COPY debug ./debug
RUN uv sync --frozen --no-dev

# FINAL builder step — replace the CUDA torch with the CPU build and prune the
# now-orphaned NVIDIA/triton wheels. This runs last so no subsequent `uv sync`
# can reinstate the CUDA build. `--torch-backend=cpu` is only available via the
# `uv pip` interface and pulls torch from the PyTorch CPU index. This shrinks
# the image by several GB; CUDA is useless on CPU-only Azure Container Instances.
RUN uv pip install --reinstall-package torch --torch-backend=cpu "torch==2.12.0+cpu" \
    && uv pip uninstall \
        triton \
        nvidia-cublas nvidia-cuda-cupti nvidia-cuda-nvrtc nvidia-cuda-runtime \
        nvidia-cudnn-cu13 nvidia-cufft nvidia-cufile nvidia-curand \
        nvidia-cusolver nvidia-cusparse nvidia-cusparselt-cu13 \
        nvidia-nccl-cu13 nvidia-nvjitlink nvidia-nvshmem-cu13 nvidia-nvtx

# Pre-download the Surya detection model weights so they are baked into the
# image. Without this, every cold-started container would fetch the weights from
# datalab's model host on first use (slow startup, network-dependent — bad for
# ACI). Surya caches under its MODEL_CACHE_DIR setting (env var: MODEL_CACHE_DIR,
# no prefix); this path must match the runtime stage's value so the predictor
# finds the baked-in weights instead of re-downloading.
ENV MODEL_CACHE_DIR=/opt/surya-models
RUN .venv/bin/python -c "from surya.detection import DetectionPredictor; DetectionPredictor()"

###############################################################################
# Runtime stage — minimal Debian slim with only the OS libraries our deps need.
#
#   tesseract-ocr / tesseract-ocr-osd : pytesseract OSD rotation detection
#   libglib2.0-0                      : opencv-python-headless (surya) runtime
#
# pymupdf, boto3 and torch (CPU) ship self-contained wheels and need no extra
# system packages.
###############################################################################
FROM python:3.13-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-osd \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user to run the worker.
RUN useradd --create-home --uid 1000 worker

WORKDIR /app

# Copy the fully-resolved virtual environment and the application source.
COPY --from=builder --chown=worker:worker /app/.venv /app/.venv
COPY --chown=worker:worker src ./src

# Copy the pre-downloaded Surya model weights baked in the builder stage.
# This makes the container fully self-contained — no model download at startup.
COPY --from=builder --chown=worker:worker /opt/surya-models /opt/surya-models

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Point Surya at the baked-in model weights. Must match the builder's value.
ENV MODEL_CACHE_DIR=/opt/surya-models

USER worker

# All configuration is supplied via environment variables at `docker run` time
# (see .env.example). The container is otherwise self-contained.
ENTRYPOINT ["transcriptor-worker"]
