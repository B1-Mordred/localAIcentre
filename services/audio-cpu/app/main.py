from __future__ import annotations

import io
import base64
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import wave
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


app = FastAPI(title="B1 AI Hub CPU Audio", version="0.1.0")
PLACEHOLDER_ENGINES = {"scaffold"}
PIPER_ENGINE = "piper"
ONNX_EMBEDDING_ENGINE = "onnx"
VOSK_STT_ENGINE = "vosk"
DEFAULT_PIPER_BINARY = "/opt/piper/piper"
DEFAULT_AUDIO_CPU_RUNTIME_VERSION = "b1-audio-cpu/v0.1.0-b1"
DEFAULT_AUDIO_CPU_BASE_IMAGE = "python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
DEFAULT_PIPER_RELEASE = "2023.11.14-2"
DEFAULT_PIPER_ASSET = "piper_linux_x86_64.tar.gz"
DEFAULT_PIPER_SHA256 = "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992"
DEFAULT_CPU_RESIDENT_ALIASES = ("embedding-default", "tts-fast", "stt-default")
SUPPORTED_ENGINES = {*PLACEHOLDER_ENGINES, PIPER_ENGINE, ONNX_EMBEDDING_ENGINE, VOSK_STT_ENGINE}
SAFE_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[a-f0-9]{64}$")
AUDIO_CPU_LIFECYCLE_ACTIONS = ("status", "build-info", "smoke", "unload")
AUDIO_CPU_PACKAGE_PINS = {
    "fastapi": "0.139.2",
    "uvicorn": "0.35.0",
    "pydantic": "2.11.7",
    "numpy": "2.5.1",
    "onnxruntime": "1.27.0",
    "tokenizers": "0.23.1",
    "vosk": "0.3.45",
}


