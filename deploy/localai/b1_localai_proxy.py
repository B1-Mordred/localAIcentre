#!/usr/bin/env python3
from __future__ import annotations

import http.client
import hmac
import json
import os
import sys
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


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

CHAT_OPERATIONS = {"chat", "chat-completion", "chat-completions", "completion", "completions", "responses", "generate"}
CHAT_MODALITIES = {"llm", "vlm", "text", "chat", "vision"}
EMBEDDING_OPERATIONS = {"embedding", "embeddings", "embed"}
EMBEDDING_MODALITIES = {"embedding", "embeddings"}
IMAGE_GENERATION_OPERATIONS = {"generation", "image-generation", "text-to-image", "image"}
IMAGE_EDIT_OPERATIONS = {"edit", "image-edit", "image-to-image", "inpainting", "inpainting-outpainting"}
VIDEO_GENERATION_OPERATIONS = {"generation", "video-generation", "text-to-video", "video"}
VIDEO_IMAGE_OPERATIONS = {"image-to-video", "video-image", "image-video"}
TTS_OPERATIONS = {"speech", "text-to-speech", "tts"}
STT_OPERATIONS = {"transcription", "speech-to-text", "stt"}
LOCALAI_PROXY_VERSION = "b1-localai-proxy/v0.2.0"
LOCALAI_LIFECYCLE_ACTIONS = ["status", "build-info", "load", "warm", "smoke", "unload"]


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
    payload = {"status": status, "runtime": "localai", "action": action}
    payload.update(extra)
    return payload


def build_info_response() -> dict[str, Any]:
    upstream_version = os.getenv("B1_LOCALAI_UPSTREAM_VERSION", "v4.7.1-gpu-nvidia-cuda-12")
    upstream_commit = os.getenv("B1_LOCALAI_UPSTREAM_COMMIT", "b224c96db6f4b87306a33a808650bfce63b12588")
    upstream_image = os.getenv(
        "B1_LOCALAI_UPSTREAM_IMAGE",
        "localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973",
    )
    pinned = bool(upstream_version and upstream_commit and "@sha256:" in upstream_image)
    return json_response(
        "ok" if pinned else "unconfigured",
        "build-info",
        proxy_version=LOCALAI_PROXY_VERSION,
        upstream="localai/localai",
        upstream_version=upstream_version,
        upstream_commit=upstream_commit,
        upstream_image=upstream_image,
        pinned=pinned,
        capabilities={"actions": LOCALAI_LIFECYCLE_ACTIONS},
    )


def guardrail_status() -> dict[str, Any]:
    max_active_backends = env_int("LOCALAI_MAX_ACTIVE_BACKENDS", 1)
    watchdog_idle = env_bool("LOCALAI_WATCHDOG_IDLE", True)
    force_eviction_when_busy = env_bool("LOCALAI_FORCE_EVICTION_WHEN_BUSY", False)
    blockers: list[str] = []
    if max_active_backends != 1:
        blockers.append("LOCALAI_MAX_ACTIVE_BACKENDS must be 1")
    if not watchdog_idle:
        blockers.append("LOCALAI_WATCHDOG_IDLE must be true")
    if force_eviction_when_busy:
        blockers.append("LOCALAI_FORCE_EVICTION_WHEN_BUSY must be false")
    return {
        "status": "ok" if not blockers else "degraded",
        "max_active_backends": max_active_backends,
        "watchdog_idle": watchdog_idle,
        "watchdog_idle_timeout": os.getenv("LOCALAI_WATCHDOG_IDLE_TIMEOUT", "5m"),
        "watchdog_interval": os.getenv("LOCALAI_WATCHDOG_INTERVAL", "1s"),
        "force_eviction_when_busy": force_eviction_when_busy,
        "blockers": blockers,
    }


