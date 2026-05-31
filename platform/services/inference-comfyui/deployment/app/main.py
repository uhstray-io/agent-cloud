"""
UhhCraft — Image Generation Service
Wraps ComfyUI to provide a simple REST API for sticker image generation.

Stack: FastAPI + httpx → ComfyUI WebSocket/REST API (running Flux.1 Schnell)

SETUP:
  1. Install ComfyUI and download Flux.1 Schnell (quantized for 12GB VRAM):
       https://github.com/comfyanonymous/ComfyUI
       Model: flux1-schnell-fp8.safetensors → ComfyUI/models/unet/
  2. pip install -r requirements.txt
  3. uvicorn main:app --host 0.0.0.0 --port 8188 --workers 1

ENV VARS:
  COMFYUI_URL   ComfyUI server URL  (default: http://localhost:8188)
  MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET
  SERVICE_PORT  Port for this FastAPI service (default: 8189)
"""

import asyncio
import json
import logging
import os
import uuid

import boto3
import httpx
import websockets
from botocore.client import Config
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Config ────────────────────────────────────────────────────────────────────

COMFYUI_URL   = os.getenv("COMFYUI_URL",   "http://localhost:8188")
COMFYUI_WS    = COMFYUI_URL.replace("http", "ws") + "/ws"
MINIO_ENDPOINT= os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_BUCKET  = os.getenv("MINIO_BUCKET", "generated-images")
MINIO_SSL     = os.getenv("MINIO_USE_SSL", "false").lower() == "true"

# Public, Caddy-routed prefix under which generated images are served to the
# browser. The Go app stores the returned URL, never the raw bucket key.
GENERATED_URL_PREFIX = os.getenv("GENERATED_URL_PREFIX", "/generated/img")


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"required environment variable {name} is not set")
    return val


# MinIO credentials are required — no insecure 'minioadmin' fallback that could
# silently reach a default-credential bucket in production.
MINIO_KEY    = _require("MINIO_ACCESS_KEY")
MINIO_SECRET = _require("MINIO_SECRET_KEY")

# ── S3 / MinIO client ─────────────────────────────────────────────────────────

s3 = boto3.client(
    "s3",
    endpoint_url=f"{'https' if MINIO_SSL else 'http'}://{MINIO_ENDPOINT}",
    aws_access_key_id=MINIO_KEY,
    aws_secret_access_key=MINIO_SECRET,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="UhhCraft Image Generation Service", version="1.0.0")
logger = logging.getLogger("inference-comfyui")


class GenerateRequest(BaseModel):
    generation_id: str = Field(..., description="UhhCraft generation UUID")
    prompt: str = Field(..., description="User's text prompt")
    width: int = Field(default=1024, ge=512, le=2048)
    height: int = Field(default=1024, ge=512, le=2048)
    steps: int = Field(default=4, ge=1, le=20)   # Flux Schnell: 4 steps is optimal
    guidance: float = Field(default=0.0)          # Flux Schnell: 0 guidance
    # Note: Flux Schnell runs at cfg=0 and ignores negative prompts, so this
    # API intentionally does not expose a negative_prompt parameter.


class GenerateResponse(BaseModel):
    generation_id: str
    url: str          # public, Caddy-routed URL of the stored PNG
    status: str = "completed"


@app.get("/health")
async def health():
    """Liveness — confirms the wrapper process is up. Cheap; no upstream calls."""
    return {"status": "ok"}


