from __future__ import annotations

import asyncio
import gc
import hmac
import os
import time
import uuid
from pathlib import PurePath
from pathlib import Path
from typing import Any

from aiohttp import web
from server import PromptServer

import comfy.model_management as model_management
import execution
import folder_paths


B1_MODEL_FOLDERS = (
    "checkpoints",
    "diffusion_models",
    "text_encoders",
    "clip_vision",
    "vae",
    "loras",
    "controlnet",
    "upscale_models",
    "embeddings",
)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def read_secret_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def runtime_control_token() -> str:
    value = os.getenv("B1_RUNTIME_CONTROL_TOKEN", "").strip()
    if value:
        return value
    token_file = os.getenv("B1_RUNTIME_CONTROL_TOKEN_FILE", "").strip()
    return read_secret_file(token_file) if token_file else ""


def runtime_control_auth_required() -> bool:
    configured = bool(os.getenv("B1_RUNTIME_CONTROL_TOKEN", "").strip() or os.getenv("B1_RUNTIME_CONTROL_TOKEN_FILE", "").strip())
    return env_bool("B1_RUNTIME_CONTROL_REQUIRE_AUTH", configured)


def bearer_token_from_header(value: str | None) -> str:
    if not value:
        return ""
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def authorization_header(headers: Any) -> str | None:
    if not hasattr(headers, "get"):
        return None
    return headers.get("Authorization") or headers.get("authorization")


def runtime_control_auth_failure(headers: Any) -> tuple[int, dict[str, Any]] | None:
    token = runtime_control_token()
    required = runtime_control_auth_required()
    if not token and not required:
        return None
    if not token:
        return 503, json_response("unconfigured", "auth", reason="runtime_control_token_missing")
    supplied = bearer_token_from_header(authorization_header(headers))
    if not hmac.compare_digest(supplied, token):
        return 401, json_response("unauthorized", "auth", reason="runtime_control_token_required")
    return None


def json_response(status: str, action: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "runtime": "comfyui", "action": action}
    payload.update(extra)
    return payload


def strip_model_version(value: str) -> str:
    return value.split("@", 1)[0].strip()


def payload_model_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("model", "resolved_model_version", "model_alias"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidate = strip_model_version(value)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def normalized_model_tokens(value: str) -> set[str]:
    stripped = strip_model_version(value).replace("\\", "/").strip()
    if not stripped:
        return set()
    path = PurePath(stripped)
    tokens = {stripped, path.name}
    if path.suffix:
        tokens.add(path.stem)
    return {token.lower() for token in tokens if token}


def configured_model_folders() -> tuple[str, ...]:
    value = os.getenv("B1_COMFYUI_HOOK_MODEL_FOLDERS", "")
    if not value.strip():
        return B1_MODEL_FOLDERS
    folders = tuple(part.strip() for part in value.replace(",", " ").split() if part.strip())
    return folders or B1_MODEL_FOLDERS


def iter_model_files() -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for folder in configured_model_folders():
        try:
            names = folder_paths.get_filename_list(folder)
        except Exception:
            continue
        for name in names:
            if isinstance(name, str):
                files.append({"folder": folder, "name": name})
    return files


def model_available(candidates: list[str], files: list[dict[str, str]]) -> bool:
    wanted = set()
    for candidate in candidates:
        wanted.update(normalized_model_tokens(candidate))
    if not wanted:
        return False
    for item in files:
        for token in normalized_model_tokens(item.get("name", "")):
            if token in wanted:
                return True
    return False


def queue_counts() -> dict[str, int]:
    queue = PromptServer.instance.prompt_queue
    try:
        running, queued = queue.get_current_queue()
        return {"running": len(running), "queued": len(queued), "tasks_remaining": queue.get_tasks_remaining()}
    except Exception:
        return {"running": 0, "queued": 0, "tasks_remaining": 0}


def memory_snapshot() -> dict[str, Any]:
    try:
        device = model_management.get_torch_device()
        device_name = model_management.get_torch_device_name(device)
        vram_total, torch_vram_total = model_management.get_total_memory(device, torch_total_too=True)
        vram_free, torch_vram_free = model_management.get_free_memory(device, torch_free_too=True)
        loaded_models = model_management.loaded_models()
        return {
            "available": True,
            "device": str(device),
            "device_name": str(device_name),
            "vram_total_mib": int(vram_total / (1024 * 1024)),
            "vram_free_mib": int(vram_free / (1024 * 1024)),
            "torch_vram_total_mib": int(torch_vram_total / (1024 * 1024)),
            "torch_vram_free_mib": int(torch_vram_free / (1024 * 1024)),
            "loaded_model_count": len(loaded_models),
        }
    except Exception as exc:
        return {"available": False, "error": exc.__class__.__name__}


def check_model_list(payload: dict[str, Any], action: str) -> dict[str, Any] | None:
    candidates = payload_model_candidates(payload)
    if not candidates:
        return json_response("unconfirmed", action, reason="workflow_dependencies_resolved_by_native_prompt")
    files = iter_model_files()
    if files and model_available(candidates, files):
        return None
    if env_bool("B1_COMFYUI_HOOK_STRICT_MODEL_LIST", False):
        return json_response("unconfigured", action, reason="model_not_listed", model_file_count=len(files))
    return json_response("unconfirmed", action, reason="model_not_listed", model_file_count=len(files))


def handle_load(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "load")
    if list_result is not None:
        return list_result
    return json_response(
        "unconfirmed",
        "load",
        reason="comfyui_loads_models_from_native_workflow",
        queue=queue_counts(),
        memory=memory_snapshot(),
    )


async def handle_warm(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "warm")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_COMFYUI_HOOK_WARM_ENABLED", False):
        return json_response("unconfirmed", "warm", reason="warm_disabled", queue=queue_counts(), memory=memory_snapshot())
    return await run_noop_queue_smoke("warm")


async def handle_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "smoke")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_COMFYUI_HOOK_SMOKE_ENABLED", False):
        return json_response("unconfirmed", "smoke", reason="smoke_disabled", queue=queue_counts(), memory=memory_snapshot())
    return await run_noop_queue_smoke("smoke")