def status_response() -> dict[str, Any]:
    guardrails = guardrail_status()
    upstream_status = "unknown"
    upstream_http_status: int | None = None
    model_count: int | None = None
    try:
        upstream_http_status, model_payload = hook_timeout_client().request_json("GET", "/v1/models")
    except (OSError, ValueError) as exc:
        upstream_status = "unreachable"
        model_probe = {"status": upstream_status, "error": exc.__class__.__name__}
    else:
        if upstream_http_status < 400:
            upstream_status = "ok"
            model_count = len(model_ids_from_list(model_payload))
        elif upstream_http_status < 500:
            upstream_status = "unconfigured"
        else:
            upstream_status = "unhealthy"
        model_probe = {"status": upstream_status, "upstream_status": upstream_http_status, "model_count": model_count}
    if guardrails["status"] != "ok":
        status = guardrails["status"]
    elif upstream_status in {"ok", "unconfigured"}:
        status = "ok"
    else:
        status = "unhealthy"
    return json_response(
        status,
        "status",
        upstream=os.getenv("B1_LOCALAI_UPSTREAM_URL", "http://127.0.0.1:18080"),
        guardrails=guardrails,
        model_probe=model_probe,
        capabilities={"actions": LOCALAI_LIFECYCLE_ACTIONS},
        build_info=build_info_response(),
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


def lower_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip().lower() if isinstance(value, str) else ""


def operation_kind(payload: dict[str, Any]) -> str:
    operation = lower_field(payload, "operation")
    modality = lower_field(payload, "modality")
    if operation in EMBEDDING_OPERATIONS or modality in EMBEDDING_MODALITIES:
        return "embeddings"
    if operation in TTS_OPERATIONS:
        return "tts"
    if operation in STT_OPERATIONS:
        return "stt"
    if operation in IMAGE_EDIT_OPERATIONS:
        return "image_edit"
    if operation in VIDEO_IMAGE_OPERATIONS:
        return "video_image"
    if operation in VIDEO_GENERATION_OPERATIONS and (modality == "video" or operation != "generation"):
        return "video_generation"
    if operation in IMAGE_GENERATION_OPERATIONS and (modality == "image" or operation != "generation"):
        return "image_generation"
    if operation in CHAT_OPERATIONS or modality in CHAT_MODALITIES:
        return "chat"
    if modality == "video":
        return "video_generation"
    if modality == "image":
        return "image_generation"
    return "unknown"


def model_ids_from_list(payload: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.add(item["id"])
                elif isinstance(item, str):
                    ids.add(item)
        for key in ("models", "installed"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        ids.add(item)
                    elif isinstance(item, dict) and isinstance(item.get("name"), str):
                        ids.add(item["name"])
                    elif isinstance(item, dict) and isinstance(item.get("id"), str):
                        ids.add(item["id"])
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.add(item["id"])
    return ids


def model_available(candidates: Iterable[str], model_ids: set[str]) -> bool:
    normalized_ids = {strip_model_version(model_id) for model_id in model_ids}
    return any(strip_model_version(candidate) in normalized_ids for candidate in candidates)


def smoke_request(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    candidates = payload_model_candidates(payload)
    if not candidates:
        return "unsupported", "model_missing", None
    model = candidates[0]
    kind = operation_kind(payload)
    if kind == "chat":
        return (
            "ok",
            "chat",
            {
                "method": "POST",
                "path": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "messages": [{"role": "user", "content": "b1 local runtime smoke"}],
                    "max_tokens": int(os.getenv("B1_LOCALAI_SMOKE_MAX_TOKENS", "1")),
                    "stream": False,
                },
            },
        )
    if kind == "embeddings":
        return (
            "ok",
            "embeddings",
            {"method": "POST", "path": "/v1/embeddings", "body": {"model": model, "input": ["b1 local runtime smoke"]}},
        )
    if kind == "image_generation":
        return (
            "ok",
            "image_generation",
            {
                "method": "POST",
                "path": "/v1/images/generations",
                "body": {
                    "model": model,
                    "prompt": "b1 local runtime smoke",
                    "size": os.getenv("B1_LOCALAI_SMOKE_IMAGE_SIZE", "64x64"),
                    "n": 1,
                },
            },
        )
    if kind == "video_generation" and env_bool("B1_LOCALAI_VIDEO_SMOKE_ENABLED", False):
        return (
            "ok",
            "video_generation",
            {
                "method": "POST",
                "path": "/v1/videos/generations",
                "body": {
                    "model": model,
                    "prompt": "b1 local runtime smoke",
                    "seconds": float(os.getenv("B1_LOCALAI_SMOKE_VIDEO_SECONDS", "1")),
                },
            },
        )
    if kind == "tts":
        return (
            "ok",
            "tts",
            {
                "method": "POST",
                "path": "/v1/audio/speech",
                "body": {
                    "model": model,
                    "voice": os.getenv("B1_LOCALAI_SMOKE_TTS_VOICE", "default"),
                    "input": "b1 local runtime smoke",
                },
            },
        )
    return "unsupported", f"{kind}_smoke_not_available", None


class LocalAIClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("B1_LOCALAI_UPSTREAM_URL must use http or https")
        self.scheme = parsed.scheme
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.prefix = parsed.path.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        raw_body = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw_body))
        response = self.request(method, path, raw_body, headers)
        data = response.read()
        try:
            payload = json.loads(data.decode("utf-8")) if data else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return response.status, payload

    def request(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> http.client.HTTPResponse:
        connection_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(self.host, self.port, timeout=self.timeout_seconds)
        upstream_path = f"{self.prefix}{path}"
        connection.request(method, upstream_path, body=body, headers=headers)
        return connection.getresponse()


def load_client() -> LocalAIClient:
    return LocalAIClient(
        os.getenv("B1_LOCALAI_UPSTREAM_URL", "http://127.0.0.1:18080"),
        env_float("B1_LOCALAI_UPSTREAM_TIMEOUT_SECONDS", 1800.0),
    )


def hook_timeout_client() -> LocalAIClient:
    return LocalAIClient(
        os.getenv("B1_LOCALAI_UPSTREAM_URL", "http://127.0.0.1:18080"),
        env_float("B1_LOCALAI_SMOKE_TIMEOUT_SECONDS", 300.0),
    )


def check_model_list(client: LocalAIClient, payload: dict[str, Any], action: str) -> dict[str, Any] | None:
    candidates = payload_model_candidates(payload)
    if not candidates:
        return json_response("unsupported", action, reason="model_missing")
    try:
        status, model_payload = client.request_json("GET", "/v1/models")
    except OSError as exc:
        return json_response("unsupported", action, reason="model_list_unreachable", error=exc.__class__.__name__)
    if status >= 500:
        return json_response("unhealthy", action, reason="model_list_failed", upstream_status=status)
    if status >= 400:
        if env_bool("B1_LOCALAI_HOOK_STRICT_MODEL_LIST", False):
            return json_response("unconfigured", action, reason="model_list_rejected", upstream_status=status)
        return json_response("unconfirmed", action, reason="model_list_unavailable", upstream_status=status)
    model_ids = model_ids_from_list(model_payload)
    if model_ids and model_available(candidates, model_ids):
        return None
    if env_bool("B1_LOCALAI_HOOK_STRICT_MODEL_LIST", False):
        return json_response("unconfigured", action, reason="model_not_listed")
    return json_response("unconfirmed", action, reason="model_not_listed")


def handle_load(payload: dict[str, Any]) -> dict[str, Any]:
    client = hook_timeout_client()
    list_result = check_model_list(client, payload, "load")
    if list_result is not None:
        return list_result
    return json_response("unconfirmed", "load", reason="localai_loads_on_first_inference")


def handle_warm(payload: dict[str, Any]) -> dict[str, Any]:
    client = hook_timeout_client()
    list_result = check_model_list(client, payload, "warm")
    if list_result is not None and list_result.get("status") not in {"unconfirmed"}:
        return list_result
    if not env_bool("B1_LOCALAI_HOOK_WARM_ENABLED", False):
        return json_response("unconfirmed", "warm", reason="warm_smoke_disabled")
    return run_smoke_request(client, payload, "warm")


def handle_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    client = hook_timeout_client()
    list_result = check_model_list(client, payload, "smoke")
    if list_result is not None and list_result.get("status") not in {"unconfirmed"}:
        return list_result
    if not env_bool("B1_LOCALAI_HOOK_SMOKE_ENABLED", False):
        return json_response("unconfirmed", "smoke", reason="smoke_disabled")
    return run_smoke_request(client, payload, "smoke")


def run_smoke_request(client: LocalAIClient, payload: dict[str, Any], action: str) -> dict[str, Any]:
    state, reason, request = smoke_request(payload)
    if state != "ok" or request is None:
        return json_response(state, action, reason=reason)
    try:
        status, response_payload = client.request_json(str(request["method"]), str(request["path"]), request["body"])
    except OSError as exc:
        return json_response("failed", action, reason="smoke_request_failed", strategy=reason, error=exc.__class__.__name__)
    if status >= 400:
        return json_response("failed", action, reason="smoke_request_rejected", strategy=reason, upstream_status=status)
    return json_response("ready" if action == "warm" else "ok", action, strategy=reason, upstream_status=status, response_observed=response_payload is not None)


def handle_unload(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload_model_candidates(payload)
    if not candidates:
        return json_response("unconfirmed", "unload", reason="model_missing")
    client = hook_timeout_client()
    try:
        status, body = client.request_json("POST", "/backend/shutdown", {"model": candidates[0]})
    except OSError as exc:
        return json_response("unsupported", "unload", reason="shutdown_unreachable", error=exc.__class__.__name__)
    if status in {404, 405}:
        return json_response("unsupported", "unload", reason=f"http_{status}")
    if status >= 400:
        return json_response("failed", "unload", reason="shutdown_rejected", upstream_status=status)
    return json_response("ok", "unload", strategy="backend_shutdown", upstream_status=status, response_observed=body is not None)


def handle_runtime_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return status_response()
    if action == "build-info":
        return build_info_response()
    if action == "load":
        return handle_load(payload)
    if action == "warm":
        return handle_warm(payload)
    if action == "smoke":
        return handle_smoke(payload)
    if action == "unload":
        return handle_unload(payload)
    if action == "health":
        return json_response("unsupported", action, reason="post_only")
    return json_response("unsupported", action, reason="unknown_action")


class B1LocalAIProxy(BaseHTTPRequestHandler):
    server_version = LOCALAI_PROXY_VERSION
    protocol_version = "HTTP/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "component": "b1-localai-proxy",
            "client": self.client_address[0],
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "message": format_string % args,
        }
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/b1/runtime/health":
            auth_failure = runtime_control_auth_failure(self.headers)
            if auth_failure is not None:
                status, payload = auth_failure
                self.write_json(payload, status=status)
                return
            self.write_json(json_response("healthy", "health", upstream=os.getenv("B1_LOCALAI_UPSTREAM_URL", "http://127.0.0.1:18080")))
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        prefix = "/b1/runtime/"
        if path.startswith(prefix):
            action = path[len(prefix) :]
            if action in {"status", "build-info"}:
                auth_failure = runtime_control_auth_failure(self.headers)
                if auth_failure is not None:
                    status, payload = auth_failure
                    self.write_json(payload, status=status)
                    return
                self.write_json(handle_runtime_action(action, {}))
                return
        self.proxy_request()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        prefix = "/b1/runtime/"
        if path.startswith(prefix):
            action = path[len(prefix) :]
            auth_failure = runtime_control_auth_failure(self.headers)
            if auth_failure is not None:
                status, payload = auth_failure
                self.write_json(payload, status=status)
                return
            payload = self.read_json_body()
            if payload is None:
                self.write_json(json_response("invalid", action, reason="invalid_json"), status=400)
                return
            result = handle_runtime_action(action, payload)
            self.write_json(result, status=200 if result.get("status") not in {"invalid"} else 400)
            return
        self.proxy_request()

    def do_PUT(self) -> None:
        self.proxy_request()

    def do_PATCH(self) -> None:
        self.proxy_request()

    def do_DELETE(self) -> None:
        self.proxy_request()

    def do_HEAD(self) -> None:
        self.proxy_request(send_body=False)

    def do_OPTIONS(self) -> None:
        self.proxy_request()

    def read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or "0")
        data = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def write_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def proxy_request(self, send_body: bool = True) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        try:
            client = load_client()
            response = client.request(self.command, self.path, body, headers)
        except (OSError, ValueError) as exc:
            self.write_json(json_response("unhealthy", "proxy", reason="upstream_unreachable", error=exc.__class__.__name__), status=503)
            return
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            self.send_header(key, value)
        self.end_headers()
        if not send_body or self.command == "HEAD":
            response.read()
            return
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()


def main() -> None:
    host = os.getenv("B1_LOCALAI_PUBLIC_HOST", "0.0.0.0")
    port = int(os.getenv("B1_LOCALAI_PUBLIC_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), B1LocalAIProxy)
    print(json.dumps({"component": "b1-localai-proxy", "event": "listening", "host": host, "port": port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