class AudioCpuError(RuntimeError):
    def __init__(self, message: str, *, code: str = "b1_audio_cpu_engine_failed", status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    return bool_env("B1_RUNTIME_CONTROL_REQUIRE_AUTH", configured)


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
        return 503, {
            "status": "unconfigured",
            "runtime": "audio-cpu",
            "action": "auth",
            "reason": "runtime_control_token_missing",
            "gpu_lease_required": False,
        }
    supplied = bearer_token_from_header(authorization_header(headers))
    if not hmac.compare_digest(supplied, token):
        return 401, {
            "status": "unauthorized",
            "runtime": "audio-cpu",
            "action": "auth",
            "reason": "runtime_control_token_required",
            "gpu_lease_required": False,
        }
    return None


def configured_engine() -> str:
    return os.getenv("B1_CPU_AUDIO_ENGINE", "scaffold").strip().lower() or "scaffold"


def configured_embedding_engine() -> str:
    raw = os.getenv("B1_CPU_EMBEDDING_ENGINE")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    return "scaffold" if configured_engine() in PLACEHOLDER_ENGINES else ""


def configured_stt_engine() -> str:
    raw = os.getenv("B1_CPU_STT_ENGINE")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    return "scaffold" if configured_engine() in PLACEHOLDER_ENGINES else ""


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    aliases = []
    seen = set()
    for item in raw.split(","):
        alias = item.strip()
        if not alias or alias in seen:
            continue
        aliases.append(alias)
        seen.add(alias)
    return tuple(aliases)


def placeholder_enabled() -> bool:
    return bool_env("B1_CPU_AUDIO_ENABLE_PLACEHOLDER", True)


def audio_cpu_runtime_version() -> str:
    return os.getenv("B1_AUDIO_CPU_RUNTIME_VERSION", DEFAULT_AUDIO_CPU_RUNTIME_VERSION).strip() or DEFAULT_AUDIO_CPU_RUNTIME_VERSION


def audio_cpu_base_image() -> str:
    return os.getenv("B1_AUDIO_CPU_BASE_IMAGE", DEFAULT_AUDIO_CPU_BASE_IMAGE).strip() or DEFAULT_AUDIO_CPU_BASE_IMAGE


def audio_cpu_piper_release() -> str:
    return os.getenv("B1_AUDIO_CPU_PIPER_RELEASE", DEFAULT_PIPER_RELEASE).strip() or DEFAULT_PIPER_RELEASE


def audio_cpu_piper_asset() -> str:
    return os.getenv("B1_AUDIO_CPU_PIPER_ASSET", DEFAULT_PIPER_ASSET).strip() or DEFAULT_PIPER_ASSET


def audio_cpu_piper_sha256() -> str:
    return os.getenv("B1_AUDIO_CPU_PIPER_SHA256", DEFAULT_PIPER_SHA256).strip().lower() or DEFAULT_PIPER_SHA256


def configured_cpu_residency_enabled() -> bool:
    return bool_env("B1_CPU_RESIDENCY_ENABLED", True)


def configured_cpu_residency_max_ram_gib() -> float:
    return float_env("B1_CPU_RESIDENCY_MAX_RAM_GIB", 2.0, 0.0, 256.0)


def configured_host_total_ram_gib() -> float:
    return float_env("B1_HOST_TOTAL_RAM_GIB", 32.0, 1.0, 4096.0)


def configured_host_reserve_ram_gib() -> float:
    return float_env("B1_HOST_RESERVE_RAM_GIB", 6.0, 0.0, configured_host_total_ram_gib())


def configured_cpu_resident_aliases() -> tuple[str, ...]:
    return list_env("B1_CPU_RESIDENT_ALIASES", DEFAULT_CPU_RESIDENT_ALIASES)


def payload_model_alias(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("b1_model_alias", "model_alias", "b1_public_model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def bool_from_payload(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def cpu_residency_allowed(payload: dict[str, Any] | None) -> bool:
    if isinstance(payload, dict):
        explicit = bool_from_payload(payload.get("b1_cpu_residency_allowed"))
        if explicit is False:
            return False
        if explicit is True:
            return cpu_residency_headroom()["ok"]
    alias = payload_model_alias(payload)
    return bool(configured_cpu_residency_enabled() and alias and alias in set(configured_cpu_resident_aliases()) and cpu_residency_headroom()["ok"])


def meminfo_available_ram_gib(path: str = "/proc/meminfo") -> float | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key != "MemAvailable":
            continue
        parts = value.strip().split()
        if not parts:
            return None
        try:
            kib = float(parts[0])
        except ValueError:
            return None
        return max(0.0, kib / 1024.0 / 1024.0)
    return None


def cpu_residency_headroom() -> dict[str, Any]:
    available_ram_gib = meminfo_available_ram_gib()
    reserve_ram_gib = configured_host_reserve_ram_gib()
    ok = available_ram_gib is None or available_ram_gib >= reserve_ram_gib
    reason = "ok"
    if available_ram_gib is None:
        reason = "mem_available_unmeasured"
    elif not ok:
        reason = "host_ram_below_reserve"
    return {
        "ok": ok,
        "reason": reason,
        "available_ram_gib": round(available_ram_gib, 3) if available_ram_gib is not None else None,
        "reserve_ram_gib": reserve_ram_gib,
        "total_ram_gib": configured_host_total_ram_gib(),
    }


def cpu_residency_details() -> dict[str, Any]:
    return {
        "enabled": configured_cpu_residency_enabled(),
        "max_ram_gib": configured_cpu_residency_max_ram_gib(),
        "resident_aliases": list(configured_cpu_resident_aliases()),
        "headroom": cpu_residency_headroom(),
        "cache": resident_cache_state(),
    }


def configured_model_root() -> Path:
    return Path(os.getenv("B1_CPU_AUDIO_MODEL_ROOT", "/srv/b1-ai-hub/models")).resolve(strict=False)


def configured_embedding_model_root() -> Path:
    return Path(os.getenv("B1_ONNX_EMBEDDING_MODEL_ROOT") or os.getenv("B1_CPU_AUDIO_MODEL_ROOT", "/srv/b1-ai-hub/models")).resolve(strict=False)


def configured_stt_model_root() -> Path:
    return Path(os.getenv("B1_VOSK_STT_MODEL_ROOT") or os.getenv("B1_CPU_AUDIO_MODEL_ROOT", "/srv/b1-ai-hub/models")).resolve(strict=False)


def is_same_or_child(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_executable(value: str) -> str | None:
    if not value:
        return None
    if os.sep not in value:
        return shutil.which(value)
    path = Path(value)
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path.resolve())


def safe_ref_component(value: str) -> str | None:
    value = value.strip()
    return value if SAFE_REF_COMPONENT.match(value) else None


def model_ref_from_payload(payload: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("b1_resolved_model_version", "resolved_model_version"):
        raw = payload.get(key)
        if isinstance(raw, str) and "@" in raw:
            model_id, version = raw.split("@", 1)
            safe_model_id = safe_ref_component(model_id)
            safe_version = safe_ref_component(version)
            if safe_model_id and safe_version:
                return safe_model_id, safe_version
    raw_model = payload.get("model")
    if isinstance(raw_model, str) and "@" in raw_model:
        model_id, version = raw_model.split("@", 1)
        safe_model_id = safe_ref_component(model_id)
        safe_version = safe_ref_component(version)
        if safe_model_id and safe_version:
            return safe_model_id, safe_version
    return None


def version_for_single_model_dir(model_dir: Path) -> str | None:
    if not model_dir.is_dir() or model_dir.is_symlink():
        return None
    versions = [item.name for item in model_dir.iterdir() if item.is_dir() and not item.is_symlink() and safe_ref_component(item.name)]
    return versions[0] if len(versions) == 1 else None


def model_ref_from_payload_or_model(payload: dict[str, Any] | None, root: Path) -> tuple[str, str] | None:
    parsed = model_ref_from_payload(payload)
    if parsed is not None:
        return parsed
    if not isinstance(payload, dict):
        return None
    raw_model = payload.get("model")
    if not isinstance(raw_model, str):
        return None
    model_id = safe_ref_component(raw_model)
    if model_id is None:
        return None
    version = version_for_single_model_dir(root / model_id)
    if version is None:
        return None
    return model_id, version


def runtime_view_dir_from_payload(payload: dict[str, Any] | None, root: Path) -> tuple[Path | None, str | None]:
    model_ref = model_ref_from_payload_or_model(payload, root)
    if model_ref is None:
        return None, "model_path_missing"
    model_id, version = model_ref
    model_dir = root / model_id / version
    try:
        resolved_dir = model_dir.resolve(strict=True)
    except OSError:
        return None, "model_runtime_view_missing"
    if not resolved_dir.is_dir() or model_dir.is_symlink() or not is_same_or_child(resolved_dir, root):
        return None, "model_runtime_view_invalid"
    return resolved_dir, None


def safe_runtime_files(root: Path, suffix: str) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob(f"*{suffix}"):
        if path.is_file() and not path.is_symlink():
            resolved = path.resolve()
            if is_same_or_child(resolved, root):
                files.append(resolved)
    return sorted(files)


def single_runtime_file(root: Path, suffix: str, missing_reason: str, ambiguous_reason: str) -> tuple[Path | None, str | None]:
    files = safe_runtime_files(root, suffix)
    if not files:
        return None, missing_reason
    if len(files) > 1:
        return None, ambiguous_reason
    return files[0], None


def piper_config_for_model(model_path: Path) -> Path | None:
    config_path = model_path.with_name(model_path.name + ".json")
    if config_path.is_file() and not config_path.is_symlink():
        return config_path.resolve()
    return None


def resolve_piper_model_from_payload(payload: dict[str, Any] | None, *, root: Path) -> tuple[Path | None, Path | None, str | None]:
    resolved_dir, reason = runtime_view_dir_from_payload(payload, root)
    if resolved_dir is None:
        return None, None, reason
    model_path, file_reason = single_runtime_file(resolved_dir, ".onnx", "model_onnx_missing", "model_onnx_ambiguous")
    if model_path is None:
        return None, None, file_reason
    return model_path, piper_config_for_model(model_path), None


def resolve_onnx_embedding_model_from_payload(payload: dict[str, Any] | None, *, root: Path) -> tuple[Path | None, Path | None, str | None]:
    resolved_dir, reason = runtime_view_dir_from_payload(payload, root)
    if resolved_dir is None:
        return None, None, reason
    model_path, model_reason = single_runtime_file(resolved_dir, ".onnx", "embedding_onnx_missing", "embedding_onnx_ambiguous")
    if model_path is None:
        return None, None, model_reason
    tokenizer_path = resolved_dir / "tokenizer.json"
    if not tokenizer_path.is_file() or tokenizer_path.is_symlink():
        tokenizer_path, tokenizer_reason = single_runtime_file(resolved_dir, "tokenizer.json", "embedding_tokenizer_missing", "embedding_tokenizer_ambiguous")
        if tokenizer_path is None:
            return model_path, None, tokenizer_reason
    return model_path, tokenizer_path.resolve(), None


def any_piper_runtime_model_available(root: Path) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    for model_dir in root.iterdir():
        if not model_dir.is_dir() or model_dir.is_symlink() or not safe_ref_component(model_dir.name):
            continue
        for version_dir in model_dir.iterdir():
            if not version_dir.is_dir() or version_dir.is_symlink() or not safe_ref_component(version_dir.name):
                continue
            onnx_files = [path for path in version_dir.rglob("*.onnx") if path.is_file() and not path.is_symlink()]
            if len(onnx_files) == 1 and piper_config_for_model(onnx_files[0]) is not None:
                return True
    return False


def onnx_embedding_dependency_status() -> list[str]:
    missing = []
    for module in ("numpy", "onnxruntime", "tokenizers"):
        if importlib.util.find_spec(module) is None:
            missing.append(module)
    return missing


def any_onnx_embedding_runtime_model_available(root: Path) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    for model_dir in root.iterdir():
        if not model_dir.is_dir() or model_dir.is_symlink() or not safe_ref_component(model_dir.name):
            continue
        for version_dir in model_dir.iterdir():
            if not version_dir.is_dir() or version_dir.is_symlink() or not safe_ref_component(version_dir.name):
                continue
            onnx_files = safe_runtime_files(version_dir, ".onnx")
            tokenizer_files = safe_runtime_files(version_dir, "tokenizer.json")
            if len(onnx_files) == 1 and len(tokenizer_files) == 1:
                return True
    return False


def vosk_stt_dependency_status() -> list[str]:
    return ["vosk"] if importlib.util.find_spec("vosk") is None else []


def is_vosk_model_dir(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    required_files = [path / "am" / "final.mdl", path / "conf" / "model.conf"]
    if any(not item.is_file() or item.is_symlink() for item in required_files):
        return False
    graph_dir = path / "graph"
    if not graph_dir.is_dir() or graph_dir.is_symlink():
        return False
    graph_candidates = [graph_dir / "HCLr.fst", graph_dir / "HCLG.fst"]
    return any(item.is_file() and not item.is_symlink() for item in graph_candidates)


def find_vosk_model_dirs(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    candidates: list[Path] = []
    if is_vosk_model_dir(root):
        candidates.append(root.resolve())
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if is_vosk_model_dir(child):
            candidates.append(child.resolve())
    return candidates


def any_vosk_stt_runtime_model_available(root: Path) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    for model_dir in root.iterdir():
        if not model_dir.is_dir() or model_dir.is_symlink() or not safe_ref_component(model_dir.name):
            continue
        for version_dir in model_dir.iterdir():
            if not version_dir.is_dir() or version_dir.is_symlink() or not safe_ref_component(version_dir.name):
                continue
            if len(find_vosk_model_dirs(version_dir)) == 1:
                return True
    return False


def resolve_model_dir(raw_path: str | None, *, root: Path) -> tuple[Path | None, str | None]:
    if not raw_path:
        return None, "model_path_missing"
    requested = Path(raw_path)
    if not requested.is_absolute():
        requested = root / requested
    try:
        resolved = requested.resolve(strict=True)
    except OSError:
        return None, "model_path_missing"
    if requested.is_symlink() or not resolved.is_dir():
        return None, "model_path_not_directory"
    if not is_same_or_child(resolved, root):
        return None, "model_path_outside_allowed_root"
    return resolved, None


def resolve_vosk_model_from_payload(payload: dict[str, Any] | None, *, root: Path) -> tuple[Path | None, str | None]:
    resolved_dir, reason = runtime_view_dir_from_payload(payload, root)
    if resolved_dir is None:
        model_dir, env_reason = resolve_model_dir(os.getenv("B1_VOSK_STT_MODEL_PATH"), root=root)
        if model_dir is None:
            return None, reason or env_reason
        candidates = find_vosk_model_dirs(model_dir)
    else:
        candidates = find_vosk_model_dirs(resolved_dir)
    if not candidates:
        return None, "vosk_model_files_missing"
    if len(candidates) > 1:
        return None, "vosk_model_ambiguous"
    return candidates[0], None


def vosk_stt_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = configured_stt_model_root()
    model_dir, model_reason = resolve_vosk_model_from_payload(payload, root=root)
    missing_dependencies = vosk_stt_dependency_status()
    model_selection_available = bool(
        payload is None
        and model_dir is None
        and not missing_dependencies
        and any_vosk_stt_runtime_model_available(root)
    )
    available = bool((model_dir or model_selection_available) and not missing_dependencies)
    reasons = []
    if model_reason and not model_selection_available:
        reasons.append(model_reason)
    if missing_dependencies:
        reasons.append("missing_dependencies:" + ",".join(missing_dependencies))
    return {
        "available": available,
        "engine": VOSK_STT_ENGINE,
        "model_path": str(model_dir) if model_dir else None,
        "model_root": str(root),
        "reason": ",".join(reasons) if reasons else None,
        "language": os.getenv("B1_VOSK_STT_LANGUAGE", "en-us").strip() or "en-us",
        "max_audio_bytes": int_env("B1_VOSK_STT_MAX_AUDIO_BYTES", 25 * 1024 * 1024, 1024, 512 * 1024 * 1024),
        "max_audio_seconds": int_env("B1_VOSK_STT_MAX_AUDIO_SECONDS", 1800, 1, 24 * 60 * 60),
        "words": bool_env("B1_VOSK_STT_WORDS", True),
    }


def onnx_embedding_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = configured_embedding_model_root()
    model, tokenizer_path, model_reason = resolve_onnx_embedding_model_from_payload(payload, root=root)
    missing_dependencies = onnx_embedding_dependency_status()
    available = bool(model and tokenizer_path and not missing_dependencies)
    reasons = []
    if model_reason:
        reasons.append(model_reason)
    if missing_dependencies:
        reasons.append("missing_dependencies:" + ",".join(missing_dependencies))
    return {
        "available": available,
        "engine": ONNX_EMBEDDING_ENGINE,
        "model_path": str(model) if model else None,
        "tokenizer_path": str(tokenizer_path) if tokenizer_path else None,
        "model_root": str(root),
        "reason": ",".join(reasons) if reasons else None,
        "max_tokens": int_env("B1_ONNX_EMBEDDING_MAX_TOKENS", 256, 8, 8192),
        "max_batch": int_env("B1_ONNX_EMBEDDING_MAX_BATCH", 32, 1, 512),
        "max_text_chars": int_env("B1_ONNX_EMBEDDING_MAX_TEXT_CHARS", 8000, 1, 100000),
        "normalize": bool_env("B1_ONNX_EMBEDDING_NORMALIZE", True),
    }


def resolve_model_file(raw_path: str | None, *, root: Path) -> tuple[Path | None, str | None]:
    if not raw_path:
        return None, "model_path_missing"
    requested = Path(raw_path)
    if not requested.is_absolute():
        requested = root / requested
    try:
        resolved = requested.resolve(strict=True)
    except OSError:
        return None, "model_path_missing"
    if requested.is_symlink() or not resolved.is_file():
        return None, "model_path_not_regular_file"
    if not is_same_or_child(resolved, root):
        return None, "model_path_outside_allowed_root"
    return resolved, None


def piper_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = configured_model_root()
    binary = resolve_executable(os.getenv("B1_PIPER_BINARY", DEFAULT_PIPER_BINARY).strip())
    model, config_path, model_reason = resolve_piper_model_from_payload(payload, root=root)
    config_reason: str | None = None
    if model is None:
        model, model_reason = resolve_model_file(os.getenv("B1_PIPER_MODEL_PATH"), root=root)
        raw_config = os.getenv("B1_PIPER_CONFIG_PATH")
        if raw_config:
            config_path, config_reason = resolve_model_file(raw_config, root=root)
    available = bool(binary and model and not config_reason)
    reasons = []
    if binary is None:
        reasons.append("piper_binary_missing")
    if model_reason:
        reasons.append(model_reason)
    if config_reason:
        reasons.append(f"config_{config_reason}")
    return {
        "available": available,
        "engine": PIPER_ENGINE,
        "binary": binary,
        "model_path": str(model) if model else None,
        "config_path": str(config_path) if config_path else None,
        "model_root": str(root),
        "reason": ",".join(reasons) if reasons else None,
        "timeout_seconds": int_env("B1_PIPER_TIMEOUT_SECONDS", 30, 1, 600),
        "max_text_chars": int_env("B1_CPU_AUDIO_MAX_TEXT_CHARS", 4000, 1, 20000),
    }


def engine_available(operation: str | None = None, payload: dict[str, Any] | None = None) -> bool:
    if operation in {"embedding", "embeddings"}:
        engine = configured_embedding_engine()
        if engine in PLACEHOLDER_ENGINES:
            return placeholder_enabled()
        if engine == ONNX_EMBEDDING_ENGINE:
            return bool(onnx_embedding_status(payload)["available"])
        return False
    if operation in {"transcription", "speech-to-text", "stt"}:
        engine = configured_stt_engine()
        if engine in PLACEHOLDER_ENGINES:
            return placeholder_enabled()
        if engine == VOSK_STT_ENGINE:
            if payload is None:
                return bool(
                    not vosk_stt_dependency_status()
                    and any_vosk_stt_runtime_model_available(configured_stt_model_root())
                )
            return bool(vosk_stt_status(payload)["available"])
        return False
    engine = configured_engine()
    if engine in PLACEHOLDER_ENGINES:
        return placeholder_enabled()
    if engine == PIPER_ENGINE:
        return (operation in {None, "speech", "tts"}) and bool(piper_status(payload)["available"])
    return False


def operation_capabilities() -> dict[str, bool]:
    engine = configured_engine()
    embedding_engine = configured_embedding_engine()
    stt_engine = configured_stt_engine()
    embeddings_available = False
    if embedding_engine in PLACEHOLDER_ENGINES:
        embeddings_available = placeholder_enabled()
    elif embedding_engine == ONNX_EMBEDDING_ENGINE:
        status = onnx_embedding_status()
        embeddings_available = bool(
            status["available"]
            or (
                not onnx_embedding_dependency_status()
                and any_onnx_embedding_runtime_model_available(configured_embedding_model_root())
            )
        )
    transcription_available = False
    if stt_engine in PLACEHOLDER_ENGINES:
        transcription_available = placeholder_enabled()
    elif stt_engine == VOSK_STT_ENGINE:
        status = vosk_stt_status()
        transcription_available = bool(
            status["available"]
            or (
                not vosk_stt_dependency_status()
                and any_vosk_stt_runtime_model_available(configured_stt_model_root())
            )
        )
    if engine in PLACEHOLDER_ENGINES:
        available = placeholder_enabled()
        return {"speech": available, "transcription": transcription_available, "embeddings": embeddings_available}
    if engine == PIPER_ENGINE:
        status = piper_status()
        dynamic_model_selection = not os.getenv("B1_PIPER_MODEL_PATH") and not os.getenv("B1_PIPER_CONFIG_PATH")
        return {
            "speech": bool(status["available"] or (status["binary"] and dynamic_model_selection and any_piper_runtime_model_available(configured_model_root()))),
            "transcription": transcription_available,
            "embeddings": embeddings_available,
        }
    return {"speech": False, "transcription": transcription_available, "embeddings": embeddings_available}


def operation_engine(operation: str) -> str:
    if operation in {"embedding", "embeddings"}:
        return configured_embedding_engine() or configured_engine()
    if operation in {"transcription", "speech-to-text", "stt"}:
        return configured_stt_engine() or configured_engine()
    return configured_engine()


def unavailable_response(operation: str) -> JSONResponse:
    engine = operation_engine(operation)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": f"audio-cpu {operation} has no configured production engine and placeholder mode is disabled",
                "type": "engine_unavailable",
                "code": "b1_audio_cpu_engine_unavailable",
            },
            "gpu_lease_required": False,
            "b1_engine": engine,
            "b1_placeholder": engine in PLACEHOLDER_ENGINES,
            "b1_engine_details": engine_details(),
        },
        headers={"X-B1-GPU-Lease-Required": "false", "X-B1-CPU-Audio-Engine": engine},
    )


def engine_failure_response(exc: AudioCpuError, operation: str = "speech") -> JSONResponse:
    engine = operation_engine(operation)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": str(exc),
                "type": "engine_failed",
                "code": exc.code,
            },
            "gpu_lease_required": False,
            "b1_engine": engine,
            "b1_placeholder": engine in PLACEHOLDER_ENGINES,
        },
        headers={"X-B1-GPU-Lease-Required": "false", "X-B1-CPU-Audio-Engine": engine},
    )


