from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import shutil
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


RUNTIME_VERSION = os.getenv("B1_LIPSYNC_RUNTIME_VERSION", "b1-lipsync-musetalk/v0.1.0-b1")
MUSETALK_COMMIT = os.getenv("B1_MUSETALK_COMMIT", "0a89dec45a0192b824e3cf4daf96c239440c5ed8")
MODEL_ROOT = Path(os.getenv("B1_LIPSYNC_MODEL_ROOT", "/srv/b1-ai-hub/models"))
CACHE_ROOT = Path(os.getenv("B1_LIPSYNC_CACHE_ROOT", "/srv/b1-ai-hub/cache"))
MUSETALK_ROOT = Path(os.getenv("B1_MUSETALK_ROOT", "/opt/musetalk"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("B1_LIPSYNC_REQUEST_TIMEOUT_SECONDS", "1800"))
MAX_INPUT_BYTES = int(os.getenv("B1_LIPSYNC_MAX_INPUT_BYTES", str(256 * 1024 * 1024)))
RUNTIME_CONTROL_TOKEN_FILE = os.getenv("B1_RUNTIME_CONTROL_TOKEN_FILE", "")
RUNTIME_CONTROL_REQUIRE_AUTH = os.getenv("B1_RUNTIME_CONTROL_REQUIRE_AUTH", "true").strip().lower() in {"1", "true", "yes", "on"}

MUSETALK_UNET_SHA256 = os.getenv("B1_MUSETALK_UNET_SHA256", "")
MUSETALK_UNET_CONFIG_SHA256 = os.getenv("B1_MUSETALK_UNET_CONFIG_SHA256", "")
MUSETALK_REQUIRED_FILES = {
    "musetalk_unet": MODEL_ROOT / "musetalkV15/unet.pth",
    "musetalk_config": MODEL_ROOT / "musetalkV15/musetalk.json",
    "sd_vae_config": MODEL_ROOT / "sd-vae/config.json",
    "sd_vae_weights": MODEL_ROOT / "sd-vae/diffusion_pytorch_model.bin",
    "whisper_config": MODEL_ROOT / "whisper/config.json",
    "whisper_weights": MODEL_ROOT / "whisper/pytorch_model.bin",
    "whisper_preprocessor": MODEL_ROOT / "whisper/preprocessor_config.json",
    "dwpose": MODEL_ROOT / "dwpose/dw-ll_ucoco_384.pth",
    "syncnet": MODEL_ROOT / "syncnet/latentsync_syncnet.pt",
    "face_parse": MODEL_ROOT / "face-parse-bisent/79999_iter.pth",
    "face_parse_resnet18": MODEL_ROOT / "face-parse-bisent/resnet18-5c106cde.pth",
    "s3fd": MODEL_ROOT / "torch/hub/checkpoints/s3fd-619a316812.pth",
}
SUPPORTED_IMAGE_MIME_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
CHARACTER_PERFORMANCE_SCHEMA_VERSION = "dialecticore.character_performance.v1"
CHARACTER_PERFORMANCE_ENUMS = {
    "on_camera_energy": {"restrained", "measured", "engaged"},
    "gaze_style": {"steady", "reflective", "responsive"},
    "head_motion": {"minimal", "subtle", "expressive"},
    "expression_range": {"contained", "warm", "animated"},
    "gesture_frequency": {"none", "occasional", "frequent"},
}
PERFORMANCE_RENDER_CACHE_VERSION = "b1-performance-render-cache/v1"

app = FastAPI(title="B1 AI Hub Lipsync Runtime", version="0.1.0")
LOGGER = logging.getLogger("b1.lipsync")


class LipsyncRequest(BaseModel):
    portrait_b64: str = Field(min_length=1)
    portrait_mime_type: str = Field(default="image/png")
    audio_b64: str = Field(min_length=1)
    audio_sha256: str = Field(min_length=64, max_length=64)
    timing_sha256: str = Field(default_factory=lambda: hashlib.sha256(b"{}").hexdigest(), min_length=64, max_length=64)
    width: int = Field(default=1280, ge=64, le=1920)
    height: int = Field(default=720, ge=64, le=1080)
    fps: int = Field(default=24, ge=1, le=60)
    duration_ms: int = Field(default=1000, ge=250, le=60000)
    batch_size: int = Field(default=1, ge=1, le=8)
    extra_margin: int = Field(default=10, ge=0, le=80)
    audio_padding_length_left: int = Field(default=2, ge=0, le=8)
    audio_padding_length_right: int = Field(default=2, ge=0, le=8)
    parsing_mode: str = Field(default="jaw")
    left_cheek_width: int = Field(default=90, ge=0, le=160)
    right_cheek_width: int = Field(default=90, ge=0, le=160)
    use_float16: bool = True
    performance_plan: dict[str, Any] | None = None
    source_context: str = Field(default="portrait", pattern=r"^(portrait|scene_face_region)$")


def read_secret(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def require_runtime_token(token: str | None) -> None:
    expected = read_secret(RUNTIME_CONTROL_TOKEN_FILE)
    if not RUNTIME_CONTROL_REQUIRE_AUTH:
        return
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail={"code": "runtime_auth_failed", "message": "invalid runtime control token"})


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def decode_b64(value: str, field_name: str) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_base64", "message": f"{field_name} must be base64"}) from exc
    if not data:
        raise HTTPException(status_code=422, detail={"code": "empty_input", "message": f"{field_name} is empty"})
    if len(data) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "input_too_large", "message": f"{field_name} exceeds input limit"})
    return data


