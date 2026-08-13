from __future__ import annotations

import asyncio
import gc
import hmac
import json
import os
import re
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
B1_MODEL_VIEW_ROOT_DEFAULT = "/srv/b1-ai-hub/models"
B1_MODEL_VIEW_FOLDER_PATHS = {
    "checkpoints": ("diffusion/checkpoints",),
    "diffusion_models": ("diffusion/diffusion_models", "diffusion/unet", "video"),
    "text_encoders": ("diffusion/text_encoders", "vision/text_encoders"),
    "clip_vision": ("vision/clip_vision",),
    "vae": ("diffusion/vae",),
    "loras": ("diffusion/loras",),
    "controlnet": ("diffusion/controlnet",),
    "upscale_models": ("diffusion/upscale_models",),
    "embeddings": ("embeddings",),
    "configs": ("diffusion/configs",),
    "audio_encoders": ("audio_encoders",),
    "model_patches": ("model_patches",),
}
B1_MODEL_VIEW_SYNC_STATE: set[tuple[str, str]] = set()
B1_MODEL_VIEW_SYNC_LAST: dict[str, Any] = {
    "status": "not-run",
    "root": B1_MODEL_VIEW_ROOT_DEFAULT,
    "manifest_count": 0,
    "added_path_count": 0,
    "folder_count": 0,
}
COMFYUI_UPSTREAM_REPOSITORY = "Comfy-Org/ComfyUI"
COMFYUI_HOOK_VERSION_DEFAULT = "b1-comfyui-hooks/v0.3.77-b1"
COMFYUI_UPSTREAM_VERSION_DEFAULT = "v0.3.77"
COMFYUI_UPSTREAM_COMMIT_DEFAULT = "59afc3984868289f808d02fa5cd180edfb2de240"
COMFYUI_SOURCE_ARCHIVE_SHA256_DEFAULT = "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def bounded_positive_number(value: Any, default: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or value <= 0:
        return default
    return min(float(value), maximum)


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


