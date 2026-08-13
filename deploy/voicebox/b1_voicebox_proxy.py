#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import hmac
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

WEBSOCKET_HANDSHAKE_HEADERS = {
    "connection",
    "host",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    "upgrade",
}
VOICE_PROFILE_UPSTREAM_FIELD_MAP = {
    "upstream_voice": "voice",
    "upstream_voice_id": "voice",
    "upstream_profile": "profile",
    "upstream_profile_id": "profile_id",
    "upstream_speaker": "speaker",
    "upstream_speaker_id": "speaker_id",
    "language": "language",
    "style": "style",
    "speed": "speed",
}
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
VOICEBOX_UPSTREAM_REPOSITORY = "jamiepine/voicebox"
VOICEBOX_UPSTREAM_VERSION_DEFAULT = "v0.5.0"
VOICEBOX_UPSTREAM_COMMIT_DEFAULT = "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d"
VOICEBOX_SOURCE_ARCHIVE_SHA256_DEFAULT = "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083"
VOICEBOX_PROXY_VERSION_DEFAULT = "b1-voicebox-proxy/v0.5.0-b1"
VOICEBOX_LIFECYCLE_ACTIONS = ["status", "build-info", "load", "warm", "smoke", "unload"]
VOICEBOX_CLONE_ENGINES = {"qwen", "luxtts", "chatterbox", "chatterbox_turbo", "tada"}
VOICEBOX_LANGUAGES = {
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "ru",
    "pt",
    "es",
    "it",
    "he",
    "ar",
    "da",
    "el",
    "fi",
    "hi",
    "ms",
    "nl",
    "no",
    "pl",
    "sv",
    "sw",
    "tr",
}
VOICEBOX_PROFILE_MAP_LOCK = threading.Lock()
VOICE_ALIGNMENT_LOCK = threading.Lock()
VOICE_ALIGNMENT_CACHE: dict[str, Any] = {}


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


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
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
    payload: dict[str, Any] = {"status": status, "runtime": "voicebox", "action": action}
    payload.update(extra)
    return payload


def normalized_git_sha(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if GIT_SHA_RE.fullmatch(candidate) else ""


def normalized_sha256(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if SHA256_RE.fullmatch(candidate) else ""


def voicebox_build_info() -> dict[str, Any]:
    upstream_version = os.getenv("B1_VOICEBOX_UPSTREAM_VERSION", VOICEBOX_UPSTREAM_VERSION_DEFAULT).strip()
    upstream_commit = normalized_git_sha(os.getenv("B1_VOICEBOX_UPSTREAM_COMMIT", VOICEBOX_UPSTREAM_COMMIT_DEFAULT))
    source_archive_sha256 = normalized_sha256(
        os.getenv("B1_VOICEBOX_SOURCE_ARCHIVE_SHA256", VOICEBOX_SOURCE_ARCHIVE_SHA256_DEFAULT)
    )
    proxy_version = os.getenv("B1_VOICEBOX_PROXY_VERSION", VOICEBOX_PROXY_VERSION_DEFAULT).strip()
    status = "ok" if upstream_version and upstream_commit and source_archive_sha256 and proxy_version else "unconfigured"
    return {
        "status": status,
        "runtime": "voicebox",
        "action": "build-info",
        "proxy": "b1-voicebox-proxy",
        "proxy_version": proxy_version,
        "upstream_repository": VOICEBOX_UPSTREAM_REPOSITORY,
        "upstream_version": upstream_version,
        "upstream_commit": upstream_commit,
        "source_archive_sha256": source_archive_sha256,
        "pinned": True,
        "capabilities": {
            "actions": VOICEBOX_LIFECYCLE_ACTIONS,
            "openai_speech_bridge": True,
            "voice_cloning": True,
            "clone_engines": sorted(VOICEBOX_CLONE_ENGINES),
            "default_clone_engine": "chatterbox",
        },
    }


def voicebox_model_inventory() -> dict[str, Any]:
    roots = configured_model_roots()
    max_entries = env_int("B1_VOICEBOX_STATUS_MODEL_LIST_MAX_ENTRIES", 2000)
    entry_count = 0
    available_root_count = 0
    truncated = False
    for root in roots:
        if root.exists():
            available_root_count += 1
        entries = iter_model_entries((root,), max_entries=max_entries)
        entry_count += len(entries)
        if len(entries) >= max_entries:
            truncated = True
    return {
        "root_count": len(roots),
        "available_root_count": available_root_count,
        "entry_count": entry_count,
        "truncated": truncated,
        "strict_model_list": env_bool("B1_VOICEBOX_HOOK_STRICT_MODEL_LIST", False),
    }


def voicebox_status(manager: "VoiceboxProcessManager", tracker: "NativeRequestTracker") -> dict[str, Any]:
    process = manager.status()
    build_info = voicebox_build_info()
    model_inventory = voicebox_model_inventory()
    process_running = process.get("running") is True
    status = "ok" if process_running and build_info.get("status") == "ok" else "unhealthy"
    return json_response(
        status,
        "status",
        process=process,
        active_requests=tracker.active(),
        model_inventory=model_inventory,
        build_info=build_info,
        capabilities={
            "actions": VOICEBOX_LIFECYCLE_ACTIONS,
            "native_http_passthrough": True,
            "native_websocket_passthrough": True,
            "openai_speech_bridge": True,
            "voice_profile_envelope": True,
            "sample_path_forwarding": env_bool("B1_VOICEBOX_FORWARD_SAMPLE_PATHS", True),
            "clone_engines": sorted(VOICEBOX_CLONE_ENGINES),
        },
    )


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


def configured_model_roots() -> tuple[Path, ...]:
    raw = os.getenv("B1_VOICEBOX_HOOK_MODEL_ROOTS", "").strip()
    if not raw:
        raw = os.getenv("VOICEBOX_MODELS_DIR", "/srv/b1-ai-hub/models")
    roots = tuple(Path(part.strip()) for part in raw.replace(",", " ").split() if part.strip())
    return roots or (Path("/srv/b1-ai-hub/models"),)


def iter_model_entries(roots: tuple[Path, ...] | None = None, max_entries: int | None = None) -> list[str]:
    entries: list[str] = []
    limit = max_entries if max_entries is not None else env_int("B1_VOICEBOX_HOOK_MODEL_LIST_MAX_ENTRIES", 20000)
    for root in roots or configured_model_roots():
        if not root.exists():
            continue
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            root_resolved = root
        for current_root, dirs, files in os.walk(root, followlinks=False):
            current = Path(current_root)
            names = sorted(dirs) + sorted(files)
            for name in names:
                candidate = current / name
                try:
                    rel = candidate.relative_to(root)
                except ValueError:
                    try:
                        rel = candidate.resolve(strict=False).relative_to(root_resolved)
                    except (OSError, ValueError):
                        continue
                entries.append(rel.as_posix())
                if len(entries) >= limit:
                    return entries
    return entries


def model_available(candidates: list[str], entries: list[str]) -> bool:
    wanted: set[str] = set()
    for candidate in candidates:
        wanted.update(normalized_model_tokens(candidate))
    if not wanted:
        return False
    for entry in entries:
        for token in normalized_model_tokens(entry):
            if token in wanted:
                return True
    return False


def check_model_list(payload: dict[str, Any], action: str) -> dict[str, Any] | None:
    candidates = payload_model_candidates(payload)
    if not candidates:
        return json_response("unconfirmed", action, reason="model_missing")
    entries = iter_model_entries()
    if entries and model_available(candidates, entries):
        return None
    if env_bool("B1_VOICEBOX_HOOK_STRICT_MODEL_LIST", False):
        return json_response("unconfigured", action, reason="model_not_listed", model_entry_count=len(entries))
    return json_response("unconfirmed", action, reason="model_not_listed", model_entry_count=len(entries))


class NativeRequestTracker:
    def __init__(self) -> None:
        self._active = 0
        self._lock = threading.Lock()

    def begin(self) -> None:
        with self._lock:
            self._active += 1

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def active(self) -> int:
        with self._lock:
            return self._active


class VoiceboxProcessManager:
    def __init__(self, command: list[str], cwd: str = "/app", env: dict[str, str] | None = None) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env or dict(os.environ)
        self.process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def start(self) -> int:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return int(self.process.pid)
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env)
            return int(self.process.pid)

    def stop(self, timeout_seconds: float) -> dict[str, Any]:
        with self._lock:
            process = self.process
        if process is None:
            return {"stopped": False, "reason": "not_started"}
        if process.poll() is not None:
            return {"stopped": False, "reason": "already_exited", "returncode": process.returncode}
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=timeout_seconds)
            return {"stopped": True, "signal": "SIGTERM", "returncode": process.returncode}
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            return {"stopped": True, "signal": "SIGKILL", "returncode": process.returncode}

    def restart(self, timeout_seconds: float, reason: str) -> dict[str, Any]:
        stopped = self.stop(timeout_seconds)
        pid = self.start()
        return {"strategy": "upstream_process_restart", "reason": reason, "stopped": stopped, "pid": pid}

    def status(self) -> dict[str, Any]:
        process = self.process
        if process is None:
            return {"running": False, "pid": None, "returncode": None}
        return {"running": process.poll() is None, "pid": process.pid, "returncode": process.returncode}


