#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import hmac
import http.client
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
INFERENCE_PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/responses"}
PROFILE_SCHEMA = "b1.deepseek.runtime_profiles.v1"
ROUTER_VERSION = "b1-deepseek-v4-router/v0.1.3"
RUNTIME_NAME = "lan-deepseek-worker"
ATTESTATION_CACHE_SCHEMA = "b1.deepseek.attestation_cache.v1"
DEEPSEEK_QUALITY_ALIAS = "deepseek-quality"
DEEPSEEK_QUALITY_REASONING_BUDGETS = {
    "fast": 512, "normal": 2048, "quality": 4096, "deep": 8192,
}
DEEPSEEK_QUALITY_MAX_REASONING_BUDGET = max(DEEPSEEK_QUALITY_REASONING_BUDGETS.values())
DEEPSEEK_QUALITY_SAMPLING_DEFAULTS: dict[str, int | float] = {
    "temperature": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0,
    "typical_p": 1.0, "repeat_penalty": 1.0,
    "presence_penalty": 0.0, "frequency_penalty": 0.0,
}
MODEL_ROOT = Path("/models")
LLAMA_SERVER = Path("/usr/local/bin/llama-server")
MANAGER_OWNED_OPTIONS = {
    "-m", "--model", "-mu", "--model-url", "-md", "--model-draft",
    "--spec-draft-model", "--host", "--port", "--reuse-port", "--path",
    "--api-prefix", "--api-key", "--api-key-file", "--ssl-key-file",
    "--ssl-cert-file", "--alias", "--ui", "--webui", "--no-ui",
    "--no-webui", "--metrics", "--no-metrics", "--slots", "--no-slots",
    "--parallel", "-np", "--cache-ram", "-cram", "--timeout", "-to",
    "--jinja", "--no-jinja", "--reasoning", "-rea", "--reasoning-format",
    "--reasoning-preserve", "--no-reasoning-preserve", "--log-file",
}


