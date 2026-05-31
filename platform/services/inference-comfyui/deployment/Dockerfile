# syntax=docker/dockerfile:1.7
# inference-comfyui — FastAPI wrapper around ComfyUI (Flux.1 Schnell).
#
# ComfyUI itself is expected to run on the same host (and same GPU)
# under a separate systemd unit or container. This image is only the
# FastAPI wrapper that talks to ComfyUI over its local HTTP/WS API.
#
# Production runs on an Nvidia GPU VM with PCIe passthrough. The CUDA
# runtime base is pinned to match the host's driver track.

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3.11 python3.11-venv \
      curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
 && python3.11 -m ensurepip --upgrade

WORKDIR /app

# Dependencies first for layer caching. Install with python3.11's own pip so
# the wrapper and its deps share one interpreter (the apt python3-pip targets
# the distro's default python3.10).
COPY app/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# App source.
COPY app/ /app/

# Non-root.
RUN useradd --create-home --uid 10001 inference \
 && chown -R inference:inference /app
USER inference

EXPOSE 8189

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8189/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8189", "--workers", "1"]