@app.get("/health/comfyui")
async def health_comfyui():
    """Verifies the wrapper can reach ComfyUI. Used by post-deploy.sh."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{COMFYUI_URL}/system_stats", timeout=5)
            r.raise_for_status()
        return {"comfyui_url": COMFYUI_URL, "reachable": True}
    except Exception:
        logger.exception("ComfyUI reachability check failed")
        return JSONResponse({"reachable": False, "error": "comfyui_unreachable"}, status_code=503)


@app.get("/health/models")
async def health_models():
    """Verifies the Flux.1 model is present in ComfyUI. Used by post-deploy.sh."""
    required = "flux1-schnell-fp8.safetensors"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{COMFYUI_URL}/object_info/UNETLoader", timeout=5)
            r.raise_for_status()
            info = r.json()
        names = (
            info.get("UNETLoader", {})
            .get("input", {})
            .get("required", {})
            .get("unet_name", [[]])[0]
        )
        if required in names:
            return {"models": [required], "missing": []}
        return JSONResponse({"missing": [required]}, status_code=503)
    except Exception:
        logger.exception("ComfyUI model check failed")
        return JSONResponse({"missing": [required], "error": "comfyui_unreachable"}, status_code=503)


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """
    Submit a sticker generation job to ComfyUI and wait for the result.
    Returns the MinIO path of the generated PNG.

    Route is /generate (not /generate/image) to match the sidecar
    contract documented in
    platform/services/inference-comfyui/context/architecture/contract.md
    and UhhCraft's internal/generation/ caller.
    """
    client_id = str(uuid.uuid4())
    workflow  = build_flux_workflow(req, client_id)

    # Submit workflow to ComfyUI. ComfyUI/MinIO outages are normalized to a
    # 503 with the machine-readable error contract the Go caller expects,
    # rather than leaking upstream internals.
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{COMFYUI_URL}/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=30,
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

        output_images = await wait_for_output(client_id, prompt_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("comfyui generation failed")
        return JSONResponse(
            {"error": "comfyui_unreachable", "detail": "image service upstream error"},
            status_code=503,
        )

    if not output_images:
        return JSONResponse(
            {"error": "internal_error", "detail": "no images returned"},
            status_code=503,
        )

    # Upload first image to this service's own MinIO.
    img_data, _ = output_images[0]
    minio_key = f"generations/{req.generation_id}/output.png"
    try:
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=minio_key,
            Body=img_data,
            ContentType="image/png",
        )
    except Exception:
        logger.exception("minio upload failed")
        return JSONResponse(
            {"error": "internal_error", "detail": "asset storage error"},
            status_code=503,
        )

    # Return the public, Caddy-routed URL — not the raw bucket key.
    return GenerateResponse(
        generation_id=req.generation_id,
        url=f"{GENERATED_URL_PREFIX}/{minio_key}",
    )


async def wait_for_output(client_id: str, prompt_id: str, timeout: float = 120.0):
    """
    Listens on the ComfyUI WebSocket until the prompt is complete,
    then fetches the output image(s) via REST.
    """
    deadline = asyncio.get_event_loop().time() + timeout

    async with websockets.connect(f"{COMFYUI_WS}?clientId={client_id}") as ws:
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except TimeoutError:
                continue

            msg = json.loads(raw) if isinstance(raw, str) else {}
            if (
                msg.get("type") == "executing"
                and msg.get("data", {}).get("node") is None
                and msg["data"].get("prompt_id") == prompt_id
            ):
                break  # execution complete
        else:
            raise HTTPException(504, "Generation timed out")

    # Fetch output images
    async with httpx.AsyncClient() as client:
        hist = await client.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=15)
        hist.raise_for_status()
        history = hist.json()

    outputs = history.get(prompt_id, {}).get("outputs", {})
    images  = []
    for node_output in outputs.values():
        for img_info in node_output.get("images", []):
            img_url = (
                f"{COMFYUI_URL}/view"
                f"?filename={img_info['filename']}"
                f"&subfolder={img_info.get('subfolder', '')}"
                f"&type={img_info.get('type', 'output')}"
            )
            async with httpx.AsyncClient() as client:
                r = await client.get(img_url, timeout=30)
                r.raise_for_status()
            images.append((r.content, img_info["filename"]))

    return images


def build_flux_workflow(req: GenerateRequest, client_id: str) -> dict:
    """
    Builds a minimal ComfyUI workflow JSON for Flux.1 Schnell (fp8 quantized).
    Node IDs are arbitrary stable strings.
    Adjust model filenames to match what's installed in ComfyUI/models/unet/.
    """
    return {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "flux1-schnell-fp8.safetensors",
                "weight_dtype": "fp8_e4m3fn",
            },
        },
        "5": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
                "clip_name2": "clip_l.safetensors",
                "type": "flux",
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["5", 0],
                "text": req.prompt,
            },
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": req.width,
                "height": req.height,
                "batch_size": 1,
            },
        },
        "8": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "ae.safetensors"},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["6", 0],  # Flux Schnell: negative is same as positive
                "latent_image": ["7", 0],
                "seed": -1,
                "steps": req.steps,
                "cfg": req.guidance,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["9", 0], "vae": ["8", 0]},
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["10", 0],
                "filename_prefix": f"uhhcraft_{req.generation_id}",
            },
        },
    }