def engine_details() -> dict[str, Any]:
    engine = configured_engine()
    embedding_engine = configured_embedding_engine()
    stt_engine = configured_stt_engine()
    details: dict[str, Any] = {
        "supported": engine in SUPPORTED_ENGINES,
        "embedding_engine": embedding_engine or "disabled",
        "stt_engine": stt_engine or "disabled",
        "cpu_residency": cpu_residency_details(),
    }
    if engine == PIPER_ENGINE:
        status = piper_status()
        details.update({key: value for key, value in status.items() if key not in {"binary"}})
    if embedding_engine == ONNX_EMBEDDING_ENGINE:
        details["embedding"] = onnx_embedding_status()
    if stt_engine == VOSK_STT_ENGINE:
        details["stt"] = vosk_stt_status()
    return details


def audio_cpu_build_info() -> dict[str, Any]:
    base_image = audio_cpu_base_image().lower()
    piper_sha256 = audio_cpu_piper_sha256()
    pinned = bool(IMAGE_DIGEST_RE.fullmatch(base_image) and SHA256_HEX_RE.fullmatch(piper_sha256))
    return {
        "status": "ok" if pinned else "unconfigured",
        "runtime": "audio-cpu",
        "action": "build-info",
        "component": "b1-audio-cpu",
        "runtime_version": audio_cpu_runtime_version(),
        "base_image": audio_cpu_base_image(),
        "piper_release": audio_cpu_piper_release(),
        "piper_asset": audio_cpu_piper_asset(),
        "piper_asset_sha256": piper_sha256,
        "pinned": pinned,
        "engines": {
            "speech": PIPER_ENGINE,
            "embeddings": ONNX_EMBEDDING_ENGINE,
            "transcription": VOSK_STT_ENGINE,
        },
        "python_packages": dict(AUDIO_CPU_PACKAGE_PINS),
        "capabilities": {
            "actions": list(AUDIO_CPU_LIFECYCLE_ACTIONS),
            "gpu_lease_required": False,
        },
    }


