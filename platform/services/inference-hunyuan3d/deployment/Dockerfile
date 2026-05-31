# syntax=docker/dockerfile:1.7
# inference-hunyuan3d — FastAPI wrapper around Hunyuan3D-2-mini.
#
# Unlike inference-comfyui (which talks to a separate ComfyUI process),
# Hunyuan3D runs in-process via the diffusers/transformers pipeline. The
# image therefore needs the full torch + CUDA stack.
#
# Production runs on an Nvidia GPU VM with PCIe passthrough; weights are
# expected to be mounted at /weights from a host volume (downloaded once,
# reused across deploys).

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/weights/.huggingface \
    TORCH_HOME=/weights/.torch

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3.11 python3.11-venv \
      git curl ca-certificates \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
 && python3.11 -m ensurepip --upgrade

WORKDIR /app

# Install everything with python3.11's own pip (the apt python3-pip targets the
# distro's default python3.10 — a mismatch with the symlinked interpreter).

# torch first (large, sticky in cache).
RUN python -m pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cu124 \
      torch==2.4.1

# App dependencies.
COPY app/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Hunyuan3D from source (the upstream is not on PyPI as of 2026-05).
# Pin to a known-good commit; update deliberately.
ARG HUNYUAN3D_REF=v2.0
RUN git clone --depth 1 --branch ${HUNYUAN3D_REF} \
      https://github.com/Tencent/Hunyuan3D-2.git /opt/Hunyuan3D-2 \
 && python -m pip install --no-cache-dir -e /opt/Hunyuan3D-2

COPY app/ /app/

RUN useradd --create-home --uid 10001 inference \
 && mkdir -p /weights \
 && chown -R inference:inference /app /weights /opt/Hunyuan3D-2
USER inference

EXPOSE 8001

# start-period covers the slow first-request weight load into VRAM (~60-90s).
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
