from __future__ import annotations

import os
import uuid
import base64
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect


RUNTIME_NAME = os.getenv("B1_RUNTIME_NAME", "runtime")
RUNTIME_KIND = os.getenv("B1_RUNTIME_KIND", "placeholder")
PROMPT_HISTORY: dict[str, dict[str, Any]] = {}
GPU_RESIDENT_MODEL: dict[str, Any] | None = None

app = FastAPI(title=f"B1 AI Hub {RUNTIME_NAME}", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "runtime": RUNTIME_NAME,
        "kind": RUNTIME_KIND,
        "gpu_resident_model": GPU_RESIDENT_MODEL,
        "placeholder": True,
    }


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    return await healthz()


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [], "runtime": RUNTIME_NAME}


@app.post("/b1/runtime/load")
async def b1_runtime_load(payload: dict[str, Any]) -> dict[str, Any]:
    global GPU_RESIDENT_MODEL
    GPU_RESIDENT_MODEL = {
        "model": payload.get("model"),
        "model_alias": payload.get("model_alias"),
        "resolved_model_version": payload.get("resolved_model_version"),
        "job_id": payload.get("job_id"),
        "loaded_at": datetime.now(tz=UTC).isoformat(),
    }
    return {"status": "ok", "action": "load", "runtime": RUNTIME_NAME, "gpu_resident_model": GPU_RESIDENT_MODEL}


@app.post("/b1/runtime/warm")
async def b1_runtime_warm(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "action": "warm",
        "runtime": RUNTIME_NAME,
        "model": payload.get("model"),
        "gpu_resident_model": GPU_RESIDENT_MODEL,
    }


@app.post("/b1/runtime/smoke")
async def b1_runtime_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "action": "smoke",
        "runtime": RUNTIME_NAME,
        "model": payload.get("model"),
        "model_alias": payload.get("model_alias"),
        "resolved_model_version": payload.get("resolved_model_version"),
        "gpu_resident_model": GPU_RESIDENT_MODEL,
        "measurements": {
            "request_count": 1,
            "placeholder": True,
        },
    }


@app.post("/b1/runtime/unload")
async def b1_runtime_unload(payload: dict[str, Any]) -> dict[str, Any]:
    global GPU_RESIDENT_MODEL
    previous = GPU_RESIDENT_MODEL
    GPU_RESIDENT_MODEL = None
    return {
        "status": "ok",
        "action": "unload",
        "runtime": RUNTIME_NAME,
        "job_id": payload.get("job_id"),
        "previous_gpu_resident_model": previous,
    }


@app.post("/v1/chat/completions")
async def chat(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(datetime.now(tz=UTC).timestamp()),
        "model": payload.get("model", f"{RUNTIME_NAME}-placeholder"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"{RUNTIME_NAME} placeholder runtime response."},
                "finish_reason": "stop",
            }
        ],
    }


@app.post("/v1/images/generations")
async def image_generations(payload: dict[str, Any]) -> dict[str, Any]:
    content = f"{RUNTIME_NAME}:image:{payload.get('model', 'placeholder')}\n".encode("utf-8")
    return {
        "created": int(datetime.now(tz=UTC).timestamp()),
        "data": [{"b64_json": base64.b64encode(content).decode("ascii"), "mime_type": "image/png"}],
        "b1_runtime": RUNTIME_NAME,
    }


@app.post("/v1/images/edits")
async def image_edits(request: Request) -> dict[str, Any]:
    model = "placeholder"
    with suppress(Exception):
        payload = await request.json()
        if isinstance(payload, dict):
            model = str(payload.get("model") or model)
    body = await request.body()
    content = f"{RUNTIME_NAME}:image-edit:{model}:bytes={len(body)}\n".encode("utf-8")
    return {
        "created": int(datetime.now(tz=UTC).timestamp()),
        "data": [{"b64_json": base64.b64encode(content).decode("ascii"), "mime_type": "image/png"}],
        "b1_runtime": RUNTIME_NAME,
    }


@app.post("/v1/videos/generations")
async def video_generations(payload: dict[str, Any]) -> dict[str, Any]:
    content = f"{RUNTIME_NAME}:video:{payload.get('model', 'placeholder')}\n".encode("utf-8")
    return {
        "created": int(datetime.now(tz=UTC).timestamp()),
        "data": [{"b64_json": base64.b64encode(content).decode("ascii"), "mime_type": "video/mp4"}],
        "b1_runtime": RUNTIME_NAME,
    }


@app.post("/v1/videos/image-to-video")
async def image_to_video(request: Request) -> dict[str, Any]:
    model = "placeholder"
    with suppress(Exception):
        payload = await request.json()
        if isinstance(payload, dict):
            model = str(payload.get("model") or model)
    body = await request.body()
    content = f"{RUNTIME_NAME}:image-to-video:{model}:bytes={len(body)}\n".encode("utf-8")
    return {
        "created": int(datetime.now(tz=UTC).timestamp()),
        "data": [{"b64_json": base64.b64encode(content).decode("ascii"), "mime_type": "video/mp4"}],
        "b1_runtime": RUNTIME_NAME,
    }


@app.post("/v1/audio/speech")
async def audio_speech(payload: dict[str, Any]) -> Response:
    content = f"{RUNTIME_NAME}:speech:{payload.get('model', 'placeholder')}\n".encode("utf-8")
    return Response(content=content, media_type="audio/wav")


@app.post("/prompt")
async def prompt(_: Request) -> dict[str, Any]:
    prompt_id = uuid.uuid4().hex
    PROMPT_HISTORY[prompt_id] = {
        prompt_id: {
            "status": {"completed": True},
            "outputs": {
                "b1-placeholder-save": {
                    "images": [
                        {"filename": f"{prompt_id}.png", "subfolder": "b1-placeholder", "type": "output"},
                    ]
                }
            },
        }
    }
    return {"prompt_id": prompt_id, "number": 0, "node_errors": {}, "b1_runtime": RUNTIME_NAME}


@app.get("/history")
async def history() -> dict[str, Any]:
    return {}


@app.get("/history/{prompt_id}")
async def history_prompt(prompt_id: str) -> dict[str, Any]:
    return PROMPT_HISTORY.get(prompt_id, {prompt_id: {"status": {"completed": True}, "outputs": {}}})


@app.get("/view")
async def view(filename: str, subfolder: str = "", type: str = "output") -> Response:
    return Response(content=f"{RUNTIME_NAME}:{type}:{subfolder}:{filename}\n".encode("utf-8"), media_type="image/png")


@app.get("/queue")
async def queue() -> dict[str, Any]:
    return {"queue_running": [], "queue_pending": []}


@app.post("/interrupt")
async def interrupt() -> dict[str, Any]:
    return {"status": "interrupt_forwarded", "runtime": RUNTIME_NAME}


@app.get("/object_info")
async def object_info() -> dict[str, Any]:
    return {}


@app.get("/system_stats")
async def system_stats() -> dict[str, Any]:
    return {"system": {"os": "placeholder"}, "devices": []}


@app.get("/models")
async def comfy_models() -> list[Any]:
    return []


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}}})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