def path_presence_fields(raw: dict[str, Any]) -> dict[str, bool]:
    fields: dict[str, bool] = {}
    for key in ("binary", "model_path", "config_path", "tokenizer_path", "model_root"):
        if key in raw:
            fields[f"{key}_present"] = bool(raw.get(key))
    return fields


def redacted_probe_status(raw: dict[str, Any], *, available: bool, placeholder: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "engine": str(raw.get("engine") or "disabled"),
        "available": bool(available),
        "configured": bool(raw.get("engine")),
        "placeholder": placeholder,
        **path_presence_fields(raw),
    }
    reason = raw.get("reason")
    if reason:
        result["reason"] = str(reason)
    for key in (
        "timeout_seconds",
        "max_text_chars",
        "max_tokens",
        "max_batch",
        "normalize",
        "language",
        "max_audio_bytes",
        "max_audio_seconds",
        "words",
    ):
        if key in raw:
            result[key] = raw[key]
    return result


def scaffold_probe_status(engine: str, *, available: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "engine": engine,
        "available": available,
        "configured": True,
        "placeholder": True,
    }
    if not available:
        payload["reason"] = "placeholder_disabled"
    return payload


def disabled_probe_status(engine: str, *, reason: str) -> dict[str, Any]:
    return {
        "engine": engine or "disabled",
        "available": False,
        "configured": bool(engine),
        "placeholder": engine in PLACEHOLDER_ENGINES,
        "reason": reason,
    }