def unload_idle_now() -> dict[str, Any]:
    try:
        model_management.unload_all_models()
        gc.collect()
        model_management.soft_empty_cache()
    except Exception as exc:
        return json_response("failed", "unload", reason="native_unload_failed", error=exc.__class__.__name__)
    return json_response("ok", "unload", strategy="native_free_flags_immediate_idle", queue=queue_counts(), memory=memory_snapshot())


def handle_unload(payload: dict[str, Any]) -> dict[str, Any]:
    queue = PromptServer.instance.prompt_queue
    counts = queue_counts()
    queue.set_flag("unload_models", True)
    queue.set_flag("free_memory", True)
    if counts["tasks_remaining"] > 0:
        return json_response("unconfirmed", "unload", reason="native_free_flags_queued_until_prompt_safe_point", queue=counts)
    if not env_bool("B1_COMFYUI_HOOK_UNLOAD_IMMEDIATE_IDLE", True):
        return json_response("unconfirmed", "unload", reason="native_free_flags_submitted", queue=counts)
    return unload_idle_now()


async def run_noop_queue_smoke(action: str) -> dict[str, Any]:
    counts = queue_counts()
    if counts["tasks_remaining"] > 0:
        return json_response("unconfirmed", action, reason="queue_busy", queue=counts)

    prompt_id = f"b1-runtime-smoke-{uuid.uuid4()}"
    prompt = {"1": {"class_type": "B1RuntimeSmoke", "inputs": {}}}
    try:
        valid = await execution.validate_prompt(prompt_id, prompt, None)
    except Exception as exc:
        return json_response("failed", action, reason="noop_prompt_validation_failed", error=exc.__class__.__name__)
    if not valid[0]:
        return json_response("failed", action, reason="noop_prompt_invalid")

    queue = PromptServer.instance.prompt_queue
    queue.put((0.0, prompt_id, prompt, {"b1_runtime_smoke": True, "create_time": int(time.time() * 1000)}, valid[2], {}))
    deadline = time.monotonic() + env_float("B1_COMFYUI_HOOK_SMOKE_TIMEOUT_SECONDS", 30.0)
    while time.monotonic() < deadline:
        history = queue.get_history(prompt_id=prompt_id)
        if prompt_id in history:
            status = ((history[prompt_id] or {}).get("status") or {}).get("status_str")
            if status == "success":
                return json_response("ready" if action == "warm" else "ok", action, strategy="b1_noop_queue", memory=memory_snapshot())
            return json_response("failed", action, reason="noop_prompt_failed", strategy="b1_noop_queue")
        await asyncio.sleep(0.2)
    return json_response("failed", action, reason="noop_prompt_timeout", strategy="b1_noop_queue")


async def runtime_action(request: web.Request) -> web.Response:
    action = request.match_info.get("action", "").strip().lower()
    auth_failure = runtime_control_auth_failure(request.headers)
    if auth_failure is not None:
        status, payload = auth_failure
        return web.json_response(payload, status=status)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return web.json_response(json_response("invalid", action, reason="invalid_json"), status=400)

    if action == "load":
        result = handle_load(payload)
    elif action == "warm":
        result = await handle_warm(payload)
    elif action == "smoke":
        result = await handle_smoke(payload)
    elif action == "unload":
        result = handle_unload(payload)
    else:
        result = json_response("unsupported", action, reason="unknown_action")
    return web.json_response(result)


@PromptServer.instance.routes.post("/b1/runtime/{action}")
async def post_b1_runtime_action(request: web.Request) -> web.Response:
    return await runtime_action(request)


class B1RuntimeSmoke:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "B1/runtime"
    OUTPUT_NODE = True

    def run(self) -> tuple[str]:
        return ("ok",)


class B1RuntimeTinyImage:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "width": ("INT", {"default": 64, "min": 1, "max": 512, "step": 1}),
                "height": ("INT", {"default": 64, "min": 1, "max": 512, "step": 1}),
                "red": ("INT", {"default": 47, "min": 0, "max": 255, "step": 1}),
                "green": ("INT", {"default": 111, "min": 0, "max": 255, "step": 1}),
                "blue": ("INT", {"default": 115, "min": 0, "max": 255, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "B1/runtime"

    def run(self, width: int = 64, height: int = 64, red: int = 47, green: int = 111, blue: int = 115) -> tuple[Any]:
        import torch

        safe_width = max(1, min(int(width), 512))
        safe_height = max(1, min(int(height), 512))
        image = torch.zeros((1, safe_height, safe_width, 3), dtype=torch.float32)
        image[:, :, :, 0] = max(0, min(int(red), 255)) / 255.0
        image[:, :, :, 1] = max(0, min(int(green), 255)) / 255.0
        image[:, :, :, 2] = max(0, min(int(blue), 255)) / 255.0
        return (image,)


NODE_CLASS_MAPPINGS = {
    "B1RuntimeSmoke": B1RuntimeSmoke,
    "B1RuntimeTinyImage": B1RuntimeTinyImage,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "B1RuntimeSmoke": "B1 Runtime Smoke",
    "B1RuntimeTinyImage": "B1 Runtime Tiny Image",
}