def default_upstream_command() -> list[str]:
    host = os.getenv("B1_VOICEBOX_UPSTREAM_HOST", "127.0.0.1")
    port = os.getenv("B1_VOICEBOX_UPSTREAM_PORT", "17494")
    data_dir = os.getenv("B1_VOICEBOX_DATA_DIR", "/srv/b1-ai-hub/voicebox")
    configured = os.getenv("B1_VOICEBOX_UPSTREAM_COMMAND", "").strip()
    if configured:
        return configured.split()
    return ["python", "-m", "backend.main", "--host", host, "--port", port, "--data-dir", data_dir]


def upstream_http_base_url() -> str:
    return os.getenv(
        "B1_VOICEBOX_UPSTREAM_URL",
        f"http://{os.getenv('B1_VOICEBOX_UPSTREAM_HOST', '127.0.0.1')}:{os.getenv('B1_VOICEBOX_UPSTREAM_PORT', '17494')}",
    ).rstrip("/")


def upstream_ws_base_url() -> str:
    http_url = upstream_http_base_url()
    if http_url.startswith("https://"):
        return f"wss://{http_url[len('https://') :]}"
    if http_url.startswith("http://"):
        return f"ws://{http_url[len('http://') :]}"
    return http_url


def voicebox_artifact_root() -> Path:
    return Path(os.getenv("B1_VOICEBOX_ARTIFACT_ROOT", "/srv/b1-ai-hub/artifacts")).resolve(strict=False)


def safe_runtime_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(stripped) > 256:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("data:", "file:", "http://", "https://")):
        return False
    return not any(ord(character) < 32 for character in stripped)