def audio_cpu_runtime_status() -> dict[str, Any]:
    engine = configured_engine()
    embedding_engine = configured_embedding_engine()
    stt_engine = configured_stt_engine()
    capabilities = operation_capabilities()

    if engine == PIPER_ENGINE:
        speech = redacted_probe_status(piper_status(), available=capabilities["speech"])
    elif engine in PLACEHOLDER_ENGINES:
        speech = scaffold_probe_status(engine, available=capabilities["speech"])
    else:
        speech = disabled_probe_status(engine, reason="unsupported_engine")

    if embedding_engine == ONNX_EMBEDDING_ENGINE:
        embeddings = redacted_probe_status(onnx_embedding_status(), available=capabilities["embeddings"])
    elif embedding_engine in PLACEHOLDER_ENGINES:
        embeddings = scaffold_probe_status(embedding_engine, available=capabilities["embeddings"])
    elif embedding_engine:
        embeddings = disabled_probe_status(embedding_engine, reason="unsupported_engine")
    else:
        embeddings = disabled_probe_status("", reason="engine_disabled")

    if stt_engine == VOSK_STT_ENGINE:
        transcription = redacted_probe_status(vosk_stt_status(), available=capabilities["transcription"])
    elif stt_engine in PLACEHOLDER_ENGINES:
        transcription = scaffold_probe_status(stt_engine, available=capabilities["transcription"])
    elif stt_engine:
        transcription = disabled_probe_status(stt_engine, reason="unsupported_engine")
    else:
        transcription = disabled_probe_status("", reason="engine_disabled")

    operation_engines = {
        "speech": engine,
        "embeddings": embedding_engine,
        "transcription": stt_engine,
    }
    placeholder_operations = [
        operation
        for operation, selected_engine in operation_engines.items()
        if capabilities.get(operation) and selected_engine in PLACEHOLDER_ENGINES
    ]
    unsupported_engines = [
        selected_engine
        for selected_engine in operation_engines.values()
        if selected_engine and selected_engine not in SUPPORTED_ENGINES
    ]
    build_info = audio_cpu_build_info()
    residency = cpu_residency_details()
    available = any(capabilities.values())
    ready = bool(
        available
        and build_info.get("status") == "ok"
        and not placeholder_operations
        and not unsupported_engines
        and residency.get("headroom", {}).get("ok") is True
    )
    status = "ok" if ready else "degraded" if available else "unconfigured"
    return {
        "status": status,
        "runtime": "audio-cpu",
        "action": "status",
        "gpu_lease_required": False,
        "capabilities": {
            "actions": list(AUDIO_CPU_LIFECYCLE_ACTIONS),
            "operations": capabilities,
        },
        "engines": {
            "speech": speech,
            "embeddings": embeddings,
            "transcription": transcription,
        },
        "placeholder": {
            "enabled": placeholder_enabled(),
            "operations": placeholder_operations,
        },
        "unsupported_engines": unsupported_engines,
        "cpu_residency": residency,
        "build_info": build_info,
    }