def checkpoint_status() -> dict[str, Any]:
    files: dict[str, Any] = {}
    for name, path in MUSETALK_REQUIRED_FILES.items():
        expected = ""
        if name == "musetalk_unet":
            expected = MUSETALK_UNET_SHA256
        elif name == "musetalk_config":
            expected = MUSETALK_UNET_CONFIG_SHA256
        present = path.is_file()
        digest = file_sha256(path) if present and expected else None
        files[name] = {
            "path": str(path),
            "present": present,
            "sha256": digest,
            "expected_sha256": expected or None,
            "ok": bool(present and (not expected or (digest and hmac.compare_digest(digest, expected)))),
        }
    return files


def ensure_ready() -> None:
    status = checkpoint_status()
    missing = [name for name, item in status.items() if not item["ok"]]
    if missing:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "lipsync_runtime_unavailable",
                "message": "MuseTalk runtime weights are missing or checksum-invalid",
                "missing": missing,
                "status": status,
            },
        )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    status = checkpoint_status()
    ready = all(item["ok"] for item in status.values())
    return {
        "status": "ok" if ready else "degraded",
        "runtime": "lipsync",
        "kind": "musetalk",
        "version": RUNTIME_VERSION,
        "musetalk_commit": MUSETALK_COMMIT,
        "ready": ready,
        "checkpoints": status,
    }


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    ensure_ready()
    return await healthz()


@app.get("/b1/runtime/build-info")
async def build_info() -> dict[str, Any]:
    return {
        "status": "ok",
        "runtime": "lipsync",
        "kind": "musetalk",
        "version": RUNTIME_VERSION,
        "musetalk_commit": MUSETALK_COMMIT,
        "base_image": "pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime@sha256:82e0d379a5dedd6303c89eda57bcc434c40be11f249ddfadfd5673b84351e806",
        "license": {
            "name": "MuseTalk MIT code and commercial-permitted trained model; third-party component licences still apply",
            "url": "https://github.com/TMElyralab/MuseTalk#disclaimerlicense",
        },
    }


@app.get("/b1/runtime/status")
async def runtime_status() -> dict[str, Any]:
    return await healthz()


@app.post("/b1/runtime/load")
async def runtime_load(x_b1_runtime_token: str | None = Header(default=None, alias="X-B1-Runtime-Token")) -> dict[str, Any]:
    require_runtime_token(x_b1_runtime_token)
    ensure_ready()
    # Model processes are spawned only after the scheduler has acquired its GPU
    # lease and submitted an audio-driven job. This hook must stay CPU-only.
    return {"status": "ready", "runtime": "lipsync", "action": "load", "resident_model": None}


@app.post("/b1/runtime/warm")
async def runtime_warm(x_b1_runtime_token: str | None = Header(default=None, alias="X-B1-Runtime-Token")) -> dict[str, Any]:
    require_runtime_token(x_b1_runtime_token)
    ensure_ready()
    return {"status": "ready", "runtime": "lipsync", "action": "warm", "resident_model": None}


