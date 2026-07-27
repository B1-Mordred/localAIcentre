#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
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


async def voicebox_openai_speech_bridge(client: Any, body: bytes, content_type: str) -> Response:
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
    upstream = await client.post(f"{upstream_http_base_url()}/generate/stream", json=generation_payload)
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-encoding"
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


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
            if request.method.upper() == "POST" and path.strip("/") == "v1/audio/speech":
                content_type = request.headers.get("content-type", "")
                async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                    try:
                        return await voicebox_openai_speech_bridge(client, body, content_type)
                    except ValueError as exc:
                        return JSONResponse(json_response("invalid", "speech", reason=str(exc)), status_code=422)
                    except httpx.HTTPError as exc:
                        return JSONResponse(
                            json_response("unhealthy", "proxy", reason="upstream_unreachable", error=exc.__class__.__name__),
                            status_code=503,
                        )
            try:
                async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                    upstream = await client.request(request.method, upstream_path, content=forward_body, headers=headers)
            except httpx.HTTPError as exc:
                return JSONResponse(
                    json_response("unhealthy", "proxy", reason="upstream_unreachable", error=exc.__class__.__name__),
                    status_code=503,
                )
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-encoding"
            }
            return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)
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