def normalized_git_sha(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if GIT_SHA_RE.fullmatch(candidate) else ""


def normalized_sha256(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if SHA256_RE.fullmatch(candidate) else ""


def comfyui_build_info() -> dict[str, Any]:
    hook_version = os.getenv("B1_COMFYUI_HOOK_VERSION", COMFYUI_HOOK_VERSION_DEFAULT).strip()
    upstream_version = os.getenv("B1_COMFYUI_UPSTREAM_VERSION", COMFYUI_UPSTREAM_VERSION_DEFAULT).strip()
    upstream_commit = normalized_git_sha(os.getenv("B1_COMFYUI_UPSTREAM_COMMIT", COMFYUI_UPSTREAM_COMMIT_DEFAULT))
    source_archive_sha256 = normalized_sha256(
        os.getenv("B1_COMFYUI_SOURCE_ARCHIVE_SHA256", COMFYUI_SOURCE_ARCHIVE_SHA256_DEFAULT)
    )
    status = "ok" if hook_version and upstream_version and upstream_commit and source_archive_sha256 else "unconfigured"
    return {
        "status": status,
        "runtime": "comfyui",
        "action": "build-info",
        "hook": "b1-comfyui-runtime-hooks",
        "hook_version": hook_version,
        "upstream_repository": COMFYUI_UPSTREAM_REPOSITORY,
        "upstream_version": upstream_version,
        "upstream_commit": upstream_commit,
        "source_archive_sha256": source_archive_sha256,
        "pinned": status == "ok",
    }


def prompt_max_bytes() -> int:
    configured = env_float("B1_COMFYUI_HOOK_SMOKE_PROMPT_MAX_BYTES", 1048576.0)
    return max(1024, int(configured))


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


def b1_model_view_root() -> Path:
    return Path(os.getenv("B1_COMFYUI_MODEL_VIEW_ROOT", B1_MODEL_VIEW_ROOT_DEFAULT))


def _manifest_declared_comfyui(marker: dict[str, Any]) -> bool:
    if marker.get("runtime") == "comfyui":
        return True
    manifest = marker.get("manifest")
    if isinstance(manifest, dict):
        runtimes = manifest.get("runtimes")
        return isinstance(runtimes, list) and "comfyui" in runtimes
    return False


def _file_folder_name(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/").strip("/")
    for folder, prefixes in B1_MODEL_VIEW_FOLDER_PATHS.items():
        for prefix in prefixes:
            prefix = prefix.strip("/")
            if normalized == prefix or normalized.startswith(f"{prefix}/"):
                return folder
    return None


def _invalidate_folder_cache(folders: set[str]) -> None:
    cache = getattr(folder_paths, "filename_list_cache", None)
    if isinstance(cache, dict):
        for folder in folders:
            cache.pop(folder, None)


def sync_b1_model_view_paths() -> dict[str, Any]:
    global B1_MODEL_VIEW_SYNC_STATE
    root = b1_model_view_root()
    if not root.is_dir():
        B1_MODEL_VIEW_SYNC_LAST.update(
            {
                "status": "unavailable",
                "root": str(root),
                "reason": "model_view_root_missing",
                "manifest_count": 0,
                "added_path_count": 0,
                "folder_count": 0,
            }
        )
        return dict(B1_MODEL_VIEW_SYNC_LAST)

    if not hasattr(folder_paths, "add_model_folder_path"):
        B1_MODEL_VIEW_SYNC_LAST.update(
            {
                "status": "unavailable",
                "root": str(root),
                "reason": "folder_paths_add_model_folder_path_missing",
                "manifest_count": 0,
                "added_path_count": 0,
                "folder_count": 0,
            }
        )
        return dict(B1_MODEL_VIEW_SYNC_LAST)

    discovered: set[tuple[str, str]] = set()
    manifest_count = 0
    for marker_path in root.glob("*/*/manifest.b1.json"):
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(marker, dict) or marker.get("format") != "b1-ai-hub-runtime-view/v1":
            continue
        if not _manifest_declared_comfyui(marker):
            continue
        manifest_count += 1
        view_root = marker_path.parent
        files = marker.get("files")
        if not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, dict):
                continue
            relative_path = item.get("path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                continue
            folder = _file_folder_name(relative_path)
            if not folder:
                continue
            candidate = view_root / relative_path.replace("\\", "/")
            if not candidate.is_file():
                continue
            discovered.add((folder, str(candidate.parent)))

    added = 0
    for folder, path in sorted(discovered):
        try:
            folder_paths.add_model_folder_path(folder, path, True)
            added += 1
        except Exception:
            continue

    if discovered != B1_MODEL_VIEW_SYNC_STATE:
        changed_folders = {folder for folder, _path in discovered}.union({folder for folder, _path in B1_MODEL_VIEW_SYNC_STATE})
        _invalidate_folder_cache(changed_folders)
        B1_MODEL_VIEW_SYNC_STATE = set(discovered)

    B1_MODEL_VIEW_SYNC_LAST.update(
        {
            "status": "ok",
            "root": str(root),
            "manifest_count": manifest_count,
            "added_path_count": added,
            "folder_count": len({folder for folder, _path in discovered}),
        }
    )
    return dict(B1_MODEL_VIEW_SYNC_LAST)


def iter_model_files() -> list[dict[str, str]]:
    sync_b1_model_view_paths()
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


def model_folder_summary() -> dict[str, Any]:
    runtime_view_sync = sync_b1_model_view_paths()
    folders: list[dict[str, Any]] = []
    total_files = 0
    for folder in configured_model_folders():
        try:
            names = folder_paths.get_filename_list(folder)
        except Exception as exc:
            folders.append({"folder": folder, "available": False, "file_count": 0, "error": exc.__class__.__name__})
            continue
        file_count = len([name for name in names if isinstance(name, str)])
        total_files += file_count
        folders.append({"folder": folder, "available": True, "file_count": file_count})
    return {
        "folders": folders,
        "folder_count": len(folders),
        "file_count": total_files,
        "runtime_view_sync": runtime_view_sync,
    }


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
    if str(payload.get("operation") or "").strip().lower().replace("_", "-") == "talking-head-lipsync":
        return json_response("unconfirmed", action, reason="operation_handled_by_b1_control_plane_runner")
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


def handle_status(payload: dict[str, Any]) -> dict[str, Any]:
    return json_response(
        "ok",
        "status",
        queue=queue_counts(),
        memory=memory_snapshot(),
        model_folders=model_folder_summary(),
        build_info=comfyui_build_info(),
        capabilities={
            "actions": ["status", "build-info", "load", "warm", "smoke", "unload"],
            "native_api": True,
            "scheduler_controlled": True,
        },
    )


async def handle_warm(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "warm")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_COMFYUI_HOOK_WARM_ENABLED", False):
        return json_response("unconfirmed", "warm", reason="warm_disabled", queue=queue_counts(), memory=memory_snapshot())
    prompt, error, timeout_seconds = configured_comfyui_prompt(payload, "warm")
    if error is not None:
        return error
    if prompt is not None:
        return attach_payload_identity(
            await run_queue_prompt_smoke("warm", prompt, "b1_native_prompt", "native_prompt", timeout_seconds=timeout_seconds),
            payload,
        )
    return attach_payload_identity(await run_noop_queue_smoke("warm"), payload)


async def handle_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "smoke")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_COMFYUI_HOOK_SMOKE_ENABLED", False):
        return json_response("unconfirmed", "smoke", reason="smoke_disabled", queue=queue_counts(), memory=memory_snapshot())
    prompt, error, timeout_seconds = configured_comfyui_prompt(payload, "smoke")
    if error is not None:
        return error
    if prompt is not None:
        return attach_payload_identity(
            await run_queue_prompt_smoke("smoke", prompt, "b1_native_prompt", "native_prompt", timeout_seconds=timeout_seconds),
            payload,
        )
    return attach_payload_identity(await run_noop_queue_smoke("smoke"), payload)


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


def selected_runtime_smoke_configs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    direct = payload.get("runtime_smoke_config")
    if isinstance(direct, dict):
        configs.append(direct)
    runtime_smoke = payload.get("runtime_smoke")
    if isinstance(runtime_smoke, dict):
        comfyui = runtime_smoke.get("comfyui")
        if isinstance(comfyui, dict) and comfyui not in configs:
            configs.append(comfyui)
    return configs


def prompt_from_runtime_config(config: dict[str, Any]) -> dict[str, Any] | None:
    prompt = config.get("prompt")
    if isinstance(prompt, dict):
        return prompt
    for key in ("request", "payload"):
        wrapper = config.get(key)
        if isinstance(wrapper, dict) and isinstance(wrapper.get("prompt"), dict):
            return wrapper["prompt"]
    return None


def validate_comfyui_prompt(prompt: Any, action: str) -> dict[str, Any] | None:
    if not isinstance(prompt, dict) or not prompt:
        return json_response("failed", action, reason="native_prompt_invalid")
    try:
        encoded = json.dumps(prompt, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        return json_response("failed", action, reason="native_prompt_not_json", error=exc.__class__.__name__)
    if len(encoded.encode("utf-8")) > prompt_max_bytes():
        return json_response("failed", action, reason="native_prompt_too_large")
    for node_id, node in prompt.items():
        if not isinstance(node_id, str) or not node_id.strip() or not isinstance(node, dict):
            return json_response("failed", action, reason="native_prompt_invalid")
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            return json_response("failed", action, reason="native_prompt_invalid")
        inputs = node.get("inputs", {})
        if inputs is not None and not isinstance(inputs, dict):
            return json_response("failed", action, reason="native_prompt_invalid")
    return None


def configured_comfyui_prompt(payload: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, float | None]:
    maximum_timeout = env_float("B1_COMFYUI_HOOK_MAX_SMOKE_TIMEOUT_SECONDS", 1800.0)
    default_timeout = env_float("B1_COMFYUI_HOOK_SMOKE_TIMEOUT_SECONDS", 30.0)
    for config in selected_runtime_smoke_configs(payload):
        prompt = prompt_from_runtime_config(config)
        if prompt is None:
            return None, json_response("failed", action, reason="native_prompt_missing"), None
        error = validate_comfyui_prompt(prompt, action)
        if error is not None:
            return None, error, None
        timeout_seconds = bounded_positive_number(config.get("timeout_seconds"), default_timeout, maximum_timeout)
        return prompt, None, timeout_seconds
    for key in ("comfyui_prompt", "native_prompt"):
        prompt = payload.get(key)
        if isinstance(prompt, dict):
            error = validate_comfyui_prompt(prompt, action)
            if error is not None:
                return None, error, None
            return prompt, None, default_timeout
    return None, None, None


def attach_payload_identity(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(result)
    for key in ("model", "model_alias", "resolved_model_version"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and key not in enriched:
            enriched[key] = value.strip()
    return enriched


def prompt_measurements(prompt: dict[str, Any], elapsed_ms: int, memory: dict[str, Any]) -> dict[str, Any]:
    measurements: dict[str, Any] = {
        "prompt_node_count": len(prompt),
        "run_time_ms": elapsed_ms,
    }
    loaded_model_count = memory.get("loaded_model_count")
    if isinstance(loaded_model_count, int) and loaded_model_count >= 0:
        measurements["loaded_model_count"] = loaded_model_count
    total = memory.get("vram_total_mib")
    free = memory.get("vram_free_mib")
    if isinstance(total, int) and isinstance(free, int) and total >= free >= 0:
        measurements["peak_vram_mib"] = total - free
    return measurements


async def run_noop_queue_smoke(action: str) -> dict[str, Any]:
    prompt = {"1": {"class_type": "B1RuntimeSmoke", "inputs": {}}}
    return await run_queue_prompt_smoke(action, prompt, "b1_noop_queue", "noop_prompt")


async def run_queue_prompt_smoke(
    action: str,
    prompt: dict[str, Any],
    strategy: str,
    failure_prefix: str,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    counts = queue_counts()
    if counts["tasks_remaining"] > 0:
        return json_response("unconfirmed", action, reason="queue_busy", queue=counts)

    prompt_id = f"b1-runtime-smoke-{uuid.uuid4()}"
    try:
        valid = await execution.validate_prompt(prompt_id, prompt, None)
    except Exception as exc:
        return json_response("failed", action, reason=f"{failure_prefix}_validation_failed", error=exc.__class__.__name__, strategy=strategy)
    if not valid[0]:
        return json_response("failed", action, reason=f"{failure_prefix}_invalid", strategy=strategy)

    queue = PromptServer.instance.prompt_queue
    start = time.monotonic()
    queue.put((0.0, prompt_id, prompt, {"b1_runtime_smoke": True, "strategy": strategy, "create_time": int(time.time() * 1000)}, valid[2], {}))
    deadline = time.monotonic() + (timeout_seconds if timeout_seconds is not None else env_float("B1_COMFYUI_HOOK_SMOKE_TIMEOUT_SECONDS", 30.0))
    while time.monotonic() < deadline:
        history = queue.get_history(prompt_id=prompt_id)
        if prompt_id in history:
            status = ((history[prompt_id] or {}).get("status") or {}).get("status_str")
            if status == "success":
                memory = memory_snapshot()
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return json_response(
                    "ready" if action == "warm" else "ok",
                    action,
                    strategy=strategy,
                    prompt_id=prompt_id,
                    measurements=prompt_measurements(prompt, elapsed_ms, memory),
                    memory=memory,
                )
            return json_response("failed", action, reason=f"{failure_prefix}_failed", strategy=strategy, prompt_id=prompt_id)
        await asyncio.sleep(0.2)
    return json_response("failed", action, reason=f"{failure_prefix}_timeout", strategy=strategy, prompt_id=prompt_id)


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
    elif action == "status":
        result = handle_status(payload)
    elif action == "warm":
        result = await handle_warm(payload)
    elif action == "smoke":
        result = await handle_smoke(payload)
    elif action == "unload":
        result = handle_unload(payload)
    elif action == "build-info":
        result = comfyui_build_info()
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


def safe_filename_prefix(value: Any, fallback: str = "b1-video/animated") -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return fallback
    parts: list[str] = []
    for raw in text.split("/"):
        cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in raw)
        segment = cleaned.strip("._-")[:120]
        if segment:
            parts.append(segment)
    return "/".join(parts[:4]) or fallback


def animated_gif_ui_result(filename: str, subfolder: str, file_type: str, frame_count: int, fps: int) -> dict[str, Any]:
    return {
        "ui": {
            "gifs": [
                {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": file_type,
                    "format": "image/gif",
                    "frame_count": frame_count,
                    "fps": fps,
                }
            ]
        }
    }


class B1RuntimeSaveAnimatedGif:
    def __init__(self) -> None:
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "b1-video/animated"}),
                "fps": ("INT", {"default": 4, "min": 1, "max": 12, "step": 1}),
                "max_frames": ("INT", {"default": 8, "min": 1, "max": 16, "step": 1}),
                "loop": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "save_gif"
    CATEGORY = "B1/runtime"
    OUTPUT_NODE = True

    def save_gif(
        self,
        images: Any,
        filename_prefix: str = "b1-video/animated",
        fps: int = 4,
        max_frames: int = 8,
        loop: bool = True,
    ) -> dict[str, Any]:
        import numpy as np
        from PIL import Image

        safe_fps = max(1, min(int(fps), 12))
        safe_max_frames = max(1, min(int(max_frames), 16))
        selected = images[:safe_max_frames]
        if len(selected) < 1:
            raise ValueError("B1RuntimeSaveAnimatedGif requires at least one image")

        first = selected[0]
        height = int(first.shape[0])
        width = int(first.shape[1])
        prefix = safe_filename_prefix(filename_prefix)
        full_output_folder, filename, counter, subfolder, _filename_prefix = folder_paths.get_save_image_path(
            prefix,
            self.output_dir,
            width,
            height,
        )

        frames: list[Any] = []
        for image in selected:
            tensor = image.detach() if hasattr(image, "detach") else image
            array = 255.0 * tensor.cpu().numpy() if hasattr(tensor, "cpu") else 255.0 * np.asarray(tensor)
            frames.append(Image.fromarray(np.clip(array, 0, 255).astype(np.uint8)))

        output_file = f"{filename}_{counter:05}_.gif"
        output_path = Path(full_output_folder) / output_file
        duration_ms = max(1, int(1000 / safe_fps))
        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=duration_ms,
            loop=0 if loop else 1,
            optimize=True,
        )
        return animated_gif_ui_result(output_file, subfolder, self.type, len(frames), safe_fps)


NODE_CLASS_MAPPINGS = {
    "B1RuntimeSmoke": B1RuntimeSmoke,
    "B1RuntimeTinyImage": B1RuntimeTinyImage,
    "B1RuntimeSaveAnimatedGif": B1RuntimeSaveAnimatedGif,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "B1RuntimeSmoke": "B1 Runtime Smoke",
    "B1RuntimeTinyImage": "B1 Runtime Tiny Image",
    "B1RuntimeSaveAnimatedGif": "B1 Runtime Save Animated GIF",
}