@app.post("/b1/runtime/unload")
async def runtime_unload(x_b1_runtime_token: str | None = Header(default=None, alias="X-B1-Runtime-Token")) -> dict[str, Any]:
    require_runtime_token(x_b1_runtime_token)
    return {"status": "ok", "runtime": "lipsync", "action": "unload", "resident_model": None}


def write_input_files(job_dir: Path, payload: LipsyncRequest) -> tuple[Path, Path]:
    portrait = decode_b64(payload.portrait_b64, "portrait_b64")
    audio = decode_b64(payload.audio_b64, "audio_b64")
    audio_sha256 = hashlib.sha256(audio).hexdigest()
    if not hmac.compare_digest(audio_sha256, payload.audio_sha256.lower()):
        raise HTTPException(status_code=422, detail={"code": "audio_sha256_mismatch", "message": "audio_sha256 does not match audio_b64"})
    extension = SUPPORTED_IMAGE_MIME_TYPES.get(payload.portrait_mime_type)
    if extension is None:
        raise HTTPException(status_code=422, detail={"code": "unsupported_portrait_type", "message": "portrait_mime_type must be PNG, JPEG, or WebP"})
    portrait_path = job_dir / f"portrait{extension}"
    audio_path = job_dir / "dialogue.wav"
    portrait_path.write_bytes(portrait)
    audio_path.write_bytes(audio)
    return portrait_path, audio_path


def normalize_output_video(input_path: Path, output_path: Path, *, width: int, height: int, fps: int, duration_ms: int) -> None:
    command = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(duration_ms / 1000) + 30))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffmpeg normalization failed")[:1000])