def request_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def request_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def request_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def safe_artifact_path_segments(relative_path: str) -> list[str]:
    segments: list[str] = []
    for raw_part in relative_path.split("/"):
        if not raw_part:
            continue
        if BAD_PERCENT_ESCAPE_RE.search(raw_part):
            raise ValueError("voice profile artifact path contains malformed percent escaping")
        try:
            part = unquote(raw_part, errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("voice profile artifact path contains invalid UTF-8 escaping") from exc
        if part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError("voice profile artifact path contains traversal or encoded separators")
        if any(ord(character) < 32 for character in part):
            raise ValueError("voice profile artifact path contains control bytes")
        segments.append(part)
    if not segments:
        raise ValueError("voice profile artifact path is empty")
    return segments


def local_voicebox_artifact_path(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("voice profile sample artifact must be an internal /artifacts URL")
    if not parsed.path.startswith("/artifacts/voicebox/"):
        raise ValueError("voice profile sample artifact must be under /artifacts/voicebox")
    relative = parsed.path.removeprefix("/artifacts/")
    root = voicebox_artifact_root()
    target = (root.joinpath(*safe_artifact_path_segments(relative))).resolve(strict=False)
    if target != root and root not in target.parents:
        raise ValueError("voice profile artifact path escapes the mounted artifact root")
    if env_bool("B1_VOICEBOX_REQUIRE_SAMPLE_PATH_EXISTS", True) and not target.is_file():
        raise ValueError("voice profile sample artifact is not mounted in the Voicebox container")
    return target.as_posix()


def profile_sample_paths(profile: dict[str, Any]) -> list[str]:
    if not env_bool("B1_VOICEBOX_FORWARD_SAMPLE_PATHS", True):
        return []
    samples = profile.get("sample_artifacts")
    if not isinstance(samples, list):
        return []
    paths: list[str] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        url = sample.get("url")
        if isinstance(url, str) and url.strip():
            paths.append(local_voicebox_artifact_path(url.strip()))
    return paths


def voicebox_data_dir() -> Path:
    return Path(os.getenv("B1_VOICEBOX_DATA_DIR", "/srv/b1-ai-hub/voicebox")).resolve(strict=False)


def voicebox_profile_map_path() -> Path:
    configured = os.getenv("B1_VOICEBOX_PROFILE_MAP_FILE", "").strip()
    if configured:
        return Path(configured).resolve(strict=False)
    return voicebox_data_dir() / "b1-profile-map.json"


def read_voicebox_profile_map() -> dict[str, Any]:
    path = voicebox_profile_map_path()
    with VOICEBOX_PROFILE_MAP_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return payload if isinstance(payload, dict) else {}


def write_voicebox_profile_map(payload: dict[str, Any]) -> None:
    path = voicebox_profile_map_path()
    with VOICEBOX_PROFILE_MAP_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)


def safe_voicebox_name_segment(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return candidate or fallback


def sample_reference_text(sample: dict[str, Any], index: int) -> str:
    value = sample.get("reference_text")
    if safe_runtime_scalar(value):
        return str(value).strip()[:1000]
    default = os.getenv("B1_VOICEBOX_DEFAULT_REFERENCE_TEXT", "B1 voice reference sample").strip()
    return (default or f"B1 voice reference sample {index + 1}")[:1000]


def profile_sample_items(profile: dict[str, Any]) -> list[dict[str, Any]]:
    if not env_bool("B1_VOICEBOX_FORWARD_SAMPLE_PATHS", True):
        return []
    samples = profile.get("sample_artifacts")
    if not isinstance(samples, list):
        return []
    items: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            continue
        url = sample.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        path = Path(local_voicebox_artifact_path(url.strip()))
        sha256 = str(sample.get("sha256") or "").strip().lower()
        if not SHA256_RE.fullmatch(sha256):
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        mime_type = str(sample.get("mime_type") or "audio/wav").strip()
        if not mime_type.startswith("audio/"):
            mime_type = "audio/wav"
        items.append(
            {
                "path": path,
                "sha256": sha256,
                "mime_type": mime_type,
                "reference_text": sample_reference_text(sample, index),
            }
        )
    return items


def voicebox_profile_digest(sample_items: list[dict[str, Any]]) -> str:
    joined = "\n".join(str(item.get("sha256") or "") for item in sample_items)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def native_profile_name(profile: dict[str, Any], engine: str, sample_items: list[dict[str, Any]]) -> str:
    profile_id = safe_voicebox_name_segment(str(profile.get("id") or "profile"), "profile")
    digest = voicebox_profile_digest(sample_items)
    return f"b1-{engine}-{profile_id[:48]}-{digest}"[:100].rstrip("-")


def selected_clone_engine(payload: dict[str, Any], profile: dict[str, Any] | None) -> str:
    raw = ""
    if isinstance(profile, dict):
        raw = str(profile.get("engine") or "").strip()
    if not raw:
        raw = str(payload.get("engine") or "").strip()
    engine = (raw or "chatterbox").lower()
    if engine == "chatterbox-tts":
        engine = "chatterbox"
    if engine not in VOICEBOX_CLONE_ENGINES:
        raise ValueError(f"Voicebox engine {engine!r} does not support cloned B1 voice profiles")
    return engine


def selected_language(payload: dict[str, Any], profile: dict[str, Any] | None) -> str:
    candidates: list[Any] = [payload.get("language")]
    if isinstance(profile, dict):
        upstream = profile.get("upstream") if isinstance(profile.get("upstream"), dict) else {}
        candidates.append(upstream.get("language") if isinstance(upstream, dict) else None)
    for value in candidates:
        if isinstance(value, str):
            language = value.strip().lower()
            if language in VOICEBOX_LANGUAGES:
                return language
    return os.getenv("B1_VOICEBOX_DEFAULT_LANGUAGE", "en").strip().lower() or "en"


def bounded_generation_int(payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def native_generation_payload(payload: dict[str, Any], profile: dict[str, Any] | None, native_profile_id: str, engine: str) -> dict[str, Any]:
    text = payload.get("input") if isinstance(payload.get("input"), str) else payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Voicebox speech bridge requires non-empty input text")
    body: dict[str, Any] = {
        "profile_id": native_profile_id,
        "text": text,
        "language": selected_language(payload, profile),
        "engine": engine,
        "normalize": payload.get("normalize", True) is not False,
        "max_chunk_chars": bounded_generation_int(payload, "max_chunk_chars", 800, 100, 5000),
        "crossfade_ms": bounded_generation_int(payload, "crossfade_ms", 50, 0, 500),
    }
    if isinstance(payload.get("seed"), int) and payload["seed"] >= 0:
        body["seed"] = payload["seed"]
    if safe_runtime_scalar(payload.get("instruct")):
        body["instruct"] = str(payload["instruct"]).strip()[:500]
    return body


def json_body_object(body: bytes, content_type: str) -> dict[str, Any] | None:
    if "json" not in content_type.lower():
        return None
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def accept_allows_wav(accept_header: str) -> bool:
    if not accept_header.strip():
        return True
    values = {item.split(";", 1)[0].strip().lower() for item in accept_header.split(",")}
    return "audio/wav" in values or "audio/*" in values or "*/*" in values


def broadcast_audio_policy(payload: dict[str, Any] | None, accept_header: str = "") -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not env_bool("B1_VOICEBOX_BROADCAST_AUDIO_ENABLED", True):
        return None
    explicit = payload.get("b1_broadcast_audio")
    normalize = request_bool(payload.get("normalize"), False)
    if explicit is not None and not request_bool(explicit, False):
        return None
    if explicit is None and not normalize:
        return None
    if not accept_allows_wav(accept_header):
        return None
    sample_rate = request_int(
        payload.get("b1_audio_sample_rate") or payload.get("output_sample_rate"),
        env_int("B1_VOICEBOX_BROADCAST_SAMPLE_RATE", 48000),
        8000,
        192000,
    )
    true_peak_ceiling = env_float("B1_VOICEBOX_BROADCAST_TRUE_PEAK_DBTP", -1.5)
    requested_true_peak = request_float(payload.get("b1_true_peak_dbtp"), true_peak_ceiling, -9.0, 0.0)
    true_peak_dbtp = min(requested_true_peak, true_peak_ceiling)
    return {
        "sample_rate": sample_rate,
        "channels": 1,
        "loudness_lufs": request_float(payload.get("b1_loudness_lufs"), env_float("B1_VOICEBOX_BROADCAST_LOUDNESS_LUFS", -18.0), -70.0, -5.0),
        "loudness_range_lu": request_float(payload.get("b1_loudness_range_lu"), env_float("B1_VOICEBOX_BROADCAST_LRA_LU", 11.0), 1.0, 50.0),
        "true_peak_dbtp": true_peak_dbtp,
        "limit_amplitude": min(1.0, max(0.0625, 10 ** (true_peak_dbtp / 20.0))),
    }


def broadcast_audio_headers(policy: dict[str, Any]) -> dict[str, str]:
    return {
        "content-type": "audio/wav",
        "x-b1-audio-policy": "broadcast",
        "x-b1-audio-sample-rate": str(policy["sample_rate"]),
        "x-b1-audio-channels": str(policy["channels"]),
        "x-b1-audio-loudness-lufs": str(policy["loudness_lufs"]),
        "x-b1-audio-true-peak-dbtp": str(policy["true_peak_dbtp"]),
    }


def broadcast_loudnorm_filter(policy: dict[str, Any], measurements: dict[str, Any] | None = None, *, print_format: str = "none") -> str:
    options = (
        f"aresample={int(policy['sample_rate'])},"
        f"loudnorm=I={float(policy['loudness_lufs'])}:"
        f"TP={float(policy['true_peak_dbtp'])}:"
        f"LRA={float(policy['loudness_range_lu'])}:"
    )
    if measurements:
        options += (
            f"measured_I={measurements['input_i']}:"
            f"measured_TP={measurements['input_tp']}:"
            f"measured_LRA={measurements['input_lra']}:"
            f"measured_thresh={measurements['input_thresh']}:"
            f"offset={measurements['target_offset']}:"
            "linear=true:"
        )
    else:
        options += "linear=false:"
    options += (
        f"dual_mono=true:print_format={print_format},"
        f"alimiter=limit={float(policy['limit_amplitude']):.6f}:attack=5:release=50:level=false"
    )
    return options


def parse_loudnorm_measurements(stderr: bytes) -> dict[str, Any] | None:
    text = stderr.decode("utf-8", errors="ignore")
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        return None
    return {key: str(payload[key]) for key in required}


def ffmpeg_run(content: bytes, args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def measure_loudnorm(content: bytes, policy: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    filters = broadcast_loudnorm_filter(policy, print_format="json")
    result = ffmpeg_run(
        content,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            filters,
            "-f",
            "null",
            "-",
        ],
        timeout,
    )
    if result.returncode != 0:
        return None
    return parse_loudnorm_measurements(result.stderr)


def render_broadcast_wav(content: bytes, policy: dict[str, Any], timeout: float, measurements: dict[str, Any] | None = None) -> bytes:
    filters = broadcast_loudnorm_filter(policy, measurements=measurements, print_format="none")
    result = ffmpeg_run(
        content,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            str(int(policy["channels"])),
            "-ar",
            str(int(policy["sample_rate"])),
            "-af",
            filters,
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ],
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode("utf-8", errors="ignore").strip()[:300]
        raise RuntimeError(error or "ffmpeg broadcast audio processing failed")
    return result.stdout


def postprocess_broadcast_wav(content: bytes, policy: dict[str, Any]) -> bytes:
    if not content:
        raise RuntimeError("empty upstream audio cannot be broadcast-normalized")
    timeout = env_float("B1_VOICEBOX_BROADCAST_FFMPEG_TIMEOUT_SECONDS", 120.0)
    measurements = measure_loudnorm(content, policy, timeout)
    return render_broadcast_wav(content, policy, timeout, measurements=measurements)


async def maybe_postprocess_broadcast_wav(content: bytes, policy: dict[str, Any] | None) -> bytes:
    if policy is None:
        return content
    return await asyncio.to_thread(postprocess_broadcast_wav, content, policy)


def voice_timing_dir() -> Path:
    return voicebox_data_dir() / "timing"


def audio_wav_properties(content: bytes) -> dict[str, int]:
    with wave.open(io.BytesIO(content), "rb") as handle:
        frames = handle.getnframes()
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
    if sample_rate <= 0:
        raise ValueError("WAV sample rate is invalid")
    bytes_per_frame = max(1, channels * sample_width)
    data_offset = content.find(b"data")
    if data_offset >= 0 and len(content) >= data_offset + 8:
        actual_data_bytes = max(0, len(content) - data_offset - 8)
        actual_frames = actual_data_bytes // bytes_per_frame
        if actual_frames > 0 and (frames <= 0 or frames > actual_frames * 2):
            frames = actual_frames
    return {
        "frames": int(frames),
        "sample_rate": int(sample_rate),
        "channels": int(channels),
        "sample_width": int(sample_width),
        "duration_ms": int(round((frames / sample_rate) * 1000)),
    }


def wav_to_mono_float_tensor(content: bytes, torch_module: Any) -> tuple[Any, int]:
    with wave.open(io.BytesIO(content), "rb") as handle:
        sample_rate = int(handle.getframerate())
        channels = int(handle.getnchannels())
        sample_width = int(handle.getsampwidth())
        frame_bytes = handle.readframes(handle.getnframes())
    if not frame_bytes or sample_rate <= 0 or channels <= 0:
        raise ValueError("WAV audio has no readable PCM frames")
    if sample_width == 2:
        waveform = torch_module.frombuffer(bytearray(frame_bytes), dtype=torch_module.int16).float() / 32768.0
    elif sample_width == 4:
        waveform = torch_module.frombuffer(bytearray(frame_bytes), dtype=torch_module.int32).float() / 2147483648.0
    elif sample_width == 1:
        waveform = (torch_module.frombuffer(bytearray(frame_bytes), dtype=torch_module.uint8).float() - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width {sample_width}")
    usable = (waveform.numel() // channels) * channels
    if usable <= 0:
        raise ValueError("WAV audio frame count is invalid")
    waveform = waveform[:usable].reshape(-1, channels).transpose(0, 1)
    if channels > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    return waveform.contiguous(), sample_rate


WORD_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ]+(?:[-'][\wÀ-ÖØ-öø-ÿ]+)?", re.UNICODE)


def timing_text_words(text: str) -> list[str]:
    return [match.group(0) for match in WORD_RE.finditer(text)]


def normalize_alignment_word(word: str) -> str:
    normalized = word.strip().lower()
    normalized = (
        normalized.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("æ", "ae")
        .replace("œ", "oe")
    )
    decomposed = unicodedata.normalize("NFKD", normalized)
    ascii_word = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z']", "", ascii_word)


def espeak_language(language: str) -> str:
    normalized = (language or "en").strip().lower().replace("_", "-")
    if normalized.startswith("de"):
        return "de"
    if normalized.startswith("en"):
        return "en-us"
    return normalized.split("-", 1)[0] or "en-us"


def fallback_word_phonemes(word: str) -> list[str]:
    return [char.lower() for char in word if char.isalnum()] or [word]


def phonemize_words(words: list[str], language: str) -> list[list[str]]:
    if not words:
        return []
    try:
        import espeakng_loader
        from phonemizer import phonemize
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        from phonemizer.separator import Separator

        tmp = voicebox_data_dir() / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(tmp)
        EspeakWrapper.set_library(str(espeakng_loader.get_library_path()))
        EspeakWrapper.set_data_path(str(espeakng_loader.get_data_path()))
        lines = phonemize(
            words,
            language=espeak_language(language),
            backend="espeak",
            strip=True,
            preserve_punctuation=False,
            with_stress=False,
            njobs=1,
            language_switch="remove-flags",
            words_mismatch="ignore",
            separator=Separator(phone=" ", word=" | ", syllable=""),
        )
    except Exception as exc:
        log_json(component="b1-voicebox-proxy", event="voice_timing_phonemizer_unavailable", error=exc.__class__.__name__)
        return [fallback_word_phonemes(word) for word in words]
    result: list[list[str]] = []
    for word, line in zip(words, lines, strict=False):
        cleaned = re.sub(r"[|‖]+", " ", str(line)).strip()
        phones = [item for item in cleaned.split() if item]
        result.append(phones or fallback_word_phonemes(word))
    while len(result) < len(words):
        result.append(fallback_word_phonemes(words[len(result)]))
    return result


def mms_alignment_enabled() -> bool:
    return env_bool("B1_VOICEBOX_TIMING_MMS_ALIGNER_ENABLED", True)


def mms_alignment_device() -> str:
    return os.getenv("B1_VOICEBOX_TIMING_MMS_ALIGNER_DEVICE", "cpu").strip().lower() or "cpu"


def mms_alignment_cache() -> tuple[Any, Any, Any, str]:
    device = mms_alignment_device()
    cache_key = f"mms-fa:{device}"
    with VOICE_ALIGNMENT_LOCK:
        cached = VOICE_ALIGNMENT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        import torch
        import torchaudio

        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        bundle = torchaudio.pipelines.MMS_FA
        model = bundle.get_model(with_star=False).to(device)
        model.eval()
        tokenizer = bundle.get_tokenizer()
        aligner = bundle.get_aligner()
        cached = (model, tokenizer, aligner, device)
        VOICE_ALIGNMENT_CACHE[cache_key] = cached
        return cached


def forced_align_words_mms(content: bytes, words: list[str]) -> dict[str, Any] | None:
    if not mms_alignment_enabled() or not words:
        return None
    normalized_words = [normalize_alignment_word(word) for word in words]
    if any(not word for word in normalized_words):
        return None
    try:
        import torch
        import torchaudio

        model, tokenizer, aligner, device = mms_alignment_cache()
        waveform, sample_rate = wav_to_mono_float_tensor(content, torch)
        if waveform.ndim != 2 or waveform.shape[0] < 1:
            return None
        target_sample_rate = int(torchaudio.pipelines.MMS_FA.sample_rate)
        if int(sample_rate) != target_sample_rate:
            waveform = torchaudio.functional.resample(waveform, int(sample_rate), target_sample_rate)
        waveform = waveform.to(device)
        tokens = tokenizer(normalized_words)
        with torch.inference_mode():
            emission, _ = model(waveform)
            token_spans = aligner(emission[0].cpu(), tokens)
        waveform_duration_ms = int(round((waveform.shape[1] / target_sample_rate) * 1000))
        ratio = waveform_duration_ms / max(1, emission.shape[1])
    except Exception as exc:
        log_json(component="b1-voicebox-proxy", event="voice_timing_mms_alignment_failed", error=exc.__class__.__name__)
        return None
    word_timestamps: list[dict[str, Any]] = []
    character_timestamps: list[dict[str, Any]] = []
    for word_index, (original_word, normalized_word, spans) in enumerate(zip(words, normalized_words, token_spans, strict=False)):
        if not spans:
            return None
        start_ms = int(round(spans[0].start * ratio))
        end_ms = int(round(spans[-1].end * ratio))
        if word_timestamps and start_ms < word_timestamps[-1]["end_ms"]:
            start_ms = int(word_timestamps[-1]["end_ms"])
        end_ms = max(start_ms, end_ms)
        scores = [float(getattr(span, "score", 0.0)) for span in spans]
        word_timestamps.append(
            {
                "word": original_word,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "confidence": round(max(0.0, min(1.0, sum(scores) / max(1, len(scores)))), 4),
            }
        )
        for character, span in zip(normalized_word, spans, strict=False):
            character_start_ms = int(round(span.start * ratio))
            character_end_ms = int(round(span.end * ratio))
            character_timestamps.append(
                {
                    "character": character,
                    "start_ms": max(start_ms, character_start_ms),
                    "end_ms": min(end_ms, max(character_start_ms, character_end_ms)),
                    "word_index": word_index,
                    "confidence": round(max(0.0, min(1.0, float(getattr(span, "score", 0.0)))), 4),
                }
            )
    return {
        "word_timestamps": word_timestamps,
        "character_timestamps": character_timestamps,
        "method": "torchaudio-mms-fa",
        "device": device,
    }


def timing_generation_id(payload: dict[str, Any], audio_sha256: str) -> str:
    identity = {
        "audio_sha256": audio_sha256,
        "engine": payload.get("engine"),
        "language": payload.get("language"),
        "profile_id": payload.get("profile_id") or payload.get("voice") or payload.get("speaker"),
        "text": payload.get("text") if isinstance(payload.get("text"), str) else payload.get("input"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"gen_{digest[:32]}"


def build_voice_timing_payload(payload: dict[str, Any], content: bytes) -> dict[str, Any]:
    props = audio_wav_properties(content)
    text = payload.get("text") if isinstance(payload.get("text"), str) else payload.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("timing metadata requires the generation text")
    words = timing_text_words(text)
    if not words:
        raise ValueError("timing metadata requires at least one spoken word")
    language = str(payload.get("language") or os.getenv("B1_VOICEBOX_DEFAULT_LANGUAGE", "en"))
    audio_sha256 = hashlib.sha256(content).hexdigest()
    generation_id = timing_generation_id(payload, audio_sha256)
    duration_ms = max(0, int(props["duration_ms"]))
    alignment = forced_align_words_mms(content, words)
    if alignment is not None:
        word_timestamps = alignment["word_timestamps"]
        timing_method = str(alignment["method"])
        timing_precision = "word-forced-aligned_phoneme-estimated"
        word_confidence_default = 0.75
        phoneme_confidence_default = 0.5
    else:
        weights = [max(1, len(word)) for word in words]
        total_weight = max(1, sum(weights))
        word_timestamps = []
        cursor = 0
        for word_index, word in enumerate(words):
            if word_index == len(words) - 1:
                word_end = duration_ms
            else:
                word_end = int(round(duration_ms * sum(weights[: word_index + 1]) / total_weight))
            if word_end < cursor:
                word_end = cursor
            word_timestamps.append(
                {
                    "word": word,
                    "start_ms": cursor,
                    "end_ms": word_end,
                    "confidence": 0.55,
                }
            )
            cursor = word_end
        timing_method = "b1-proportional-ipa-estimate"
        timing_precision = "estimated"
        word_confidence_default = 0.55
        phoneme_confidence_default = 0.45
    phonemes_by_word = phonemize_words(words, language)
    phoneme_timestamps: list[dict[str, Any]] = []
    for word_index, word_entry in enumerate(word_timestamps):
        word = words[word_index] if word_index < len(words) else str(word_entry.get("word") or "")
        phones = phonemes_by_word[word_index] if word_index < len(phonemes_by_word) else fallback_word_phonemes(word)
        word_start = int(word_entry["start_ms"])
        phone_start = word_start
        word_end = int(word_entry["end_ms"])
        phone_duration = max(0, word_end - word_start)
        for phone_index, phoneme in enumerate(phones):
            if phone_index == len(phones) - 1:
                phone_end = word_end
            else:
                phone_end = word_start + int(round(phone_duration * (phone_index + 1) / max(1, len(phones))))
            if phone_end < phone_start:
                phone_end = phone_start
            phoneme_timestamps.append(
                {
                    "phoneme": phoneme,
                    "start_ms": phone_start,
                    "end_ms": phone_end,
                    "word_index": word_index,
                    "confidence": phoneme_confidence_default,
                }
            )
            phone_start = phone_end
        word_entry.setdefault("confidence", word_confidence_default)
    result = {
        "schema_version": "b1_voice_timing.v1",
        "generation_id": generation_id,
        "audio_sha256": audio_sha256,
        "audio_bytes": len(content),
        "audio_sample_rate": props["sample_rate"],
        "audio_channels": props["channels"],
        "profile_id": str(payload.get("profile_id") or payload.get("voice") or payload.get("speaker") or ""),
        "engine": str(payload.get("engine") or ""),
        "language": language,
        "duration_ms": duration_ms,
        "phoneme_alphabet": "ipa",
        "timing_method": timing_method,
        "timing_precision": timing_precision,
        "word_timestamps": word_timestamps,
        "phoneme_timestamps": phoneme_timestamps,
    }
    if alignment is not None:
        result["alignment_model"] = "torchaudio.pipelines.MMS_FA"
        result["alignment_device"] = alignment.get("device")
        result["character_timestamps"] = alignment.get("character_timestamps", [])
    return result


def voice_timing_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "", name.strip())
    if not safe:
        raise ValueError("timing metadata identifier is empty")
    return voice_timing_dir() / f"{safe}.json"


def store_voice_timing(metadata: dict[str, Any]) -> None:
    generation_id = str(metadata.get("generation_id") or "")
    audio_sha256 = str(metadata.get("audio_sha256") or "")
    if not generation_id or not SHA256_RE.fullmatch(audio_sha256):
        raise ValueError("timing metadata is missing generation_id or audio_sha256")
    root = voice_timing_dir()
    root.mkdir(parents=True, exist_ok=True)
    body = json.dumps(metadata, sort_keys=True, indent=2)
    for name in (generation_id, f"sha256-{audio_sha256}"):
        path = voice_timing_path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)


def load_voice_timing(generation_id: str = "", audio_sha256: str = "") -> dict[str, Any] | None:
    candidates: list[str] = []
    if generation_id.strip():
        candidates.append(generation_id.strip())
    if audio_sha256.strip():
        candidates.append(f"sha256-{audio_sha256.strip().lower()}")
    for candidate in candidates:
        try:
            payload = json.loads(voice_timing_path(candidate).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == "b1_voice_timing.v1":
            return payload
    return None


def timing_response_headers(metadata: dict[str, Any]) -> dict[str, str]:
    return {
        "x-b1-generation-id": str(metadata.get("generation_id") or ""),
        "x-b1-audio-sha256": str(metadata.get("audio_sha256") or ""),
        "x-b1-timing-url": f"/generate/timing/{metadata.get('generation_id')}",
    }


def maybe_store_voice_timing(payload: dict[str, Any] | None, content: bytes, response_headers: dict[str, str]) -> None:
    if not isinstance(payload, dict):
        return
    try:
        metadata = build_voice_timing_payload(payload, content)
        store_voice_timing(metadata)
    except Exception as exc:
        log_json(component="b1-voicebox-proxy", event="voice_timing_store_failed", error=exc.__class__.__name__)
        return
    response_headers.update(timing_response_headers(metadata))


async def native_profile_exists(client: Any, native_profile_id: str) -> bool:
    response = await client.get(f"{upstream_http_base_url()}/profiles/{native_profile_id}")
    return response.status_code < 400


async def native_profile_id_for_name(client: Any, name: str) -> str:
    response = await client.get(f"{upstream_http_base_url()}/profiles")
    if response.status_code >= 400:
        return ""
    try:
        profiles = response.json()
    except ValueError:
        return ""
    if not isinstance(profiles, list):
        return ""
    for item in profiles:
        if isinstance(item, dict) and item.get("name") == name and isinstance(item.get("id"), str):
            return item["id"]
    return ""


async def create_native_clone_profile(client: Any, profile: dict[str, Any], engine: str, sample_items: list[dict[str, Any]], language: str) -> str:
    name = native_profile_name(profile, engine, sample_items)
    payload = {
        "name": name,
        "description": f"B1-managed {engine} cloned voice profile",
        "language": language,
        "voice_type": "cloned",
        "default_engine": engine,
    }
    response = await client.post(f"{upstream_http_base_url()}/profiles", json=payload)
    if response.status_code >= 400:
        existing = await native_profile_id_for_name(client, name)
        if existing:
            return existing
        raise ValueError(f"native Voicebox profile create failed with HTTP {response.status_code}")
    try:
        created = response.json()
    except ValueError as exc:
        raise ValueError("native Voicebox profile create returned invalid JSON") from exc
    native_profile_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(native_profile_id, str) or not native_profile_id:
        raise ValueError("native Voicebox profile create did not return an id")
    return native_profile_id


async def upload_native_profile_sample(client: Any, native_profile_id: str, sample: dict[str, Any]) -> None:
    path = Path(sample["path"])
    with path.open("rb") as handle:
        response = await client.post(
            f"{upstream_http_base_url()}/profiles/{native_profile_id}/samples",
            data={"reference_text": sample["reference_text"]},
            files={"file": (path.name, handle, sample["mime_type"])},
        )
    if response.status_code >= 400:
        raise ValueError(f"native Voicebox sample upload failed with HTTP {response.status_code}")


async def ensure_native_clone_profile(client: Any, payload: dict[str, Any], profile: dict[str, Any]) -> str:
    profile_id = str(profile.get("id") or "").strip()
    if not profile_id:
        raise ValueError("B1 voice profile envelope is missing an id")
    profile_type = str(profile.get("profile_type") or "").strip().lower()
    if profile_type not in {"clone", "cloned", "reference"}:
        raise ValueError(f"B1 voice profile type {profile_type!r} is not usable for cloned Voicebox speech")
    engine = selected_clone_engine(payload, profile)
    sample_items = profile_sample_items(profile)
    if not sample_items:
        raise ValueError("B1 voice profile needs at least one mounted reference sample for Voicebox cloning")
    sample_sha256s = [str(item["sha256"]) for item in sample_items]
    mapping = read_voicebox_profile_map()
    mapped = mapping.get(profile_id) if isinstance(mapping.get(profile_id), dict) else {}
    native_profile_id = str(mapped.get("native_profile_id") or "").strip() if isinstance(mapped, dict) else ""
    if (
        native_profile_id
        and mapped.get("engine") == engine
        and mapped.get("sample_sha256s") == sample_sha256s
        and await native_profile_exists(client, native_profile_id)
    ):
        return native_profile_id
    language = selected_language(payload, profile)
    native_profile_id = await create_native_clone_profile(client, profile, engine, sample_items, language)
    for sample in sample_items:
        await upload_native_profile_sample(client, native_profile_id, sample)
    mapping[profile_id] = {
        "native_profile_id": native_profile_id,
        "engine": engine,
        "sample_sha256s": sample_sha256s,
        "profile_name": native_profile_name(profile, engine, sample_items),
        "updated_at_unix": int(time.time()),
    }
    write_voicebox_profile_map(mapping)
    return native_profile_id


async def post_native_generation(client: Any, payload: dict[str, Any], generation_lock: asyncio.Lock | None = None) -> Any:
    if generation_lock is None:
        return await client.post(f"{upstream_http_base_url()}/generate/stream", json=payload)
    async with generation_lock:
        return await client.post(f"{upstream_http_base_url()}/generate/stream", json=payload)


def upstream_generation_failure_response(upstream: Any, action: str = "speech") -> JSONResponse:
    return JSONResponse(
        json_response(
            "failed",
            action,
            reason="upstream_generation_failed",
            upstream_status=upstream.status_code,
            upstream_content_type=str(upstream.headers.get("content-type") or "").split(";", 1)[0],
        ),
        status_code=upstream.status_code,
    )


def broadcast_audio_failure_response(exc: Exception, action: str = "speech") -> JSONResponse:
    return JSONResponse(
        json_response("failed", action, reason="broadcast_audio_postprocess_failed", error=str(exc)[:300]),
        status_code=502,
    )


async def voicebox_openai_speech_bridge(
    client: Any,
    body: bytes,
    content_type: str,
    generation_lock: asyncio.Lock | None = None,
) -> Response:
    if "json" not in content_type.lower():
        raise ValueError("Voicebox speech bridge requires a JSON request body")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Voicebox speech request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Voicebox speech request body must be a JSON object")
    profile = payload.get("b1_voice_profile") if isinstance(payload.get("b1_voice_profile"), dict) else None
    if profile is not None:
        engine = selected_clone_engine(payload, profile)
        native_profile_id = await ensure_native_clone_profile(client, payload, profile)
    else:
        engine = selected_clone_engine(payload, None)
        candidate = payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else payload.get("voice")
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Voicebox speech bridge needs a B1 voice profile or native Voicebox profile_id")
        native_profile_id = candidate.strip()
    generation_payload = native_generation_payload(payload, profile, native_profile_id, engine)
    upstream = await post_native_generation(client, generation_payload, generation_lock)
    if upstream.status_code >= 400:
        return upstream_generation_failure_response(upstream, "speech")
    policy = broadcast_audio_policy(payload, "audio/wav")
    try:
        content = await maybe_postprocess_broadcast_wav(upstream.content, policy)
    except RuntimeError as exc:
        return broadcast_audio_failure_response(exc, "speech")
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"content-encoding", "content-length"}
    }
    if policy is not None:
        response_headers.update(broadcast_audio_headers(policy))
    maybe_store_voice_timing(generation_payload, content, response_headers)
    response_headers.setdefault("content-type", "audio/wav")
    return Response(content=content, status_code=upstream.status_code, headers=response_headers)


async def generate_timing_metadata(
    client: Any,
    payload: dict[str, Any],
    generation_lock: asyncio.Lock | None = None,
) -> dict[str, Any]:
    existing = load_voice_timing(str(payload.get("generation_id") or ""), str(payload.get("audio_sha256") or ""))
    if existing is not None:
        return existing
    text = payload.get("text") if isinstance(payload.get("text"), str) else payload.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("timing metadata lookup needs generation_id, audio_sha256, or a generation text")
    upstream = await post_native_generation(client, payload, generation_lock)
    if upstream.status_code >= 400:
        raise RuntimeError(f"upstream generation failed with HTTP {upstream.status_code}")
    policy = broadcast_audio_policy(payload, "audio/wav")
    content = await maybe_postprocess_broadcast_wav(upstream.content, policy)
    metadata = build_voice_timing_payload(payload, content)
    store_voice_timing(metadata)
    return metadata


def apply_b1_voice_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.pop("b1_voice_profile", None)
    cleaned = {key: value for key, value in payload.items() if not key.startswith("b1_")}
    if not isinstance(profile, dict):
        return cleaned
    profile_id = str(profile.get("id") or "").strip()
    upstream = profile.get("upstream") if isinstance(profile.get("upstream"), dict) else {}
    for metadata_key, runtime_key in VOICE_PROFILE_UPSTREAM_FIELD_MAP.items():
        value = upstream.get(metadata_key) if isinstance(upstream, dict) else None
        if safe_runtime_scalar(value):
            cleaned[runtime_key] = value
    if profile_id and "voice" not in cleaned:
        cleaned["voice"] = profile_id
    if env_bool("B1_VOICEBOX_FORWARD_PROFILE_ENGINE", True):
        engine = profile.get("engine")
        if safe_runtime_scalar(engine):
            cleaned.setdefault("engine", engine)
    paths = profile_sample_paths(profile)
    if paths:
        sample_field = os.getenv("B1_VOICEBOX_SAMPLE_FIELD", "reference_audio_path").strip() or "reference_audio_path"
        samples_field = os.getenv("B1_VOICEBOX_SAMPLE_LIST_FIELD", "reference_audio_paths").strip() or "reference_audio_paths"
        cleaned.setdefault(sample_field, paths[0])
        if len(paths) > 1:
            cleaned.setdefault(samples_field, paths)
    return cleaned


def transform_voicebox_speech_body(body: bytes, content_type: str) -> bytes:
    if "json" not in content_type.lower():
        return body
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Voicebox speech request body must be JSON when using B1 voice profiles") from exc
    if not isinstance(payload, dict):
        return body
    transformed = apply_b1_voice_profile(payload)
    return json.dumps(transformed, separators=(",", ":"), sort_keys=True).encode("utf-8")


def handle_load(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "load")
    if list_result is not None:
        return list_result
    return json_response("unconfirmed", "load", reason="voicebox_loads_models_from_engine_or_profile")


async def run_speech_probe(payload: dict[str, Any], action: str) -> dict[str, Any]:
    import httpx

    candidates = payload_model_candidates(payload)
    if not candidates:
        return json_response("unsupported", action, reason="model_missing")
    body = {
        "model": candidates[0],
        "input": os.getenv("B1_VOICEBOX_HOOK_SMOKE_TEXT", "b1 voicebox runtime smoke"),
        "voice": os.getenv("B1_VOICEBOX_HOOK_SMOKE_VOICE", "default"),
    }
    endpoint = os.getenv("B1_VOICEBOX_HOOK_SMOKE_ENDPOINT", "/v1/audio/speech")
    try:
        async with httpx.AsyncClient(timeout=env_float("B1_VOICEBOX_HOOK_SMOKE_TIMEOUT_SECONDS", 300.0)) as client:
            response = await client.post(f"{upstream_http_base_url()}/{endpoint.lstrip('/')}", json=body)
    except httpx.HTTPError as exc:
        return json_response("failed", action, reason="speech_probe_failed", error=exc.__class__.__name__)
    if response.status_code >= 400:
        return json_response("failed", action, reason="speech_probe_rejected", upstream_status=response.status_code)
    return json_response(
        "ready" if action == "warm" else "ok",
        action,
        strategy="voicebox_speech_probe",
        upstream_status=response.status_code,
        content_type=response.headers.get("content-type", "").split(";", 1)[0],
        bytes_observed=len(response.content),
    )


async def handle_warm(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "warm")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_VOICEBOX_HOOK_WARM_ENABLED", False):
        return json_response("unconfirmed", "warm", reason="warm_disabled")
    return await run_speech_probe(payload, "warm")


async def handle_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    list_result = check_model_list(payload, "smoke")
    if list_result is not None and list_result.get("status") != "unconfirmed":
        return list_result
    if not env_bool("B1_VOICEBOX_HOOK_SMOKE_ENABLED", False):
        return json_response("unconfirmed", "smoke", reason="smoke_disabled")
    return await run_speech_probe(payload, "smoke")


def handle_unload(
    payload: dict[str, Any],
    manager: VoiceboxProcessManager,
    tracker: NativeRequestTracker,
) -> dict[str, Any]:
    active_requests = tracker.active()
    if active_requests > 0:
        return json_response("unconfirmed", "unload", reason="native_requests_active", active_requests=active_requests)
    if not env_bool("B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD", True):
        return json_response("unsupported", "unload", reason="restart_on_unload_disabled")
    timeout = env_float("B1_VOICEBOX_HOOK_UNLOAD_TIMEOUT_SECONDS", 20.0)
    try:
        result = manager.restart(timeout, reason="b1_runtime_unload")
    except OSError as exc:
        return json_response("failed", "unload", reason="restart_failed", error=exc.__class__.__name__)
    return json_response("ok", "unload", **result)


async def handle_runtime_action(
    action: str,
    payload: dict[str, Any],
    manager: VoiceboxProcessManager,
    tracker: NativeRequestTracker,
) -> dict[str, Any]:
    if action == "status":
        return voicebox_status(manager, tracker)
    if action == "build-info":
        return voicebox_build_info()
    if action == "load":
        return handle_load(payload)
    if action == "warm":
        return await handle_warm(payload)
    if action == "smoke":
        return await handle_smoke(payload)
    if action == "unload":
        return handle_unload(payload, manager, tracker)
    return json_response("unsupported", action, reason="unknown_action")


def log_json(**payload: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload}
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


def create_app(manager: VoiceboxProcessManager | None = None, tracker: NativeRequestTracker | None = None):
    import httpx

    app = FastAPI(title="B1 Voicebox Runtime Proxy", docs_url=None, redoc_url=None)
    runtime_manager = manager or VoiceboxProcessManager(default_upstream_command(), cwd=os.getenv("B1_VOICEBOX_UPSTREAM_CWD", "/app"))
    request_tracker = tracker or NativeRequestTracker()
    generation_lock = asyncio.Lock()

    @app.on_event("startup")
    async def startup() -> None:
        pid = runtime_manager.start()
        log_json(component="b1-voicebox-proxy", event="upstream_started", pid=pid)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        result = runtime_manager.stop(env_float("B1_VOICEBOX_SHUTDOWN_TIMEOUT_SECONDS", 20.0))
        log_json(component="b1-voicebox-proxy", event="upstream_stopped", result=result)

    @app.get("/b1/runtime/health")
    async def b1_runtime_health(request: Request) -> JSONResponse:
        auth_failure = runtime_control_auth_failure(request.headers)
        if auth_failure is not None:
            status, payload = auth_failure
            return JSONResponse(payload, status_code=status)
        return JSONResponse(json_response("healthy", "health", process=runtime_manager.status(), active_requests=request_tracker.active()))

    @app.get("/b1/runtime/build-info")
    async def b1_runtime_build_info() -> JSONResponse:
        payload = voicebox_build_info()
        return JSONResponse(payload, status_code=200 if payload["status"] == "ok" else 503)

    @app.get("/b1/runtime/status")
    async def b1_runtime_status(request: Request) -> JSONResponse:
        auth_failure = runtime_control_auth_failure(request.headers)
        if auth_failure is not None:
            status, payload = auth_failure
            return JSONResponse(payload, status_code=status)
        payload = voicebox_status(runtime_manager, request_tracker)
        return JSONResponse(payload, status_code=200 if payload["status"] == "ok" else 503)

    @app.post("/b1/runtime/{action}")
    async def b1_runtime_action(action: str, request: Request) -> JSONResponse:
        auth_failure = runtime_control_auth_failure(request.headers)
        if auth_failure is not None:
            status, payload = auth_failure
            return JSONResponse(payload, status_code=status)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse(json_response("invalid", action, reason="invalid_json"), status_code=400)
        result = await handle_runtime_action(action.strip().lower(), payload, runtime_manager, request_tracker)
        return JSONResponse(result, status_code=400 if result.get("status") == "invalid" else 200)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            async with httpx.AsyncClient(timeout=env_float("B1_VOICEBOX_HEALTH_TIMEOUT_SECONDS", 5.0)) as client:
                response = await client.get(f"{upstream_http_base_url()}/health")
        except httpx.HTTPError as exc:
            return JSONResponse(
                json_response("unhealthy", "health", reason="upstream_unreachable", error=exc.__class__.__name__),
                status_code=503,
            )
        status = "healthy" if response.status_code < 400 else "unhealthy"
        try:
            upstream_body: Any = response.json()
        except ValueError:
            upstream_body = response.text[:500]
        return JSONResponse(
            json_response(status, "health", upstream_status=response.status_code, upstream=upstream_body, process=runtime_manager.status()),
            status_code=200 if response.status_code < 400 else 503,
        )

    @app.post("/generate/timing")
    async def generate_timing(request: Request) -> JSONResponse:
        request_tracker.begin()
        try:
            body = await request.body()
            payload = json_body_object(body, request.headers.get("content-type", ""))
            if not isinstance(payload, dict):
                return JSONResponse(json_response("invalid", "timing", reason="JSON object request body required"), status_code=422)
            try:
                async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                    metadata = await generate_timing_metadata(client, payload, generation_lock)
            except ValueError as exc:
                return JSONResponse(json_response("invalid", "timing", reason=str(exc)), status_code=422)
            except RuntimeError as exc:
                return JSONResponse(json_response("failed", "timing", reason=str(exc)[:300]), status_code=502)
            except httpx.HTTPError as exc:
                return JSONResponse(
                    json_response("unhealthy", "timing", reason="upstream_unreachable", error=exc.__class__.__name__),
                    status_code=503,
                )
            return JSONResponse(metadata, headers=timing_response_headers(metadata))
        finally:
            request_tracker.end()

    @app.get("/generate/timing/{generation_id}")
    async def get_generate_timing(generation_id: str) -> JSONResponse:
        request_tracker.begin()
        try:
            metadata = load_voice_timing(generation_id, "")
            if metadata is None:
                return JSONResponse(json_response("missing", "timing", reason="timing_metadata_not_found"), status_code=404)
            return JSONResponse(metadata, headers=timing_response_headers(metadata))
        finally:
            request_tracker.end()

    @app.websocket("/{path:path}")
    async def websocket_proxy(websocket: WebSocket, path: str) -> None:
        import websockets

        await websocket.accept()
        request_tracker.begin()
        query = websocket.scope.get("query_string", b"").decode("latin-1")
        upstream_url = f"{upstream_ws_base_url()}/{path.lstrip('/')}"
        if query:
            upstream_url = f"{upstream_url}?{query}"
        headers = {
            key: value
            for key, value in websocket.headers.items()
            if key.lower() not in WEBSOCKET_HANDSHAKE_HEADERS and key.lower() != "authorization"
        }
        try:
            async with websockets.connect(upstream_url, additional_headers=headers, max_size=None) as upstream:

                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                await asyncio.gather(client_to_upstream(), upstream_to_client())
        except WebSocketDisconnect:
            return
        except Exception as exc:
            log_json(component="b1-voicebox-proxy", event="websocket_proxy_failed", path=path, error=exc.__class__.__name__)
            with suppress(Exception):
                await websocket.close(code=1011)
        finally:
            request_tracker.end()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def http_proxy(path: str, request: Request) -> Response:
        request_tracker.begin()
        try:
            body = await request.body()
            query = request.url.query
            upstream_path = f"{upstream_http_base_url()}/{path.lstrip('/')}"
            if query:
                upstream_path = f"{upstream_path}?{query}"
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "content-length", "authorization"}
            }
            forward_body = body
            request_payload = json_body_object(body, request.headers.get("content-type", ""))
            if request.method.upper() == "POST" and path.strip("/") == "v1/audio/speech":
                content_type = request.headers.get("content-type", "")
                async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                    try:
                        return await voicebox_openai_speech_bridge(client, body, content_type, generation_lock)
                    except ValueError as exc:
                        return JSONResponse(json_response("invalid", "speech", reason=str(exc)), status_code=422)
                    except httpx.HTTPError as exc:
                        return JSONResponse(
                            json_response("unhealthy", "proxy", reason="upstream_unreachable", error=exc.__class__.__name__),
                            status_code=503,
                        )
            try:
                async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                    if request.method.upper() == "POST" and path.strip("/") == "generate/stream":
                        async with generation_lock:
                            upstream = await client.request(request.method, upstream_path, content=forward_body, headers=headers)
                    else:
                        upstream = await client.request(request.method, upstream_path, content=forward_body, headers=headers)
            except httpx.HTTPError as exc:
                return JSONResponse(
                    json_response("unhealthy", "proxy", reason="upstream_unreachable", error=exc.__class__.__name__),
                    status_code=503,
                        )
            if request.method.upper() == "POST" and path.strip("/") == "generate/stream" and upstream.status_code >= 400:
                return upstream_generation_failure_response(upstream, "native-generate")
            content = upstream.content
            policy = None
            if request.method.upper() == "POST" and path.strip("/") == "generate/stream" and upstream.status_code < 400:
                policy = broadcast_audio_policy(request_payload, request.headers.get("accept", ""))
                try:
                    content = await maybe_postprocess_broadcast_wav(content, policy)
                except RuntimeError as exc:
                    return broadcast_audio_failure_response(exc, "native-generate")
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"content-encoding", "content-length"}
            }
            if policy is not None:
                response_headers.update(broadcast_audio_headers(policy))
            if request.method.upper() == "POST" and path.strip("/") == "generate/stream" and upstream.status_code < 400:
                maybe_store_voice_timing(request_payload, content, response_headers)
            return Response(content=content, status_code=upstream.status_code, headers=response_headers)
        finally:
            request_tracker.end()

    return app


def main() -> None:
    import uvicorn

    host = os.getenv("B1_VOICEBOX_HOST", "0.0.0.0")
    port = int(os.getenv("B1_VOICEBOX_PORT", "17493"))
    log_json(component="b1-voicebox-proxy", event="listening", host=host, port=port, upstream=upstream_http_base_url())
    uvicorn.run(create_app(), host=host, port=port, log_level=os.getenv("B1_VOICEBOX_PROXY_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