def silence_wav(duration_seconds: float = 0.25, sample_rate: int = 16000) -> bytes:
    frames = int(duration_seconds * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


async def json_body(request: Request) -> dict[str, Any]:
    with suppress(Exception):
        payload = await request.json()
        if isinstance(payload, dict):
            return payload
    return {}


def process_peak_ram_mib() -> int:
    with suppress(Exception):
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if value > 0:
            if sys.platform == "darwin":
                return max(1, math.ceil(value / (1024 * 1024)))
            return max(1, math.ceil(value / 1024))
    return 1


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    engine = configured_engine()
    placeholder = engine in PLACEHOLDER_ENGINES
    capabilities = operation_capabilities()
    available = any(capabilities.values())
    return {
        "status": "ok" if available else "unconfigured",
        "service": "audio-cpu",
        "gpu_lease_required": False,
        "engine": engine,
        "placeholder": placeholder,
        "placeholder_enabled": placeholder_enabled(),
        "capabilities": capabilities,
        "details": engine_details(),
    }


@app.get("/b1/runtime/build-info")
async def runtime_build_info_get(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    return audio_cpu_build_info()


@app.post("/b1/runtime/build-info")
async def runtime_build_info(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    return audio_cpu_build_info()


@app.get("/b1/runtime/status")
async def runtime_status_get(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    return audio_cpu_runtime_status()


@app.post("/b1/runtime/status")
async def runtime_status(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    return audio_cpu_runtime_status()


@app.post("/b1/runtime/unload")
async def runtime_unload(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    payload = await json_body(request)
    cache = clear_resident_caches()
    return {
        "status": "ok",
        "runtime": "audio-cpu",
        "action": "unload",
        "gpu_lease_required": False,
        "model": payload.get("model"),
        "model_alias": payload.get("model_alias") or payload.get("b1_model_alias"),
        "resolved_model_version": payload.get("resolved_model_version") or payload.get("b1_resolved_model_version"),
        "details": {
            "cpu_residency": cpu_residency_details(),
            "cache": cache,
        },
    }


@app.post("/b1/runtime/smoke")
async def runtime_smoke(request: Request) -> Any:
    auth_failure = runtime_control_auth_failure(getattr(request, "headers", {}))
    if auth_failure is not None:
        status, payload = auth_failure
        return JSONResponse(payload, status_code=status)
    payload = await json_body(request)
    engine = configured_engine()
    modality = str(payload.get("modality") or "").lower()
    if modality == "embedding":
        reported_engine = configured_embedding_engine()
    elif modality == "stt":
        reported_engine = configured_stt_engine()
    else:
        reported_engine = engine
    placeholder = reported_engine in PLACEHOLDER_ENGINES
    if not engine_available(modality or None, payload):
        return {
            "status": "unconfigured",
            "reason": "engine_unavailable",
            "action": "smoke",
            "runtime": "audio-cpu",
            "model": payload.get("model"),
            "model_alias": payload.get("model_alias"),
            "resolved_model_version": payload.get("resolved_model_version"),
            "gpu_lease_required": False,
            "engine": reported_engine,
            "placeholder": placeholder,
            "details": engine_details(),
            "measurements": {
                "peak_vram_mib": 0,
                "peak_ram_mib": process_peak_ram_mib(),
                "placeholder": placeholder,
            },
        }

    measurements: dict[str, Any] = {
        "peak_vram_mib": 0,
        "peak_ram_mib": process_peak_ram_mib(),
        "placeholder": placeholder,
    }
    if modality == "embedding":
        if configured_embedding_engine() == ONNX_EMBEDDING_ENGINE:
            try:
                vector = onnx_embedding_vectors(["b1-ai-hub smoke"], payload)[0]
            except AudioCpuError as exc:
                return {
                    "status": "failed",
                    "reason": exc.code,
                    "action": "smoke",
                    "runtime": "audio-cpu",
                    "model": payload.get("model"),
                    "model_alias": payload.get("model_alias"),
                    "resolved_model_version": payload.get("resolved_model_version"),
                    "gpu_lease_required": False,
                    "engine": ONNX_EMBEDDING_ENGINE,
                    "placeholder": False,
                    "measurements": measurements,
                }
        else:
            vector = hashed_embedding("b1-ai-hub smoke", 16)
        measurements["embedding_dimensions"] = len(vector)
        measurements["embedding_nonzero"] = any(value != 0 for value in vector)
    elif modality == "stt":
        if configured_stt_engine() == VOSK_STT_ENGINE:
            try:
                smoke_payload = {
                    **payload,
                    "audio": base64.b64encode(silence_wav(0.25)).decode("ascii"),
                    "audio_mime_type": "audio/wav",
                }
                transcript = vosk_transcription(smoke_payload)
            except AudioCpuError as exc:
                return {
                    "status": "failed",
                    "reason": exc.code,
                    "action": "smoke",
                    "runtime": "audio-cpu",
                    "model": payload.get("model"),
                    "model_alias": payload.get("model_alias"),
                    "resolved_model_version": payload.get("resolved_model_version"),
                    "gpu_lease_required": False,
                    "engine": VOSK_STT_ENGINE,
                    "placeholder": False,
                    "measurements": measurements,
                }
            measurements["transcript_chars"] = len(str(transcript.get("text") or ""))
            measurements["audio_seconds"] = transcript.get("duration_seconds")
        else:
            transcript = "B1 AI Hub CPU transcription scaffold."
            measurements["transcript_chars"] = len(transcript)
    elif engine == PIPER_ENGINE:
        try:
            measurements["speech_bytes"] = len(piper_speech_bytes("B1 AI Hub CPU audio smoke.", payload))
        except AudioCpuError as exc:
            return {
                "status": "failed",
                "reason": exc.code,
                "action": "smoke",
                "runtime": "audio-cpu",
                "model": payload.get("model"),
                "model_alias": payload.get("model_alias"),
                "resolved_model_version": payload.get("resolved_model_version"),
                "gpu_lease_required": False,
                "engine": engine,
                "placeholder": placeholder,
                "measurements": measurements,
            }
    else:
        measurements["speech_bytes"] = len(silence_wav(0.25))

    return {
        "status": "ok",
        "action": "smoke",
        "runtime": "audio-cpu",
        "model": payload.get("model"),
        "model_alias": payload.get("model_alias"),
        "resolved_model_version": payload.get("resolved_model_version"),
        "gpu_lease_required": False,
        "engine": reported_engine,
        "placeholder": placeholder,
        "measurements": measurements,
    }


def piper_speech_bytes(text: str, payload: dict[str, Any] | None = None) -> bytes:
    status = piper_status(payload)
    if not status["available"]:
        raise AudioCpuError(f"piper is not configured: {status.get('reason') or 'unknown'}")
    max_chars = int(status["max_text_chars"])
    if len(text) > max_chars:
        raise AudioCpuError(
            f"speech input exceeds {max_chars} characters",
            code="b1_audio_cpu_input_too_large",
            status_code=413,
        )
    output_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="b1-piper-", suffix=".wav", delete=False) as output:
            output_path = Path(output.name)
        command = [
            str(status["binary"]),
            "--model",
            str(status["model_path"]),
            "--output_file",
            str(output_path),
        ]
        if status.get("config_path"):
            command.extend(["--config", str(status["config_path"])])
        speaker = os.getenv("B1_PIPER_SPEAKER")
        if speaker:
            command.extend(["--speaker", speaker.strip()])
        completed = subprocess.run(
            command,
            check=False,
            input=text,
            capture_output=True,
            text=True,
            timeout=float(status["timeout_seconds"]),
        )
        if completed.returncode != 0:
            raise AudioCpuError(f"piper exited with status {completed.returncode}")
        if output_path is None or not output_path.is_file():
            raise AudioCpuError("piper did not create an output file")
        content = output_path.read_bytes()
        if len(content) <= 44:
            raise AudioCpuError("piper returned an empty audio file")
        return content
    except subprocess.TimeoutExpired as exc:
        raise AudioCpuError("piper timed out") from exc
    except OSError as exc:
        raise AudioCpuError(f"piper execution failed: {exc.__class__.__name__}") from exc
    finally:
        if output_path is not None:
            with suppress(OSError):
                output_path.unlink()


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def base64_audio_from_payload(payload: dict[str, Any], *, max_audio_bytes: int) -> tuple[bytes, str | None]:
    raw = payload.get("audio")
    if raw is None:
        raw = payload.get("file")
    if raw is None:
        raw = payload.get("audio_base64")
    if not isinstance(raw, str) or not raw.strip():
        raise AudioCpuError(
            "transcription requires a base64-encoded PCM WAV audio field",
            code="b1_audio_cpu_audio_missing",
            status_code=422,
        )
    value = raw.strip()
    declared_mime_type: str | None = None
    if value.startswith("data:") and "," in value:
        header, value = value.split(",", 1)
        declared_mime_type = header[5:].split(";", 1)[0] or None
    try:
        content = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise AudioCpuError(
            "transcription audio must be valid base64",
            code="b1_audio_cpu_audio_base64_invalid",
            status_code=422,
        ) from exc
    if len(content) > max_audio_bytes:
        raise AudioCpuError(
            f"transcription audio exceeds {max_audio_bytes} bytes",
            code="b1_audio_cpu_audio_too_large",
            status_code=413,
        )
    return content, declared_mime_type or payload.get("audio_mime_type")


def mp3_to_pcm16_mono_wav(content: bytes, *, max_audio_seconds: int) -> bytes:
    """Decode a bounded MP3 upload without exposing a shell or host path."""
    max_output_bytes = 44 + max_audio_seconds * 16000 * 2
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-nostdin", "-i", "pipe:0", "-map_metadata", "-1",
                "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
            ],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(120, max(15, max_audio_seconds // 4)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioCpuError("transcription MP3 decode timed out", code="b1_audio_cpu_mp3_decode_timeout", status_code=422) from exc
    except OSError as exc:
        raise AudioCpuError("transcription MP3 decoder is unavailable", code="b1_audio_cpu_mp3_decoder_unavailable") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise AudioCpuError("transcription audio/mpeg input could not be decoded", code="b1_audio_cpu_mp3_invalid", status_code=415)
    if len(completed.stdout) + 44 > max_output_bytes:
        raise AudioCpuError(f"transcription audio exceeds {max_audio_seconds} seconds", code="b1_audio_cpu_audio_duration_too_large", status_code=413)
    pcm = completed.stdout
    return (
        b"RIFF"
        + (36 + len(pcm)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (16000).to_bytes(4, "little")
        + (32000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(pcm).to_bytes(4, "little")
        + pcm
    )


def normalize_streaming_wav_sizes(content: bytes) -> bytes:
    """Replace streaming RIFF/data length sentinels with bounded input lengths."""
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        return content
    normalized = bytearray(content)
    if normalized[4:8] == b"\xff\xff\xff\xff":
        normalized[4:8] = (len(normalized) - 8).to_bytes(4, "little")
    offset = 12
    while offset + 8 <= len(normalized):
        chunk_type = bytes(normalized[offset : offset + 4])
        chunk_size = int.from_bytes(normalized[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        if chunk_type == b"data" and chunk_size == 0xFFFFFFFF:
            if data_start >= len(normalized):
                return content
            normalized[offset + 4 : offset + 8] = (len(normalized) - data_start).to_bytes(4, "little")
            return bytes(normalized)
        data_end = data_start + chunk_size
        if data_end > len(normalized):
            return content
        offset = data_end + (chunk_size % 2)
    return bytes(normalized)


def pcm16_mono_wav_from_bytes(content: bytes, *, max_audio_seconds: int, mime_type: str | None = None) -> tuple[bytes, int, float]:
    normalized_mime_type = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime_type == "audio/mpeg":
        content = mp3_to_pcm16_mono_wav(content, max_audio_seconds=max_audio_seconds)
    elif normalized_mime_type in {"audio/wav", "audio/x-wav", ""}:
        content = normalize_streaming_wav_sizes(content)
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            duration = frames / float(sample_rate or 1)
            if channels != 1:
                raise AudioCpuError(
                    "transcription WAV input must be mono",
                    code="b1_audio_cpu_wav_channels_unsupported",
                    status_code=415,
                )
            if sample_width != 2:
                raise AudioCpuError(
                    "transcription WAV input must be signed 16-bit PCM",
                    code="b1_audio_cpu_wav_sample_width_unsupported",
                    status_code=415,
                )
            if sample_rate < 8000 or sample_rate > 48000:
                raise AudioCpuError(
                    "transcription WAV sample rate must be between 8000 and 48000 Hz",
                    code="b1_audio_cpu_wav_sample_rate_unsupported",
                    status_code=415,
                )
            if duration > max_audio_seconds:
                raise AudioCpuError(
                    f"transcription audio exceeds {max_audio_seconds} seconds",
                    code="b1_audio_cpu_audio_duration_too_large",
                    status_code=413,
                )
            pcm = wav.readframes(frames)
    except AudioCpuError:
        raise
    except (EOFError, wave.Error) as exc:
        raise AudioCpuError(
            "transcription audio must be a readable PCM WAV file",
            code="b1_audio_cpu_wav_invalid",
            status_code=415,
        ) from exc
    if not pcm:
        raise AudioCpuError(
            "transcription audio is empty",
            code="b1_audio_cpu_audio_empty",
            status_code=422,
        )
    return pcm, sample_rate, duration


def create_vosk_model(model_path: str) -> Any:
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    return Model(model_path)


@lru_cache(maxsize=2)
def vosk_model(model_path: str) -> Any:
    return create_vosk_model(model_path)


def vosk_model_for_payload(model_path: str, payload: dict[str, Any] | None) -> Any:
    if cpu_residency_allowed(payload):
        return vosk_model(model_path)
    clear_resident_caches()
    return create_vosk_model(model_path)


def vosk_result_parts(raw: str) -> tuple[str, list[dict[str, Any]]]:
    parsed = parse_json_object(raw)
    text = str(parsed.get("text") or "").strip()
    words = parsed.get("result")
    return text, words if isinstance(words, list) else []


def vosk_transcription(payload: dict[str, Any]) -> dict[str, Any]:
    status = vosk_stt_status(payload)
    if not status["available"]:
        raise AudioCpuError(f"Vosk STT is not configured: {status.get('reason') or 'unknown'}")
    audio_bytes, mime_type = base64_audio_from_payload(payload, max_audio_bytes=int(status["max_audio_bytes"]))
    pcm, sample_rate, duration_seconds = pcm16_mono_wav_from_bytes(
        audio_bytes,
        max_audio_seconds=int(status["max_audio_seconds"]),
        mime_type=mime_type,
    )
    try:
        from vosk import KaldiRecognizer
    except ModuleNotFoundError as exc:
        raise AudioCpuError("Vosk dependency is missing", code="b1_audio_cpu_stt_dependency_missing") from exc

    recognizer = KaldiRecognizer(vosk_model_for_payload(str(status["model_path"]), payload), float(sample_rate))
    recognizer.SetWords(bool(status["words"]))
    text_parts: list[str] = []
    word_results: list[dict[str, Any]] = []
    for offset in range(0, len(pcm), 8000):
        chunk = pcm[offset : offset + 8000]
        if recognizer.AcceptWaveform(chunk):
            text, words = vosk_result_parts(recognizer.Result())
            if text:
                text_parts.append(text)
            word_results.extend(words)
    final_text, final_words = vosk_result_parts(recognizer.FinalResult())
    if final_text:
        text_parts.append(final_text)
    word_results.extend(final_words)
    text = " ".join(part for part in text_parts if part).strip()
    response: dict[str, Any] = {
        "text": text,
        "duration_seconds": round(duration_seconds, 3),
        "language": status["language"],
        "gpu_lease_required": False,
        "b1_engine": VOSK_STT_ENGINE,
        "b1_stt_engine": "vosk",
        "b1_placeholder": False,
        "b1_audio_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "channels": 1,
            "sample_rate_hz": sample_rate,
            "mime_type": mime_type or "audio/wav",
        },
    }
    if bool(status["words"]):
        response["words"] = word_results
    return response


@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    payload = await json_body(request)
    if not engine_available("speech", payload):
        return unavailable_response("speech")
    text = payload.get("input") or payload.get("text") or ""
    if configured_engine() == PIPER_ENGINE:
        try:
            content = piper_speech_bytes(str(text), payload)
        except AudioCpuError as exc:
            return engine_failure_response(exc)
        return Response(
            content=content,
            media_type="audio/wav",
            headers={
                "X-B1-Placeholder": "false",
                "X-B1-CPU-Audio-Engine": configured_engine(),
                "X-B1-GPU-Lease-Required": "false",
            },
        )
    duration = min(2.0, max(0.25, len(str(text)) / 80.0))
    return Response(
        content=silence_wav(duration),
        media_type="audio/wav",
        headers={
            "X-B1-Placeholder": "true",
            "X-B1-CPU-Audio-Engine": configured_engine(),
            "X-B1-GPU-Lease-Required": "false",
        },
    )


@app.post("/v1/audio/transcriptions")
async def transcription(request: Request) -> Any:
    payload = await json_body(request)
    if not engine_available("transcription", payload):
        return unavailable_response("transcription")
    if configured_stt_engine() == VOSK_STT_ENGINE:
        try:
            return vosk_transcription(payload)
        except AudioCpuError as exc:
            return engine_failure_response(exc, "transcription")
    audio = payload.get("audio")
    suffix = f" Received {len(audio)} base64 character(s)." if isinstance(audio, str) else ""
    return {
        "text": f"B1 AI Hub CPU transcription scaffold.{suffix}",
        "gpu_lease_required": False,
        "b1_engine": configured_stt_engine(),
        "b1_placeholder": True,
    }


def hashed_embedding(text: str, dimensions: int) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.blake2b(f"{counter}:{text}".encode("utf-8", errors="ignore"), digest_size=32).digest()
        values.extend((byte / 127.5) - 1.0 for byte in digest)
        counter += 1
    vector = values[:dimensions]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 8) for value in vector]


def create_onnx_embedding_session(model_path: str) -> Any:
    import onnxruntime as ort

    return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=4)
def onnx_embedding_session(model_path: str) -> Any:
    return create_onnx_embedding_session(model_path)


def onnx_embedding_session_for_payload(model_path: str, payload: dict[str, Any] | None) -> Any:
    if cpu_residency_allowed(payload):
        return onnx_embedding_session(model_path)
    clear_resident_caches()
    return create_onnx_embedding_session(model_path)


def resident_cache_state() -> dict[str, dict[str, int]]:
    vosk_info = vosk_model.cache_info()
    embedding_info = onnx_embedding_session.cache_info()
    return {
        "vosk": {"maxsize": int(vosk_info.maxsize or 0), "currsize": int(vosk_info.currsize)},
        "onnx_embedding": {"maxsize": int(embedding_info.maxsize or 0), "currsize": int(embedding_info.currsize)},
    }


def clear_resident_caches() -> dict[str, Any]:
    before = resident_cache_state()
    vosk_model.cache_clear()
    onnx_embedding_session.cache_clear()
    return {"before": before, "after": resident_cache_state()}


def onnx_input_array(name: str, input_type: str, encodings: list[Any]) -> Any:
    import numpy as np

    dtype = np.int32 if "int32" in input_type else np.int64
    if name == "input_ids":
        return np.array([encoding.ids for encoding in encodings], dtype=dtype)
    if name == "attention_mask":
        return np.array([encoding.attention_mask for encoding in encodings], dtype=dtype)
    if name == "token_type_ids":
        return np.array([encoding.type_ids for encoding in encodings], dtype=dtype)
    raise AudioCpuError(f"unsupported ONNX embedding input: {name}", code="b1_audio_cpu_embedding_input_unsupported")


def normalize_embeddings(array: Any) -> Any:
    import numpy as np

    norm = np.linalg.norm(array, axis=1, keepdims=True)
    norm = np.where(norm == 0, 1.0, norm)
    return array / norm


def pooled_onnx_embeddings(output: Any, attention_mask: Any, *, normalize: bool) -> Any:
    import numpy as np

    embeddings = output
    if embeddings.ndim == 3:
        mask = attention_mask.astype(np.float32)
        masked = embeddings * mask[:, :, None]
        counts = np.clip(mask.sum(axis=1, keepdims=True), 1.0, None)
        embeddings = masked.sum(axis=1) / counts
    if embeddings.ndim != 2:
        raise AudioCpuError("ONNX embedding output must be rank 2 or rank 3", code="b1_audio_cpu_embedding_output_invalid")
    if normalize:
        embeddings = normalize_embeddings(embeddings)
    return embeddings


def requested_embedding_dimensions(payload: dict[str, Any], actual_dimensions: int) -> int:
    raw = payload.get("dimensions")
    if raw is None:
        return actual_dimensions
    if not isinstance(raw, int):
        raise AudioCpuError("embedding dimensions must be an integer", code="b1_audio_cpu_embedding_dimensions_invalid", status_code=422)
    return max(8, min(raw, actual_dimensions))


def onnx_embedding_vectors(texts: list[str], payload: dict[str, Any] | None = None) -> list[list[float]]:
    payload = payload or {}
    status = onnx_embedding_status(payload)
    if not status["available"]:
        raise AudioCpuError(f"ONNX embeddings are not configured: {status.get('reason') or 'unknown'}")
    if len(texts) > int(status["max_batch"]):
        raise AudioCpuError(
            f"embedding batch exceeds {status['max_batch']} item(s)",
            code="b1_audio_cpu_embedding_batch_too_large",
            status_code=413,
        )
    too_large = [index for index, text in enumerate(texts) if len(text) > int(status["max_text_chars"])]
    if too_large:
        raise AudioCpuError(
            f"embedding input item {too_large[0]} exceeds {status['max_text_chars']} characters",
            code="b1_audio_cpu_embedding_input_too_large",
            status_code=413,
        )
    try:
        import numpy as np
        from tokenizers import Tokenizer
    except ModuleNotFoundError as exc:
        raise AudioCpuError(f"ONNX embedding dependency is missing: {exc.name}", code="b1_audio_cpu_embedding_dependency_missing") from exc

    tokenizer = Tokenizer.from_file(str(status["tokenizer_path"]))
    tokenizer.enable_truncation(max_length=int(status["max_tokens"]))
    tokenizer.enable_padding()
    encodings = tokenizer.encode_batch(texts)
    session = onnx_embedding_session_for_payload(str(status["model_path"]), payload)
    feed: dict[str, Any] = {}
    for item in session.get_inputs():
        feed[item.name] = onnx_input_array(item.name, item.type, encodings)
    if "attention_mask" not in feed:
        raise AudioCpuError("ONNX embedding model must accept attention_mask", code="b1_audio_cpu_embedding_attention_missing")
    outputs = session.run(None, feed)
    if not outputs:
        raise AudioCpuError("ONNX embedding model returned no outputs", code="b1_audio_cpu_embedding_output_missing")
    embeddings = pooled_onnx_embeddings(outputs[0], feed["attention_mask"], normalize=bool(status["normalize"]))
    dimensions = requested_embedding_dimensions(payload, int(embeddings.shape[1]))
    if dimensions < embeddings.shape[1]:
        embeddings = embeddings[:, :dimensions]
        if bool(status["normalize"]):
            embeddings = normalize_embeddings(embeddings)
    embeddings = np.asarray(embeddings, dtype=np.float32)
    return [[round(float(value), 8) for value in row.tolist()] for row in embeddings]


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    payload = await json_body(request)
    if not engine_available("embeddings", payload):
        return unavailable_response("embeddings")
    raw_input = payload.get("input", "")
    items = raw_input if isinstance(raw_input, list) else [raw_input]
    dimensions = payload.get("dimensions", 384)
    if not isinstance(dimensions, int):
        dimensions = 384
    dimensions = max(8, min(dimensions, 1536))
    if configured_embedding_engine() == ONNX_EMBEDDING_ENGINE:
        try:
            vectors = onnx_embedding_vectors([str(item) for item in items], payload)
        except AudioCpuError as exc:
            return engine_failure_response(exc)
        return {
            "object": "list",
            "model": payload.get("model", "embedding-default"),
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": vector,
                }
                for index, vector in enumerate(vectors)
            ],
            "usage": {
                "prompt_tokens": sum(len(str(item).split()) for item in items),
                "total_tokens": sum(len(str(item).split()) for item in items),
            },
            "gpu_lease_required": False,
            "b1_embedding_engine": "onnxruntime",
            "b1_engine": ONNX_EMBEDDING_ENGINE,
            "b1_placeholder": False,
            "b1_embedding_dimensions": len(vectors[0]) if vectors else 0,
        }
    return {
        "object": "list",
        "model": payload.get("model", "embedding-default"),
        "data": [
            {
                "object": "embedding",
                "index": index,
                "embedding": hashed_embedding(str(item), dimensions),
            }
            for index, item in enumerate(items)
        ],
        "usage": {
            "prompt_tokens": sum(len(str(item).split()) for item in items),
            "total_tokens": sum(len(str(item).split()) for item in items),
        },
        "gpu_lease_required": False,
        "b1_embedding_engine": "deterministic-hash-placeholder",
        "b1_engine": configured_engine(),
        "b1_placeholder": True,
    }