def validated_character_performance_plan(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {"schema_version", *CHARACTER_PERFORMANCE_ENUMS, "signature_habit", "variation_seed"}
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown or missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "lipsync_performance_plan_unsupported",
                "message": "performance_plan must contain exactly the supported DialectiCore character performance fields",
                "unknown_fields": unknown,
                "missing_fields": missing,
            },
        )
    if value.get("schema_version") != CHARACTER_PERFORMANCE_SCHEMA_VERSION:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "lipsync_performance_plan_unsupported",
                "message": f"performance_plan.schema_version must be {CHARACTER_PERFORMANCE_SCHEMA_VERSION}",
            },
        )
    for field_name, allowed_values in CHARACTER_PERFORMANCE_ENUMS.items():
        if value.get(field_name) not in allowed_values:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "lipsync_performance_plan_unsupported",
                    "message": f"performance_plan.{field_name} has an unsupported value",
                    "allowed_values": sorted(allowed_values),
                },
            )
    signature_habit = value.get("signature_habit")
    seed = value.get("variation_seed")
    if not isinstance(signature_habit, str) or not signature_habit.strip() or len(signature_habit) > 240:
        raise HTTPException(
            status_code=422,
            detail={"code": "lipsync_performance_plan_unsupported", "message": "performance_plan.signature_habit must be a non-empty description of at most 240 characters"},
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647:
        raise HTTPException(
            status_code=422,
            detail={"code": "lipsync_performance_plan_unsupported", "message": "performance_plan.variation_seed must be an integer from 0 to 2147483647"},
        )
    return {key: value[key] for key in sorted(required)}


def alpha_blend(base: np.ndarray, altered: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    alpha_3d = np.clip(alpha, 0.0, 1.0)[..., None]
    return (base.astype(np.float32) * (1.0 - alpha_3d) + altered.astype(np.float32) * alpha_3d).astype(np.uint8)


def feathered_ellipse_mask(shape: tuple[int, int], center: tuple[int, int], axes: tuple[int, int], blur: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    blur = max(3, blur | 1)
    return cv2.GaussianBlur(mask, (blur, blur), 0).astype(np.float32) / 255.0


def detected_face_bounds(frame: np.ndarray) -> tuple[tuple[int, int, int, int], str]:
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        raise RuntimeError("OpenCV frontal-face detector is unavailable")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    if len(faces) > 0:
        return tuple(int(value) for value in max(faces, key=lambda face: int(face[2]) * int(face[3]))), "opencv_haar"
    # MuseTalk has already accepted the portrait and produces a face-centred
    # talking-head frame. Haar can miss its own generated first frame, so use a
    # conservative central region rather than nondeterministically rejecting a
    # valid audio-driven render. The metadata makes this fallback observable.
    height, width = frame.shape[:2]
    return (int(width * 0.20), int(height * 0.15), int(width * 0.60), int(height * 0.68)), "musetalk_face_centered_fallback"


def mux_performance_audio(video_path: Path, audio_source: Path, output_path: Path) -> None:
    command = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "performance audio mux failed")[:1000])


def apply_character_performance(input_path: Path, audio_source: Path, output_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply sparse deterministic non-mouth motion while retaining MuseTalk mouth pixels."""
    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError("cannot open MuseTalk video for performance compositing")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 24.0)
    ok, first_frame = capture.read()
    if not ok or first_frame is None:
        capture.release()
        raise RuntimeError("MuseTalk video has no frames for performance compositing")
    height, width = first_frame.shape[:2]
    (face_x, face_y, face_w, face_h), face_region_source = detected_face_bounds(first_frame)
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    silent_path = output_path.with_name("performance-silent.mp4")
    writer = cv2.VideoWriter(str(silent_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("cannot create performance compositor video")

    plan_json = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    plan_sha256 = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    phase = (int(plan_sha256[:8], 16) ^ int(plan["variation_seed"])) / 2**32 * math.tau
    energy = {"restrained": 0.35, "measured": 0.75, "engaged": 1.25}[plan["on_camera_energy"]]
    head_amplitude = {"minimal": 0.45, "subtle": 1.25, "expressive": 2.5}[plan["head_motion"]] * energy
    gaze_amplitude = {"steady": 0.08, "reflective": 0.55, "responsive": 1.05}[plan["gaze_style"]] * energy
    expression_amplitude = {"contained": 2.0, "warm": 6.0, "animated": 10.0}[plan["expression_range"]] * energy
    gesture_amplitude = {"none": 0.0, "occasional": 0.75, "frequent": 1.65}[plan["gesture_frequency"]] * energy

    mouth_mask = feathered_ellipse_mask(
        (height, width),
        (face_x + face_w // 2, face_y + int(face_h * 0.73)),
        (max(16, int(face_w * 0.36)), max(12, int(face_h * 0.20))),
        max(9, int(min(face_w, face_h) * 0.12)),
    )
    non_mouth_mask = 1.0 - mouth_mask
    left_eye_mask = feathered_ellipse_mask(
        (height, width),
        (face_x + int(face_w * 0.32), face_y + int(face_h * 0.39)),
        (max(8, int(face_w * 0.13)), max(5, int(face_h * 0.055))),
        max(5, int(face_w * 0.04)),
    )
    right_eye_mask = feathered_ellipse_mask(
        (height, width),
        (face_x + int(face_w * 0.68), face_y + int(face_h * 0.39)),
        (max(8, int(face_w * 0.13)), max(5, int(face_h * 0.055))),
        max(5, int(face_w * 0.04)),
    )
    eye_mask = np.clip((left_eye_mask + right_eye_mask) * non_mouth_mask, 0.0, 1.0)
    cheek_mask = np.clip(
        (
            feathered_ellipse_mask((height, width), (face_x + int(face_w * 0.25), face_y + int(face_h * 0.60)), (max(8, int(face_w * 0.18)), max(8, int(face_h * 0.15))), max(5, int(face_w * 0.05)))
            + feathered_ellipse_mask((height, width), (face_x + int(face_w * 0.75), face_y + int(face_h * 0.60)), (max(8, int(face_w * 0.18)), max(8, int(face_h * 0.15))), max(5, int(face_w * 0.05)))
        )
        * non_mouth_mask,
        0.0,
        1.0,
    )
    torso_mask = np.zeros((height, width), dtype=np.float32)
    torso_start = min(height - 1, face_y + int(face_h * 0.82))
    torso_mask[torso_start:, :] = 1.0
    torso_mask = cv2.GaussianBlur(torso_mask, (0, 0), sigmaX=max(2.0, face_h * 0.08)) * non_mouth_mask

    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            time_seconds = frame_index / fps
            head_phase = math.tau * 0.32 * time_seconds + phase
            dx = head_amplitude * math.sin(head_phase)
            dy = head_amplitude * 0.55 * math.sin(head_phase * 0.73 + 0.8)
            angle = head_amplitude * 0.12 * math.sin(head_phase * 0.57 + 0.3)
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            matrix[:, 2] += (dx, dy)
            head_frame = cv2.warpAffine(frame, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
            rendered = alpha_blend(frame, head_frame, non_mouth_mask)

            gaze_dx = gaze_amplitude * math.sin(math.tau * 0.46 * time_seconds + phase * 1.7)
            gaze_frame = cv2.warpAffine(rendered, np.float32([[1, 0, gaze_dx], [0, 1, 0]]), (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
            rendered = alpha_blend(rendered, gaze_frame, eye_mask)

            hsv = cv2.cvtColor(rendered, cv2.COLOR_BGR2HSV).astype(np.float32)
            warmth = expression_amplitude * (0.55 + 0.45 * math.sin(math.tau * 0.21 * time_seconds + phase * 0.6))
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] + cheek_mask * warmth, 0, 255)
            hsv[:, :, 2] = np.clip(hsv[:, :, 2] + cheek_mask * (warmth * 0.35), 0, 255)
            expression_frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
            rendered = alpha_blend(rendered, expression_frame, cheek_mask)

            if gesture_amplitude:
                gesture_dx = gesture_amplitude * math.sin(math.tau * 0.18 * time_seconds + phase * 0.4)
                torso_frame = cv2.warpAffine(rendered, np.float32([[1, 0, gesture_dx], [0, 1, 0]]), (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
                rendered = alpha_blend(rendered, torso_frame, torso_mask)
            writer.write(rendered)
            frame_index += 1
    finally:
        capture.release()
        writer.release()
    if frame_index == 0:
        raise RuntimeError("performance compositor emitted no frames")
    mux_performance_audio(silent_path, audio_source, output_path)
    with suppress(FileNotFoundError):
        silent_path.unlink()
    return {
        "mode": "audio_driven_character_performance",
        "applied": True,
        "plan_sha256": plan_sha256,
        "variation_seed": plan["variation_seed"],
        "backend": "b1-musetalk-protected-performance-compositor/v1",
        "mouth_region_protected": True,
        "face_region_source": face_region_source,
        "controls": ["head_motion", "gaze_style", "expression_range", "gesture_frequency", "signature_habit"],
    }


def write_musetalk_config(job_dir: Path, portrait_path: Path, audio_path: Path) -> Path:
    config_path = job_dir / "musetalk_job.yaml"
    config_path.write_text(
        "\n".join(
            [
                "task_0:",
                f" video_path: {json.dumps(str(portrait_path))}",
                f" audio_path: {json.dumps(str(audio_path))}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def write_seeded_musetalk_sitecustomize(job_dir: Path, variation_seed: int) -> Path:
    """Seed the child MuseTalk process without changing its CUDA kernel choices."""
    sitecustomize_path = job_dir / "sitecustomize.py"
    sitecustomize_path.write_text(
        "\n".join(
            [
                "import os",
                "import random",
                "import numpy as np",
                "import torch",
                f"seed = {variation_seed}",
                "random.seed(seed)",
                "np.random.seed(seed)",
                "torch.manual_seed(seed)",
                "if torch.cuda.is_available():",
                "    torch.cuda.manual_seed_all(seed)",
                "torch.backends.cudnn.benchmark = False",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return sitecustomize_path


def performance_render_cache_key(payload: LipsyncRequest, plan: dict[str, Any], portrait_path: Path) -> str:
    """Bind a reusable render to every visual/audio input and performance control."""
    payload_view = {
        "cache_version": PERFORMANCE_RENDER_CACHE_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "musetalk_commit": MUSETALK_COMMIT,
        "portrait_sha256": file_sha256(portrait_path),
        "portrait_mime_type": payload.portrait_mime_type,
        "audio_sha256": payload.audio_sha256.lower(),
        "timing_sha256": payload.timing_sha256.lower(),
        "width": payload.width,
        "height": payload.height,
        "fps": payload.fps,
        "duration_ms": payload.duration_ms,
        "batch_size": payload.batch_size,
        "extra_margin": payload.extra_margin,
        "audio_padding_length_left": payload.audio_padding_length_left,
        "audio_padding_length_right": payload.audio_padding_length_right,
        "parsing_mode": payload.parsing_mode,
        "left_cheek_width": payload.left_cheek_width,
        "right_cheek_width": payload.right_cheek_width,
        "use_float16": payload.use_float16,
        "performance_plan": plan,
    }
    return hashlib.sha256(json.dumps(payload_view, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def performance_render_cache_paths(cache_key: str) -> tuple[Path, Path]:
    cache_dir = CACHE_ROOT / "performance-renders"
    return cache_dir / f"{cache_key}.mp4", cache_dir / f"{cache_key}.json"


def load_cached_performance_render(cache_key: str) -> tuple[bytes, dict[str, Any]] | None:
    video_path, metadata_path = performance_render_cache_paths(cache_key)
    try:
        content = video_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not content or not isinstance(metadata, dict) or metadata.get("cache_key") != cache_key:
        return None
    performance = metadata.get("performance")
    if not isinstance(performance, dict) or performance.get("applied") is not True:
        return None
    return content, {"performance": performance, "performance_render_cache": "hit"}


def store_cached_performance_render(cache_key: str, content: bytes, performance: dict[str, Any]) -> None:
    video_path, metadata_path = performance_render_cache_paths(cache_key)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_video = video_path.with_suffix(".mp4.partial")
    temporary_metadata = metadata_path.with_suffix(".json.partial")
    temporary_video.write_bytes(content)
    os.chmod(temporary_video, 0o600)
    temporary_metadata.write_text(
        json.dumps({"cache_key": cache_key, "performance": performance}, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary_metadata, 0o600)
    temporary_video.replace(video_path)
    temporary_metadata.replace(metadata_path)


def find_musetalk_output(result_dir: Path) -> Path | None:
    candidates = sorted(result_dir.rglob("*.mp4"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for candidate in candidates:
        try:
            if candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def musetalk_diagnostic_tail(result: subprocess.CompletedProcess[str]) -> str:
    """Keep the upstream script's final error, which it writes to stdout."""
    parts = [part[-1000:] for part in (result.stderr or "", result.stdout or "") if part]
    return "\n".join(parts) or "MuseTalk produced no diagnostic output"


def runtime_error_detail(exc: Exception) -> tuple[int, str, str]:
    diagnostic = str(exc)[-2000:]
    normalized = diagnostic.lower()
    if "out of memory" in normalized and "cuda" in normalized:
        return 503, "lipsync_cuda_out_of_memory", "MuseTalk could not reserve enough CUDA memory; wait for the active GPU workload to unload and retry"
    if "invalid device ordinal" in normalized or "no cuda gpus are available" in normalized:
        return 503, "lipsync_cuda_device_unavailable", "MuseTalk could not access its configured CUDA device; verify NVIDIA container access and retry"
    return 500, "lipsync_runtime_error", diagnostic[-1000:]


def run_musetalk(job_dir: Path, payload: LipsyncRequest) -> tuple[bytes, dict[str, Any]]:
    ensure_ready()
    performance_plan = validated_character_performance_plan(payload.performance_plan)
    portrait_path, audio_path = write_input_files(job_dir, payload)
    performance_cache_key: str | None = None
    if performance_plan is not None:
        performance_cache_key = performance_render_cache_key(payload, performance_plan, portrait_path)
        cached_render = load_cached_performance_render(performance_cache_key)
        if cached_render is not None:
            return cached_render
    result_dir = job_dir / "results"
    final_output = job_dir / "result.mp4"
    result_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_musetalk_config(job_dir, portrait_path, audio_path)
    if performance_plan is not None:
        write_seeded_musetalk_sitecustomize(job_dir, performance_plan["variation_seed"])
    command = [
        "python",
        "-m",
        "scripts.inference",
        "--inference_config",
        str(config_path),
        "--result_dir",
        str(result_dir),
        "--unet_model_path",
        str(MODEL_ROOT / "musetalkV15/unet.pth"),
        "--unet_config",
        str(MODEL_ROOT / "musetalkV15/musetalk.json"),
        "--whisper_dir",
        str(MODEL_ROOT / "whisper"),
        "--version",
        "v15",
        "--ffmpeg_path",
        "/usr/bin",
        "--fps",
        str(payload.fps),
        "--batch_size",
        str(payload.batch_size),
        "--extra_margin",
        str(payload.extra_margin),
        "--audio_padding_length_left",
        str(payload.audio_padding_length_left),
        "--audio_padding_length_right",
        str(payload.audio_padding_length_right),
        "--parsing_mode",
        payload.parsing_mode,
        "--left_cheek_width",
        str(payload.left_cheek_width),
        "--right_cheek_width",
        str(payload.right_cheek_width),
        "--output_vid_name",
        "musetalk_raw.mp4",
    ]
    if payload.use_float16:
        command.append("--use_float16")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{job_dir}:{MUSETALK_ROOT}:{env.get('PYTHONPATH', '')}"
    env["TORCH_HOME"] = str(MODEL_ROOT / "torch")
    env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
    if performance_plan is not None:
        env["PYTHONHASHSEED"] = str(performance_plan["variation_seed"])
    result = subprocess.run(command, cwd=MUSETALK_ROOT, env=env, capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SECONDS)
    raw_output = find_musetalk_output(result_dir)
    if raw_output is None:
        diagnostic = musetalk_diagnostic_tail(result)
        if "division by zero" in diagnostic and "face bounding boxes" in diagnostic:
            if payload.source_context == "scene_face_region":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "scene_face_not_detected",
                        "message": "MuseTalk could not track a usable face inside the declared scene face region",
                    },
                )
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "lipsync_face_not_detected",
                    "message": "MuseTalk could not detect a usable frontal face in the portrait; provide a clear human face with visible eyes, nose, and mouth",
                },
            )
        if result.returncode != 0:
            raise RuntimeError(f"MuseTalk inference failed with exit {result.returncode}: {diagnostic}")
        raise RuntimeError(f"MuseTalk did not create a non-empty MP4: {diagnostic}")
    performance: dict[str, Any] | None = None
    output_for_normalization = raw_output
    if performance_plan is not None:
        performance_output = job_dir / "performance-with-audio.mp4"
        performance = apply_character_performance(raw_output, audio_path, performance_output, performance_plan)
        output_for_normalization = performance_output
    normalize_output_video(output_for_normalization, final_output, width=payload.width, height=payload.height, fps=payload.fps, duration_ms=payload.duration_ms)
    content = final_output.read_bytes()
    if not content:
        raise RuntimeError("normalized lipsync MP4 is empty")
    if performance_cache_key is not None and performance is not None:
        store_cached_performance_render(performance_cache_key, content, performance)
    return content, {
        "musetalk_stdout_tail": (result.stdout or "")[-1000:],
        "musetalk_stderr_tail": (result.stderr or "")[-1000:],
        "musetalk_returncode": result.returncode,
        **({"performance": performance} if performance is not None else {}),
    }


