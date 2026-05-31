"""
UhhCraft — 3D Model Generation Service
Wraps Hunyuan3D-2 to generate 3D meshes from text prompts.

Outputs:
  GLB  — for Three.js preview in the browser
  STL  — manufacturing asset sent to the 3D printer or Hubs/Shapeways

SETUP:
  1. Clone and install Hunyuan3D-2:
       https://github.com/Tencent/Hunyuan3D-2
       pip install -e .
  2. Download model weights (see Hunyuan3D-2 README for HuggingFace links)
  3. pip install -r requirements.txt
  4. uvicorn main:app --host 0.0.0.0 --port 8001 --workers 1

NOTE: 3D generation requires significant VRAM. Hunyuan3D-2-lite runs on 12GB
      (RTX 5070). Use the 'lite' variant: tencent/Hunyuan3D-2-mini

ENV VARS:
  MODEL_PATH    Path to Hunyuan3D model weights (default: ./weights/hunyuan3d-2-mini)
  DEVICE        cuda (default) or cpu (slow, dev-only)
  MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import boto3
from botocore.client import Config
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Config ────────────────────────────────────────────────────────────────────

MODEL_PATH    = os.getenv("MODEL_PATH",    "./weights/hunyuan3d-2-mini")
DEVICE        = os.getenv("DEVICE",        "cuda")
MINIO_ENDPOINT= os.getenv("MINIO_ENDPOINT","localhost:9000")
MINIO_BUCKET  = os.getenv("MINIO_BUCKET",  "generated-3d")
MINIO_SSL     = os.getenv("MINIO_USE_SSL", "false").lower() == "true"

# Public, Caddy-routed prefix under which generated 3D assets are served.
GENERATED_URL_PREFIX = os.getenv("GENERATED_URL_PREFIX", "/generated/3d")


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"required environment variable {name} is not set")
    return val


# MinIO credentials are required — no insecure 'minioadmin' fallback.
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

# ── Model loading (lazy — loaded on first request) ────────────────────────────

_pipeline = None

def get_pipeline():
    global _pipeline
    if _pipeline is None:
        try:
            # Import here so the service can start even if Hunyuan3D isn't installed
            from hy3dgen.text2shape import Text2ShapePipeline  # Hunyuan3D-2 API
            _pipeline = Text2ShapePipeline.from_pretrained(
                MODEL_PATH,
                device=DEVICE,
            )
        except ImportError as e:
            raise RuntimeError(
                "Hunyuan3D not installed. "
                "See https://github.com/Tencent/Hunyuan3D-2 for setup instructions."
            ) from e
    return _pipeline

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="UhhCraft 3D Model Generation Service", version="1.0.0")
logger = logging.getLogger("inference-hunyuan3d")

# Single in-flight generation per GPU. The wrapper does not queue — concurrent
# callers are rejected with 503 (the Go caller serializes via River).
_gen_lock = asyncio.Semaphore(1)


class GenerateRequest(BaseModel):
    generation_id: str = Field(..., description="UhhCraft generation UUID")
    prompt: str = Field(..., description="User's text prompt")
    steps: int = Field(default=30, ge=10, le=60)
    guidance: float = Field(default=7.5, ge=1.0, le=15.0)
    octree_resolution: int = Field(default=256, ge=128, le=512)  # higher = more detail, slower


class GenerateResponse(BaseModel):
    generation_id: str
    glb_url: str      # public, Caddy-routed URL — Three.js preview
    stl_url: str      # public, Caddy-routed URL — manufacturing
    status: str = "completed"


@app.get("/health")
async def health():
    """Liveness — confirms the wrapper process is up. Cheap; no inference."""
    return {"status": "ok"}


@app.get("/health/weights")
async def health_weights():
    """Verifies the model weights are present on disk. Cheap — does not load
    the model into VRAM (loading is lazy on first generation)."""
    if Path(MODEL_PATH).exists():
        return {"model_path": MODEL_PATH, "present": True, "loaded": _pipeline is not None}
    return JSONResponse({"present": False, "error": "weights_missing"}, status_code=503)


@app.get("/health/gpu")
async def health_gpu():
    """Verifies CUDA visibility + free VRAM. Cheap — no inference."""
    try:
        import torch
        if not torch.cuda.is_available():
            return JSONResponse({"cuda_available": False, "error": "gpu_unavailable"}, status_code=503)
        free, _ = torch.cuda.mem_get_info()
        return {
            "cuda_available": True,
            "device_count": torch.cuda.device_count(),
            "device_name": torch.cuda.get_device_name(0),
            "vram_free_mb": free // (1024 * 1024),
        }
    except Exception:
        logger.exception("gpu health check failed")
        return JSONResponse({"cuda_available": False, "error": "gpu_unavailable"}, status_code=503)


def _run_generation(req: "GenerateRequest") -> tuple[str, str]:
    """Blocking: run the model and upload GLB+STL. Executed in a worker thread
    so it never blocks the FastAPI event loop. Returns (glb_key, stl_key)."""
    pipeline = get_pipeline()
    result = pipeline(
        prompt=req.prompt,
        num_inference_steps=req.steps,
        guidance_scale=req.guidance,
        octree_resolution=req.octree_resolution,
    )
    mesh = result.mesh  # trimesh.Trimesh or compatible
    if mesh is None:
        raise ValueError("model returned no mesh")

    glb_key = f"generations/{req.generation_id}/model.glb"
    stl_key = f"generations/{req.generation_id}/model.stl"

    for key, ftype, ctype in (
        (glb_key, "glb", "model/gltf-binary"),
        (stl_key, "stl", "model/stl"),
    ):
        with tempfile.NamedTemporaryFile(suffix="." + ftype, delete=False) as f:
            mesh.export(f.name, file_type=ftype)
            with open(f.name, "rb") as fh:
                s3.put_object(Bucket=MINIO_BUCKET, Key=key, Body=fh.read(), ContentType=ctype)
            Path(f.name).unlink(missing_ok=True)

    return glb_key, stl_key


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """
    Generate a 3D mesh from a text prompt. Returns public Caddy-routed URLs for
    both GLB (browser preview) and STL (manufacturing).

    Route is /generate (not /generate/3d) to match the sidecar contract.
    Failures map to the degraded-response error contract the Go caller expects.
    """
    if _gen_lock.locked():
        return JSONResponse(
            {"error": "vram_exhausted", "detail": "a generation is already in progress"},
            status_code=503,
        )

    async with _gen_lock:
        try:
            # Run the synchronous, GPU-bound pipeline + uploads off the event loop.
            glb_key, stl_key = await asyncio.to_thread(_run_generation, req)
        except RuntimeError:
            # get_pipeline() raises RuntimeError when Hunyuan3D isn't installed.
            # Full traceback is logged; the response stays generic so internal
            # import paths / module names aren't exposed to callers.
            logger.exception("3d pipeline unavailable")
            return JSONResponse(
                {"error": "weights_not_loaded", "detail": "model pipeline unavailable"},
                status_code=503,
            )
        except Exception as e:  # noqa: BLE001 — normalize all failures to the contract
            detail = str(e).lower()
            if "out of memory" in detail or "cuda" in detail:
                logger.exception("3d generation OOM/CUDA failure")
                return JSONResponse(
                    {"error": "vram_exhausted", "detail": "GPU out of memory"}, status_code=503
                )
            logger.exception("3d generation failed")
            return JSONResponse(
                {"error": "internal_error", "detail": "generation failed"}, status_code=503
            )

    return GenerateResponse(
        generation_id=req.generation_id,
        glb_url=f"{GENERATED_URL_PREFIX}/{glb_key}",
        stl_url=f"{GENERATED_URL_PREFIX}/{stl_key}",
    )