def read_secret(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bearer(value: str | None) -> str:
    if not value:
        return ""
    scheme, _, token = value.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def authenticated(headers: Any) -> bool:
    supplied = bearer(headers.get("Authorization") if hasattr(headers, "get") else None)
    return bool(RUNTIME_TOKEN) and hmac.compare_digest(RUNTIME_TOKEN, supplied)


def translate_responses_reasoning(payload: dict[str, Any]) -> dict[str, Any]:
    """Map OpenAI Responses effort levels to DeepSeek V4 template controls."""
    reasoning = payload.get("reasoning")
    effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
    if effort in {"none", "minimal", "low"}:
        translated: dict[str, Any] = {"enable_thinking": False}
    elif effort in {"medium", "high"}:
        translated = {"enable_thinking": True, "reasoning_effort": "high"}
    elif effort in {"xhigh", "max"}:
        translated = {"enable_thinking": True, "reasoning_effort": "max"}
    else:
        return payload
    existing = payload.get("chat_template_kwargs")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("chat_template_kwargs must be an object")
    kwargs = dict(existing or {})
    for key, value in translated.items():
        kwargs.setdefault(key, value)
    updated = dict(payload)
    updated["chat_template_kwargs"] = kwargs
    return updated


def apply_quality_request_policy(payload: dict[str, Any], profile: RuntimeProfile) -> dict[str, Any]:
    """Normalize and bound quality-profile reasoning at the worker trust boundary."""
    if DEEPSEEK_QUALITY_ALIAS not in profile.aliases:
        return payload
    raw_budgets = [
        (key, payload[key])
        for key in ("reasoning_budget_tokens", "thinking_budget_tokens")
        if key in payload
    ]
    for key, value in raw_budgets:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if not 0 <= value <= DEEPSEEK_QUALITY_MAX_REASONING_BUDGET:
            raise ValueError(
                f"{key} must be between 0 and {DEEPSEEK_QUALITY_MAX_REASONING_BUDGET}"
            )
    if len({value for _key, value in raw_budgets}) > 1:
        raise ValueError("reasoning budget fields disagree")
    requested_level = payload.get("reasoning")
    if requested_level is not None and not isinstance(requested_level, dict):
        if requested_level not in DEEPSEEK_QUALITY_REASONING_BUDGETS:
            raise ValueError("reasoning level is unsupported")
    budget = (
        raw_budgets[0][1]
        if raw_budgets
        else DEEPSEEK_QUALITY_REASONING_BUDGETS.get(requested_level or "quality", 4096)
    )
    updated = dict(payload)
    if not isinstance(requested_level, dict):
        updated.pop("reasoning", None)
    updated.pop("thinking_budget_tokens", None)
    updated["reasoning_budget_tokens"] = budget
    for key, value in DEEPSEEK_QUALITY_SAMPLING_DEFAULTS.items():
        updated.setdefault(key, value)
    return updated


def drop_privileges() -> None:
    identity = pwd.getpwnam(os.getenv("B1_DEEPSEEK_CHILD_USER", "deepseek"))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(identity.pw_gid)
        os.setuid(identity.pw_uid)
    if os.geteuid() != identity.pw_uid or os.getegid() != identity.pw_gid:
        raise RuntimeError("DeepSeek router did not enter its unprivileged identity")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def response_chunks(response: http.client.HTTPResponse, size: int = 65536) -> Any:
    read = response.read1 if hasattr(response, "read1") else response.read
    while chunk := read(size):
        yield chunk


def normalized_model(value: Any) -> str:
    return value.split("@", 1)[0].strip() if isinstance(value, str) else ""


def model_candidates(payload: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("b1_resolved_model_version", "resolved_model_version", "model", "model_alias"):
        candidate = normalized_model(payload.get(key))
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def checked_model_path(raw: Any, context: str) -> Path:
    if not isinstance(raw, str) or not raw.startswith("/models/"):
        raise ValueError(f"{context} must be below /models")
    path = Path(raw)
    if ".." in path.parts or not path.is_absolute():
        raise ValueError(f"{context} is invalid")
    return path


def checked_digest(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or re.fullmatch(r"[0-9a-fA-F]{64}", raw) is None:
        raise ValueError(f"{context} is invalid")
    return raw.lower()


def checked_size(raw: Any, context: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(f"{context} is invalid")
    return raw


@dataclass(frozen=True)
class Artifact:
    role: str
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RuntimeProfile:
    model_id: str
    version: str
    artifacts: tuple[Artifact, ...]
    server_args: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    draft: Artifact | None = None

    @property
    def model_path(self) -> Path:
        return self.artifacts[0].path


def option_name(value: str) -> str:
    return value.split("=", 1)[0]


def load_profiles(path: Path) -> dict[str, RuntimeProfile]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema_version", "profiles"}:
        raise ValueError("profile document must contain schema_version and profiles")
    if document["schema_version"] != PROFILE_SCHEMA or not isinstance(document["profiles"], list):
        raise ValueError("unsupported profile document")
    profiles: dict[str, RuntimeProfile] = {}
    selectors: set[str] = set()
    for index, item in enumerate(document["profiles"]):
        context = f"profiles[{index}]"
        allowed = {"model_id", "version", "artifacts", "server_args", "aliases", "draft"}
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError(f"{context} contains unsupported fields")
        for required in ("model_id", "version", "artifacts", "server_args"):
            if required not in item:
                raise ValueError(f"{context}.{required} is required")
        model_id = item["model_id"]
        if not isinstance(model_id, str) or not model_id.startswith("b1-unsloth-deepseek-v4-flash-0731-"):
            raise ValueError(f"{context}.model_id is invalid")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(v, str) and v.strip() for v in aliases):
            raise ValueError(f"{context}.aliases must be a string list")
        normalized_aliases = tuple(v.strip() for v in aliases)
        if len(set(normalized_aliases)) != len(normalized_aliases):
            raise ValueError(f"{context}.aliases contains duplicates")
        if any(v in selectors for v in (model_id, *normalized_aliases)):
            raise ValueError(f"{context} contains a duplicated model selector")
        selectors.update((model_id, *normalized_aliases))
        version = item["version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{context}.version is required")
        raw_artifacts = item["artifacts"]
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ValueError(f"{context}.artifacts must be a non-empty list")
        artifacts: list[Artifact] = []
        artifact_paths: set[Path] = set()
        for artifact_index, raw_artifact in enumerate(raw_artifacts):
            artifact_context = f"{context}.artifacts[{artifact_index}]"
            if not isinstance(raw_artifact, dict) or set(raw_artifact) != {"role", "path", "sha256", "size_bytes"}:
                raise ValueError(f"{artifact_context} is invalid")
            role = raw_artifact["role"]
            if not isinstance(role, str) or not role.strip():
                raise ValueError(f"{artifact_context}.role is invalid")
            artifact = Artifact(
                role=role.strip(),
                path=checked_model_path(raw_artifact["path"], f"{artifact_context}.path"),
                sha256=checked_digest(raw_artifact["sha256"], f"{artifact_context}.sha256"),
                size_bytes=checked_size(raw_artifact["size_bytes"], f"{artifact_context}.size_bytes"),
            )
            if artifact.path in artifact_paths:
                raise ValueError(f"{context}.artifacts contains duplicate paths")
            artifacts.append(artifact)
            artifact_paths.add(artifact.path)
        if artifacts[0].role != "main-shard-1":
            raise ValueError(f"{context}.artifacts[0] must be main-shard-1")
        args = item["server_args"]
        if not isinstance(args, list) or not args or not all(isinstance(v, str) and v for v in args):
            raise ValueError(f"{context}.server_args must be a non-empty string list")
        if any(option_name(v) in MANAGER_OWNED_OPTIONS for v in args):
            raise ValueError(f"{context}.server_args contains a manager-owned option")
        draft = None
        raw_draft = item.get("draft")
        if raw_draft is not None:
            if not isinstance(raw_draft, dict) or set(raw_draft) != {"role", "path", "sha256", "size_bytes"}:
                raise ValueError(f"{context}.draft is invalid")
            draft = Artifact(
                role=str(raw_draft["role"]),
                path=checked_model_path(raw_draft["path"], f"{context}.draft.path"),
                sha256=checked_digest(raw_draft["sha256"], f"{context}.draft.sha256"),
                size_bytes=checked_size(raw_draft["size_bytes"], f"{context}.draft.size_bytes"),
            )
            if draft.role != "dspark-draft" or draft.path in artifact_paths:
                raise ValueError(f"{context}.draft is invalid")
        profiles[model_id] = RuntimeProfile(
            model_id=model_id,
            version=version.strip(),
            artifacts=tuple(artifacts),
            server_args=tuple(args),
            aliases=normalized_aliases,
            draft=draft,
        )
    if not profiles:
        raise ValueError("at least one DeepSeek profile is required")
    return profiles


def gpu_metrics() -> dict[str, Any]:
    fields = ["uuid", "name", "memory.total", "memory.used", "memory.free", "utilization.gpu",
              "temperature.gpu", "power.draw", "power.limit", "pstate"]
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        row = next(csv.reader(result.stdout.splitlines(), skipinitialspace=True))
        values = dict(zip(fields, (value.strip() for value in row), strict=True))
        return {"available": True, "devices": [{
            "uuid": values["uuid"], "name": values["name"],
            "memory_total_mib": int(float(values["memory.total"])),
            "memory_used_mib": int(float(values["memory.used"])),
            "memory_free_mib": int(float(values["memory.free"])),
            "utilization_gpu_percent": int(float(values["utilization.gpu"])),
            "temperature_c": int(float(values["temperature.gpu"])),
            "power_draw_w": float(values["power.draw"]), "power_limit_w": float(values["power.limit"]),
            "performance_state": values["pstate"],
        }]}
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError):
        return {"available": False, "devices": []}


def memory_metrics() -> dict[str, Any]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        total, available = values["MemTotal"], values["MemAvailable"]
        return {"available": True, "total_bytes": total, "available_bytes": available,
                "used_bytes": total - available, "swap_free_bytes": values.get("SwapFree", 0)}
    except (OSError, KeyError, ValueError):
        return {"available": False}


class DeepSeekRuntime:
    def __init__(self, profiles: dict[str, RuntimeProfile]) -> None:
        self.profiles = profiles
        self.selectors = {selector: profile for profile in profiles.values()
                          for selector in (profile.model_id, *profile.aliases)}
        self.lock = threading.RLock()
        self.load_lock = threading.RLock()
        cache_file = os.getenv("B1_DEEPSEEK_ATTESTATION_CACHE_FILE", "").strip()
        self.attestation_cache_file = Path(cache_file) if cache_file else None
        self.verified_files = self._load_verified_files()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_model = ""
        self.active_job_id = ""
        self.active_requests = 0
        self.last_activity = time.monotonic()
        self.started_at = 0.0
        self.stop_event = threading.Event()
        self.idle_seconds = max(30, int(os.getenv("B1_DEEPSEEK_IDLE_TIMEOUT_SECONDS", "300")))
        self.child_port = int(os.getenv("B1_DEEPSEEK_CHILD_PORT", "18000"))
        self.start_timeout = max(30, int(os.getenv("B1_DEEPSEEK_START_TIMEOUT_SECONDS", "2400")))
        self.maximum_idle_gpu_mib = max(0, int(os.getenv("B1_DEEPSEEK_IDLE_GPU_MEMORY_MIB", "256")))
        self.maximum_power_limit_w = max(1.0, float(os.getenv("B1_DEEPSEEK_MAX_POWER_LIMIT_W", "125")))
        self.maximum_load_temperature_c = max(1, int(os.getenv("B1_DEEPSEEK_MAX_LOAD_TEMPERATURE_C", "60")))
        self.load_cooling_timeout = max(
            0, int(os.getenv("B1_DEEPSEEK_LOAD_COOLING_TIMEOUT_SECONDS", "600"))
        )
        self.maximum_runtime_temperature_c = max(
            self.maximum_load_temperature_c + 1,
            int(os.getenv("B1_DEEPSEEK_MAX_RUNTIME_TEMPERATURE_C", "88")),
        )
        self.safety_sample_seconds = max(1.0, float(os.getenv("B1_DEEPSEEK_SAFETY_SAMPLE_SECONDS", "5")))
        self.last_safety_event: dict[str, Any] | None = None
        self.thread = threading.Thread(target=self._idle_loop, name="deepseek-idle-watchdog", daemon=True)
        self.thread.start()

    def profile(self, selector: str) -> RuntimeProfile:
        profile = self.selectors.get(normalized_model(selector))
        if profile is None:
            raise KeyError(selector)
        return profile

    def profile_from_payload(self, payload: dict[str, Any]) -> RuntimeProfile | None:
        for candidate in model_candidates(payload):
            if candidate in self.selectors:
                return self.selectors[candidate]
        return None

    def _child_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _child_health(self) -> bool:
        if not self._child_alive():
            return False
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.child_port, timeout=3)
            connection.request("GET", "/health")
            response = connection.getresponse()
            response.read()
            status = response.status
            connection.close()
            return status < 400
        except OSError:
            return False

    def _all_artifacts(self, profile: RuntimeProfile) -> tuple[Artifact, ...]:
        return profile.artifacts + ((profile.draft,) if profile.draft is not None else ())

    def _cache_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ATTESTATION_CACHE_SCHEMA,
            "profiles_sha256": os.getenv("B1_DEEPSEEK_PROFILES_SHA256", ""),
            "files": {
                str(path): {
                    "device": identity[0], "inode": identity[1], "size_bytes": identity[2],
                    "mtime_ns": identity[3], "sha256": identity[4],
                }
                for path, identity in sorted(self.verified_files.items(), key=lambda item: str(item[0]))
            },
        }

    @staticmethod
    def _cache_signature(payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(RUNTIME_TOKEN.encode(), body, hashlib.sha256).hexdigest()

    def _load_verified_files(self) -> dict[Path, tuple[int, int, int, int, str]]:
        path = self.attestation_cache_file
        if path is None or not RUNTIME_TOKEN:
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) != {"payload", "signature"}:
                return {}
            payload, signature = document["payload"], document["signature"]
            if not isinstance(payload, dict) or not isinstance(signature, str):
                return {}
            if not hmac.compare_digest(self._cache_signature(payload), signature):
                return {}
            if payload.get("schema_version") != ATTESTATION_CACHE_SCHEMA:
                return {}
            if payload.get("profiles_sha256") != os.getenv("B1_DEEPSEEK_PROFILES_SHA256", ""):
                return {}
            files = payload.get("files")
            if not isinstance(files, dict):
                return {}
            verified: dict[Path, tuple[int, int, int, int, str]] = {}
            for raw_path, value in files.items():
                if not isinstance(raw_path, str) or not isinstance(value, dict):
                    return {}
                if set(value) != {"device", "inode", "size_bytes", "mtime_ns", "sha256"}:
                    return {}
                numbers = tuple(value[key] for key in ("device", "inode", "size_bytes", "mtime_ns"))
                digest = value["sha256"]
                if not all(isinstance(item, int) and not isinstance(item, bool) for item in numbers):
                    return {}
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    return {}
                verified[Path(raw_path)] = (*numbers, digest)
            return verified
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _save_verified_files(self) -> None:
        path = self.attestation_cache_file
        if path is None or not RUNTIME_TOKEN:
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._cache_payload()
            document = {"payload": payload, "signature": self._cache_signature(payload)}
            temporary.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError:
            with suppress(OSError):
                temporary.unlink()

    def _verify_profile_files(self, profile: RuntimeProfile) -> None:
        root = MODEL_ROOT.resolve()
        for artifact in self._all_artifacts(profile):
            resolved = artifact.path.resolve(strict=True)
            if root not in resolved.parents or not resolved.is_file():
                raise RuntimeError(f"profile artifact is outside the read-only model root: {artifact.path}")
            before = resolved.stat()
            if before.st_size != artifact.size_bytes:
                raise RuntimeError(f"profile artifact size mismatch: {artifact.path.name}")
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            cached = self.verified_files.get(resolved)
            if cached is not None and cached[:4] == identity and cached[4] == artifact.sha256:
                continue
            actual = sha256(resolved)
            after = resolved.stat()
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity != after_identity:
                raise RuntimeError(f"profile artifact changed during verification: {artifact.path.name}")
            if actual != artifact.sha256:
                raise RuntimeError(f"profile artifact checksum mismatch: {artifact.path.name}")
            self.verified_files[resolved] = (*identity, actual)
            self._save_verified_files()

    def _assert_load_safety(self, *, allow_hot: bool = False) -> bool:
        devices = gpu_metrics().get("devices") or []
        if not devices:
            raise RuntimeError("DeepSeek GPU telemetry is unavailable")
        hot = False
        for device in devices:
            if device.get("power_limit_w", 9999) > self.maximum_power_limit_w + 0.5:
                raise RuntimeError("DeepSeek GPU power policy is not enforced")
            if device.get("temperature_c", 9999) > self.maximum_load_temperature_c:
                hot = True
            if device.get("memory_used_mib", 999999) > self.maximum_idle_gpu_mib:
                raise RuntimeError("P40 is not idle; B1 must unload the current GPU runtime first")
        if hot and not allow_hot:
            raise RuntimeError("DeepSeek GPU is above the safe load temperature")
        return not hot

    def _wait_for_load_safety(self) -> None:
        deadline = time.monotonic() + self.load_cooling_timeout
        while True:
            if self._assert_load_safety(allow_hot=True):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("DeepSeek GPU did not cool to the safe load temperature")
            time.sleep(min(self.safety_sample_seconds, max(0.1, deadline - time.monotonic())))

    def command(self, profile: RuntimeProfile) -> list[str]:
        command = [
            str(LLAMA_SERVER), "--model", str(profile.model_path), "--host", "127.0.0.1",
            "--port", str(self.child_port), "--alias", profile.model_id, "--parallel", "1",
            "--cache-ram", "1024", "--metrics", "--no-webui", "--no-slots", "--jinja",
            "--reasoning", "on", "--reasoning-preserve", "--timeout", "3600",
            "--log-verbosity", "1",
        ]
        if profile.draft is not None:
            command.extend(["--spec-draft-model", str(profile.draft.path)])
        command.extend(profile.server_args)
        return command

    def attest(self, selector: str) -> dict[str, Any]:
        profile = self.profile(selector)
        with self.load_lock:
            if self.active_requests:
                raise RuntimeError("cannot attest DeepSeek artifacts while inference is active")
            self._verify_profile_files(profile)
            return {
                "status": "ok", "runtime": RUNTIME_NAME, "backend": "deepseek-v4", "action": "attest",
                "model_id": profile.model_id, "version": profile.version,
                "files": [{"role": item.role, "path": item.path.name, "size_bytes": item.size_bytes,
                           "sha256": item.sha256, "read_only": not os.access(item.path, os.W_OK)}
                          for item in self._all_artifacts(profile)],
                "llama_commit": os.getenv("B1_DEEPSEEK_LLAMA_COMMIT", ""),
                "image_digest": os.getenv("B1_DEEPSEEK_IMAGE_DIGEST", ""),
                "profiles_sha256": os.getenv("B1_DEEPSEEK_PROFILES_SHA256", ""),
            }

    def ensure_loaded(self, selector: str, job_id: str = "") -> dict[str, Any]:
        profile = self.profile(selector)
        with self.load_lock:
            with self.lock:
                if self.active_model == profile.model_id and self._child_health():
                    self.active_job_id = job_id or self.active_job_id
                    self.last_activity = time.monotonic()
                    return {"status": "ready", "strategy": "already_loaded", "model": profile.model_id}
                if self.active_requests:
                    raise RuntimeError("cannot replace DeepSeek profile while inference is active")
                self._stop_locked(force=False)
                self._wait_for_load_safety()
                self._verify_profile_files(profile)
                self.process = subprocess.Popen(self.command(profile), stdin=subprocess.DEVNULL,
                                                stdout=None, stderr=None, cwd="/tmp")
                self.active_model = profile.model_id
                self.active_job_id = job_id
                self.started_at = time.monotonic()
                self.last_activity = self.started_at
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                with self.lock:
                    if not self._child_alive():
                        code = self.process.returncode if self.process is not None else None
                        self.active_model = ""
                        self.active_job_id = ""
                        raise RuntimeError(f"llama-server exited during load with code {code}")
                    if self._child_health():
                        return {"status": "ready", "strategy": "started", "model": profile.model_id,
                                "load_time_ms": round((time.monotonic() - self.started_at) * 1000)}
                time.sleep(1)
            with self.lock:
                self._stop_locked(force=True)
            raise TimeoutError("llama-server did not become healthy before the load timeout")

    def _stop_locked(self, *, force: bool) -> dict[str, Any]:
        process, model = self.process, self.active_model
        if self.active_requests and not force:
            raise RuntimeError("cannot unload DeepSeek while inference is active")
        if process is None or process.poll() is not None:
            self.process, self.active_model, self.active_job_id = None, "", ""
            return {"status": "ok", "strategy": "already_idle", "model": model or None}
        process.send_signal(signal.SIGKILL if force else signal.SIGTERM)
        try:
            process.wait(timeout=5 if force else 30)
            escalated = force
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            escalated = True
        self.process, self.active_model, self.active_job_id = None, "", ""
        self.last_activity = time.monotonic()
        return {"status": "ok", "strategy": "process_stopped", "model": model, "escalated": escalated}

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        with self.load_lock, self.lock:
            return self._stop_locked(force=force)

    def enter_request(self, selector: str) -> RuntimeProfile:
        profile = self.profile(selector)
        with self.load_lock, self.lock:
            if self.active_model != profile.model_id or not self._child_health():
                raise RuntimeError("managed DeepSeek runtime must be loaded by B1 before inference")
            self.active_requests += 1
            self.last_activity = time.monotonic()
            return profile

    def leave_request(self) -> None:
        with self.lock:
            self.active_requests = max(0, self.active_requests - 1)
            self.last_activity = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "active_model": self.active_model or None, "active_job_id": self.active_job_id or None,
                "active_requests": self.active_requests, "queued_requests": 0,
                "loaded": self._child_alive(), "idle_timeout_seconds": self.idle_seconds,
                "artifact_attestation": {
                    "persistent_cache_enabled": self.attestation_cache_file is not None,
                    "verified_file_count": len(self.verified_files),
                },
                "safety_limits": {
                    "maximum_idle_gpu_memory_mib": self.maximum_idle_gpu_mib,
                    "maximum_power_limit_w": self.maximum_power_limit_w,
                    "maximum_load_temperature_c": self.maximum_load_temperature_c,
                    "load_cooling_timeout_seconds": self.load_cooling_timeout,
                    "maximum_runtime_temperature_c": self.maximum_runtime_temperature_c,
                    "sample_seconds": self.safety_sample_seconds,
                },
                "last_safety_event": self.last_safety_event,
                "child_pid": self.process.pid if self._child_alive() and self.process is not None else None,
            }

    def _idle_loop(self) -> None:
        last_sample = 0.0
        while not self.stop_event.wait(1):
            with self.load_lock, self.lock:
                if not self._child_alive():
                    continue
                now = time.monotonic()
                temperatures: list[int] = []
                if now - last_sample >= self.safety_sample_seconds:
                    temperatures = [item["temperature_c"] for item in gpu_metrics().get("devices", [])
                                    if isinstance(item.get("temperature_c"), int)]
                    last_sample = now
                if temperatures and max(temperatures) >= self.maximum_runtime_temperature_c:
                    self.last_safety_event = {
                        "reason": "gpu_temperature_limit_reached", "temperature_c": max(temperatures),
                        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    self._stop_locked(force=True)
                elif not self.active_requests and now - self.last_activity >= self.idle_seconds:
                    self._stop_locked(force=False)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.stop(force=True)
        self.thread.join(timeout=3)


class Router:
    def __init__(self, runtime: DeepSeekRuntime) -> None:
        self.runtime = runtime

    def lifecycle(self, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        profile = self.runtime.profile_from_payload(payload)
        try:
            if action == "attest" and profile is not None:
                return 200, self.runtime.attest(profile.model_id)
            if action in {"load", "warm"} and profile is not None:
                result = self.runtime.ensure_loaded(profile.model_id, str(payload.get("job_id") or ""))
                return 200, {"runtime": RUNTIME_NAME, "backend": "deepseek-v4", "action": action, **result}
            if action == "smoke" and profile is not None:
                return 200, {"runtime": RUNTIME_NAME, "backend": "deepseek-v4", "action": action,
                             **self.smoke(profile)}
            if action in {"unload", "cancel", "recover"}:
                result = self.runtime.stop(force=action in {"cancel", "recover"})
                return 200, {"runtime": RUNTIME_NAME, "backend": "deepseek-v4", "action": action, **result}
            return 404, {"status": "unsupported", "runtime": RUNTIME_NAME, "action": action,
                         "reason": "unknown_model" if profile is None else "unsupported_action"}
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            return 503, {"status": "failed", "runtime": RUNTIME_NAME, "backend": "deepseek-v4",
                         "action": action, "reason": exc.__class__.__name__}

    def smoke(self, profile: RuntimeProfile) -> dict[str, Any]:
        self.runtime.enter_request(profile.model_id)
        body = json.dumps({"model": profile.model_id, "messages": [{"role": "user", "content": "Return OK."}],
                           "max_tokens": 1, "temperature": 0.0, "stream": False},
                          separators=(",", ":")).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.runtime.child_port, timeout=300)
        try:
            connection.request("POST", "/v1/chat/completions", body=body,
                               headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise RuntimeError("DeepSeek smoke inference was rejected")
            result = json.loads(raw.decode())
            if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
                raise RuntimeError("DeepSeek smoke returned an invalid response")
            return {"status": "ok", "strategy": "bounded_chat", "upstream_status": response.status}
        finally:
            connection.close()
            self.runtime.leave_request()


ROUTER: Router | None = None
RUNTIME_TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    server_version = ROUTER_VERSION
    protocol_version = "HTTP/1.0"

    @property
    def router(self) -> Router:
        if ROUTER is None:
            raise RuntimeError("router is not initialized")
        return ROUTER

    def log_message(self, format_string: str, *args: Any) -> None:
        print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "component": "b1-deepseek-v4-router", "method": self.command,
                          "path": self.path.split("?", 1)[0], "message": format_string % args},
                         separators=(",", ":")), file=sys.stderr, flush=True)

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def require_auth(self) -> bool:
        if authenticated(self.headers):
            return True
        self.write_json(401, {"status": "unauthorized", "reason": "runtime_control_token_required"})
        return False

    def read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length < 0 or length > 16 * 1024 * 1024:
            return None
        return self.rfile.read(length) if length else b""

    def json_body(self, raw: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/readyz":
            gpu = gpu_metrics()
            self.write_json(200 if gpu["available"] else 503,
                            {"status": "ok" if gpu["available"] else "degraded", "runtime": RUNTIME_NAME,
                             "gpu_count": len(gpu["devices"])})
            return
        if not self.require_auth():
            return
        if path == "/v1/models":
            data = [{"id": profile.model_id, "object": "model", "owned_by": "b1-ai-hub",
                     "version": profile.version, "runtime": "deepseek-v4"}
                    for profile in self.router.runtime.profiles.values()]
            self.write_json(200, {"object": "list", "data": data})
            return
        if path == "/b1/runtime/metrics":
            gpu = gpu_metrics()
            self.write_json(200, {"status": "ok" if gpu["available"] else "degraded", "runtime": RUNTIME_NAME,
                                  "action": "metrics", "backend": "deepseek-v4", "gpu": gpu,
                                  "memory": memory_metrics(), "work": self.router.runtime.snapshot()})
            return
        if path in {"/b1/runtime/status", "/b1/runtime/build-info", "/b1/runtime/health"}:
            self.write_json(200, {"status": "ok", "runtime": RUNTIME_NAME,
                                  "action": path.rsplit("/", 1)[-1], "router_version": ROUTER_VERSION,
                                  "llama_commit": os.getenv("B1_DEEPSEEK_LLAMA_COMMIT", ""),
                                  "image_digest": os.getenv("B1_DEEPSEEK_IMAGE_DIGEST", ""),
                                  "profiles_sha256": os.getenv("B1_DEEPSEEK_PROFILES_SHA256", ""),
                                  "work": self.router.runtime.snapshot()})
            return
        self.write_json(404, {"status": "unsupported", "runtime": RUNTIME_NAME})

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        raw = self.read_body()
        if raw is None:
            self.write_json(413, {"status": "invalid", "reason": "request_too_large"})
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        payload = self.json_body(raw)
        if payload is None:
            self.write_json(400, {"status": "invalid", "reason": "invalid_json"})
            return
        if path.startswith("/b1/runtime/"):
            status, result = self.router.lifecycle(path.rsplit("/", 1)[-1], payload)
            self.write_json(status, result)
            return
        profile = self.router.runtime.profile_from_payload(payload)
        if path not in INFERENCE_PATHS or profile is None:
            self.write_json(400, {"error": {"message": "operation or model is unsupported by managed DeepSeek",
                                             "type": "unsupported_operation"}})
            return
        if path == "/v1/responses":
            try:
                translated = translate_responses_reasoning(payload)
            except ValueError:
                self.write_json(400, {"error": {"message": "chat_template_kwargs must be an object",
                                                 "type": "invalid_request_error"}})
                return
            if translated is not payload:
                payload = translated
        try:
            payload = apply_quality_request_policy(payload, profile)
        except ValueError as exc:
            self.write_json(422, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.proxy_inference(path, raw, profile)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_PUT(self) -> None:
        self.reject_method()

    def do_PATCH(self) -> None:
        self.reject_method()

    def do_DELETE(self) -> None:
        self.reject_method()

    def do_OPTIONS(self) -> None:
        self.reject_method()

    def reject_method(self) -> None:
        if self.require_auth():
            self.write_json(405, {"status": "unsupported", "runtime": RUNTIME_NAME})

    def forward_headers(self, body: bytes) -> dict[str, str]:
        excluded = HOP_BY_HOP_HEADERS | {"host", "content-length", "authorization"}
        headers = {key: value for key, value in self.headers.items() if key.lower() not in excluded}
        headers["Content-Length"] = str(len(body))
        return headers

    def relay(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            for chunk in response_chunks(response):
                self.wfile.write(chunk)
                self.wfile.flush()
        connection.close()

    def proxy_inference(self, path: str, body: bytes, profile: RuntimeProfile) -> None:
        try:
            self.router.runtime.enter_request(profile.model_id)
        except (KeyError, OSError, RuntimeError, TimeoutError) as exc:
            self.write_json(503, {"error": {"message": "managed DeepSeek runtime is not loaded by B1",
                                             "type": "runtime_not_loaded", "detail": exc.__class__.__name__}})
            return
        connection = http.client.HTTPConnection("127.0.0.1", self.router.runtime.child_port, timeout=3700)
        try:
            connection.request("POST", path, body=body, headers=self.forward_headers(body))
            response = connection.getresponse()
            self.relay(connection, response)
        except (BrokenPipeError, ConnectionResetError):
            connection.close()
        except OSError:
            connection.close()
            with suppress(BrokenPipeError):
                self.write_json(502, {"error": {"message": "DeepSeek child request failed",
                                                 "type": "upstream_error"}})
        finally:
            self.router.runtime.leave_request()


class ManagedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> int:
    global ROUTER, RUNTIME_TOKEN
    profiles = load_profiles(Path(os.getenv("B1_DEEPSEEK_PROFILES_FILE", "/etc/b1/deepseek-profiles.json")))
    RUNTIME_TOKEN = read_secret(os.getenv("B1_RUNTIME_CONTROL_TOKEN_FILE", "/run/secrets/runtime_control_token"))
    if not RUNTIME_TOKEN:
        raise RuntimeError("runtime-control token is missing")
    if not LLAMA_SERVER.is_file():
        raise RuntimeError("pinned llama-server binary is missing")
    drop_privileges()
    runtime = DeepSeekRuntime(profiles)
    ROUTER = Router(runtime)
    server = ManagedThreadingHTTPServer(
        (os.getenv("B1_DEEPSEEK_MANAGER_HOST", "0.0.0.0"),
         int(os.getenv("B1_DEEPSEEK_MANAGER_PORT", "8080"))), Handler,
    )

    def terminate(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        server.serve_forever()
    finally:
        runtime.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