@app.post("/v1/talking-head/lipsync")
async def talking_head_lipsync(
    payload: LipsyncRequest,
    x_b1_runtime_token: str | None = Header(default=None, alias="X-B1-Runtime-Token"),
) -> dict[str, Any]:
    require_runtime_token(x_b1_runtime_token)
    job_dir = CACHE_ROOT / "jobs" / f"lipsync_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        content, runtime_log = await asyncio.to_thread(run_musetalk, job_dir, payload)
        return {
            "status": "ok",
            "runtime": "lipsync",
            "backend": "musetalk",
            "mime_type": "video/mp4",
            "content_b64": base64.b64encode(content).decode("ascii"),
            "metadata": {
                "mode": "audio_driven",
                "backend": "b1-musetalk-v1.5",
                "musetalk_commit": MUSETALK_COMMIT,
                "duration_ms": payload.duration_ms,
                "fps": payload.fps,
                "batch_size": payload.batch_size,
                "use_float16": payload.use_float16,
                **runtime_log,
            },
        }
    except HTTPException:
        raise
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail={"code": "lipsync_timeout", "message": "MuseTalk inference timed out"}) from exc
    except Exception as exc:
        status_code, code, message = runtime_error_detail(exc)
        # Preserve the private runtime diagnostic for operators without logging
        # request text, portrait bytes, audio bytes, or client credentials.
        LOGGER.warning("MuseTalk inference failed code=%s diagnostic=%s", code, str(exc)[-4000:])
        raise HTTPException(status_code=status_code, detail={"code": code, "message": message}) from exc
    finally:
        with suppress(FileNotFoundError):
            shutil.rmtree(job_dir)
