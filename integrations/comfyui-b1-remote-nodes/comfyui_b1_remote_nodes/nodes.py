from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TERMINAL_JOB_STATES = {"completed", "cancelled", "failed", "expired", "recovery_required"}
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_OUTPUT_DIR = "b1-artifacts"
DEFAULT_MAX_DATA_URL_BYTES = 256 * 1024 * 1024
CONFIG_FILE_ENV = "B1_AI_HUB_CONFIG_FILE"
API_KEY_ENV = "B1_AI_HUB_API_KEY"
API_KEY_FILE_ENV = "B1_AI_HUB_API_KEY_FILE"
CA_FILE_ENV = "B1_AI_HUB_CA_FILE"
ALLOW_INSECURE_HTTP_ENV = "B1_AI_HUB_ALLOW_INSECURE_HTTP"
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
SAFE_FORM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
BAD_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
MIME_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
UPLOAD_ID_PATTERN = re.compile(r"^upload_[a-f0-9]{32}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MEDIA_JOB_ROUTE_PREFIX = "/v1/media/jobs/"
MEDIA_JOB_LINK_SUFFIXES = {"self": "", "cancel": "", "events": "/events", "artifacts": "/artifacts"}
MEDIA_KINDS = {"image", "audio", "video"}
ARTIFACT_URL_KEYS = ("url", "artifact_url", "download_url")
ALLOWED_UPLOAD_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "audio/wav",
    "audio/mpeg",
    "audio/ogg",
    "video/mp4",
    "video/webm",
}
PLACEHOLDER_CPU_AUDIO_ENGINES = {"scaffold"}


class B1RemoteNodeError(RuntimeError):
    pass


def default_config_file_path() -> Path:
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return root / "B1 AI Hub" / "comfyui-remote-nodes.json"
    root = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "b1-ai-hub" / "comfyui-remote-nodes.json"


def configured_config_file_path() -> tuple[Path, bool] | None:
    configured = os.getenv(CONFIG_FILE_ENV)
    if configured is not None:
        if not configured.strip():
            return None
        return Path(configured).expanduser(), True
    return default_config_file_path().expanduser(), False


def local_config_with_path() -> tuple[dict[str, Any], Path | None]:
    candidate = configured_config_file_path()
    if candidate is None:
        return {}, None
    path, explicit = candidate
    if not path.exists():
        if explicit:
            raise B1RemoteNodeError(f"B1 config file does not exist: {path}")
        return {}, None
    if not path.is_file():
        raise B1RemoteNodeError(f"B1 config path is not a file: {path}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise B1RemoteNodeError(f"B1 config file is not readable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise B1RemoteNodeError(f"B1 config file must be JSON: {path}") from exc
    if not isinstance(parsed, dict):
        raise B1RemoteNodeError("B1 config file must contain a JSON object")
    return parsed, path


def local_config() -> dict[str, Any]:
    return local_config_with_path()[0]


def require_private_file(path: Path, label: str) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o077
    if mode:
        raise B1RemoteNodeError(f"{label} must not be readable, writable, or executable by group/other users; run chmod 600 {path}")


def config_value(config_key: str, env_key: str, default: str = "") -> str:
    env_value = os.getenv(env_key)
    if env_value is not None:
        return env_value
    config = local_config()
    value = config.get(config_key, config.get(env_key))
    if value is None:
        return default
    if not isinstance(value, str):
        raise B1RemoteNodeError(f"B1 config value {config_key} must be a string")
    return value


def config_string(config: dict[str, Any], key: str, env_key: str | None = None) -> str:
    value = config.get(key)
    if value is None and env_key is not None:
        value = config.get(env_key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise B1RemoteNodeError(f"B1 config value {key} must be a string")
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def has_unsafe_path_segment(path: str) -> bool:
    for segment in path.split("/"):
        if not segment:
            continue
        if BAD_PERCENT_ESCAPE_PATTERN.search(segment):
            return True
        try:
            decoded = urllib.parse.unquote(segment, errors="strict")
        except UnicodeDecodeError:
            return True
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded:
            return True
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
            return True
    return False


def resolve_secret_file_path(value: str, config_path: Path | None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and config_path is not None:
        path = config_path.parent / path
    return path


def read_api_key_file(path: Path) -> str:
    if not path.is_file():
        raise B1RemoteNodeError(f"B1 API key file is not a file: {path}")
    require_private_file(path, "B1 API key file")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise B1RemoteNodeError(f"B1 API key file is not readable: {path}") from exc
    if not token:
        raise B1RemoteNodeError(f"B1 API key file is empty: {path}")
    return token


def api_base() -> str:
    value = config_value("api_base", "B1_AI_HUB_API_BASE", "https://api.ai.b1.germering").strip().rstrip("/")
    if "\\" in value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise B1RemoteNodeError("B1 API base contains unsafe characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise B1RemoteNodeError("B1 API base must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise B1RemoteNodeError("B1 API base must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise B1RemoteNodeError("B1 API base must not include credentials, query, or fragment")
    if "\\" in parsed.netloc or "\\" in parsed.path or not parsed.hostname:
        raise B1RemoteNodeError("B1 API base contains unsafe characters")
    path = parsed.path.rstrip("/")
    if has_unsafe_path_segment(path):
        raise B1RemoteNodeError("B1 API base path contains unsafe traversal segments")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def api_key() -> str:
    env_value = os.getenv(API_KEY_ENV)
    if env_value is not None:
        return env_value
    env_file = os.getenv(API_KEY_FILE_ENV)
    if env_file is not None:
        if not env_file.strip():
            return ""
        return read_api_key_file(Path(env_file).expanduser())
    config, path = local_config_with_path()
    inline_key = config_string(config, "api_key", API_KEY_ENV)
    key_file = config_string(config, "api_key_file", API_KEY_FILE_ENV)
    if inline_key and key_file:
        raise B1RemoteNodeError("B1 config must not set both api_key and api_key_file")
    if key_file:
        return read_api_key_file(resolve_secret_file_path(key_file, path))
    if inline_key and path is not None:
        require_private_file(path, "B1 config file containing api_key")
    return inline_key


def ca_file() -> str:
    env_value = os.getenv(CA_FILE_ENV)
    if env_value is not None:
        raw = env_value.strip()
        config_path = None
    else:
        config, config_path = local_config_with_path()
        raw = config_string(config, "ca_file", CA_FILE_ENV)
    if not raw:
        return ""
    path = resolve_secret_file_path(raw, config_path)
    if not path.is_file():
        raise B1RemoteNodeError(f"B1 CA file is not a file: {path}")
    return str(path)


def configured_download_dir() -> Path:
    return Path(config_value("download_dir", "B1_AI_HUB_DOWNLOAD_DIR", DEFAULT_OUTPUT_DIR)).expanduser()


def request_url(path: str) -> str:
    raw = str(path or "")
    if raw != raw.strip():
        raise B1RemoteNodeError("B1 API path is unsafe")
    if "\\" in raw or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw):
        raise B1RemoteNodeError("B1 API path is unsafe")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "?" in raw or "#" in raw or not parsed.path.startswith("/"):
        raise B1RemoteNodeError("B1 API path must be an internal path without scheme, host, query, or fragment")
    if has_unsafe_path_segment(parsed.path):
        raise B1RemoteNodeError("B1 API path is unsafe")
    return api_base() + parsed.path


def response_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if body:
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(detail, dict):
                return detail.get("message") or json.dumps(detail, sort_keys=True)
            if isinstance(detail, list):
                return "; ".join(str(item.get("msg") or item) for item in detail)
        except Exception:
            return body[:500]
    return f"HTTP {exc.code}"


def build_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    if payload is not None and data is not None:
        raise B1RemoteNodeError("request cannot include both JSON payload and raw data")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") if payload is not None else data
    request = urllib.request.Request(request_url(path), data=body, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    token = api_key()
    enforce_token_transport_security(request.full_url, token)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return request


def enforce_token_transport_security(url: str, token: str) -> None:
    if not token:
        return
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and env_bool(ALLOW_INSECURE_HTTP_ENV):
        return
    raise B1RemoteNodeError(
        "refusing to send a B1 API key over plain HTTP; use HTTPS "
        f"or set {ALLOW_INSECURE_HTTP_ENV}=true only for isolated development"
    )


def b1_urlopen(request: urllib.request.Request, *, timeout: int) -> Any:
    configured_ca = ca_file()
    if not configured_ca:
        return urllib.request.urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=configured_ca)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def request_json(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        with b1_urlopen(
            build_request(path, method=method, payload=payload, data=data, headers=headers),
            timeout=timeout_seconds,
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise B1RemoteNodeError(response_error_detail(exc)) from exc
    except urllib.error.URLError as exc:
        raise B1RemoteNodeError(f"B1 API request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise B1RemoteNodeError("B1 API returned non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise B1RemoteNodeError("B1 API returned an unexpected JSON shape")
    return parsed


def request_bytes(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, dict[str, str]]:
    try:
        with b1_urlopen(
            build_request(path, method=method, payload=payload, data=data, headers=headers),
            timeout=timeout_seconds,
        ) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.read(), response_headers
    except urllib.error.HTTPError as exc:
        raise B1RemoteNodeError(response_error_detail(exc)) from exc
    except urllib.error.URLError as exc:
        raise B1RemoteNodeError(f"B1 API request failed: {exc.reason}") from exc


def tts_placeholder_proof(headers: dict[str, str]) -> dict[str, Any]:
    header_map = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    marker_text = header_map.get("x-b1-placeholder", "").lower()
    placeholder: bool | None = None
    reasons: list[str] = []
    if marker_text in {"true", "false"}:
        placeholder = marker_text == "true"
    else:
        reasons.append("placeholder_marker_missing")
    cpu_audio_engine = header_map.get("x-b1-cpu-audio-engine", "")
    if placeholder is True:
        reasons.append("explicit_placeholder_marker")
    if cpu_audio_engine and placeholder is not False:
        reasons.append("audio_cpu_non_placeholder_marker_missing")
    if cpu_audio_engine.lower() in PLACEHOLDER_CPU_AUDIO_ENGINES:
        reasons.append("scaffold_cpu_audio_engine")
    return {
        "placeholder": placeholder,
        "cpu_audio_engine": cpu_audio_engine or None,
        "placeholder_failure": bool(reasons),
        "reasons": reasons,
    }


def text_to_speech_download(
    model: str,
    text: str,
    voice: str,
    response_format: str = "wav",
    runtime_policy: str = "any",
    filename: str = "speech.wav",
) -> tuple[str, int, str, dict[str, Any]]:
    content, headers = request_bytes(
        "/v1/audio/speech",
        {"model": model, "input": text, "voice": voice, "response_format": response_format, "runtime_policy": runtime_policy},
        method="POST",
        timeout_seconds=1800,
    )
    file_path, byte_count, digest = write_download(content, headers, filename)
    return file_path, byte_count, digest, tts_placeholder_proof(headers)


def json_output(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def parse_json_object(value: str, field_name: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise B1RemoteNodeError(f"{field_name} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise B1RemoteNodeError(f"{field_name} must be a JSON object")
    return parsed


def max_data_url_bytes() -> int:
    raw = os.getenv("B1_AI_HUB_MAX_DATA_URL_BYTES")
    if raw is None:
        config = local_config()
        configured = config.get("max_data_url_bytes", config.get("B1_AI_HUB_MAX_DATA_URL_BYTES"))
        raw = str(configured) if configured is not None else None
    if raw is None:
        return DEFAULT_MAX_DATA_URL_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_DATA_URL_BYTES
    return value if value > 0 else DEFAULT_MAX_DATA_URL_BYTES


def validate_data_url_reference(reference: str, kind: str) -> None:
    header, separator, encoded = reference.partition(",")
    if not separator or not header.startswith("data:"):
        raise B1RemoteNodeError(f"{kind} data URL must include media metadata and payload")
    metadata = header.removeprefix("data:")
    parts = [part.strip().lower() for part in metadata.split(";") if part.strip()]
    mime_type = parts[0] if parts and "/" in parts[0] else ""
    expected_prefix = f"{kind.strip().lower()}/" if kind.strip().lower() in {"image", "audio", "video"} else ""
    if expected_prefix and not mime_type.startswith(expected_prefix):
        raise B1RemoteNodeError(f"{kind} data URL must use a {expected_prefix} media type")
    if "base64" not in parts[1:]:
        raise B1RemoteNodeError(f"{kind} data URL must be base64 encoded")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise B1RemoteNodeError(f"{kind} data URL payload is not valid base64") from exc
    if not content:
        raise B1RemoteNodeError(f"{kind} data URL payload is empty")
    limit = max_data_url_bytes()
    if len(content) > limit:
        raise B1RemoteNodeError(f"{kind} data URL payload exceeds {limit} bytes")


def expected_media_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    return normalized if normalized in MEDIA_KINDS else ""


def validate_staged_media_reference(reference: dict[str, Any], kind: str) -> None:
    if reference.get("source") != "staged_upload":
        raise B1RemoteNodeError(f"{kind} staged reference has unsupported source")
    upload_id = reference.get("id")
    if not isinstance(upload_id, str) or not UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise B1RemoteNodeError(f"{kind} staged reference has an invalid upload id")
    path = reference.get("path")
    if not isinstance(path, str) or not path.startswith("inputs/"):
        raise B1RemoteNodeError(f"{kind} staged reference path is invalid")
    if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise B1RemoteNodeError(f"{kind} staged reference path is invalid")
    mime_type = reference.get("mime_type")
    if not isinstance(mime_type, str) or not MIME_TYPE_PATTERN.fullmatch(mime_type):
        raise B1RemoteNodeError(f"{kind} staged reference has an invalid MIME type")
    expected_kind = expected_media_kind(kind)
    if expected_kind and not mime_type.startswith(f"{expected_kind}/"):
        raise B1RemoteNodeError(f"{kind} staged reference must use a {expected_kind}/ media type")
    reference_kind = reference.get("kind")
    if not isinstance(reference_kind, str) or reference_kind not in MEDIA_KINDS:
        raise B1RemoteNodeError(f"{kind} staged reference has an invalid media kind")
    if expected_kind and reference_kind != expected_kind:
        raise B1RemoteNodeError(f"{kind} staged reference kind must be {expected_kind}")
    bytes_value = reference.get("bytes")
    if not isinstance(bytes_value, int) or bytes_value < 1:
        raise B1RemoteNodeError(f"{kind} staged reference has an invalid byte count")
    sha256 = reference.get("sha256")
    if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
        raise B1RemoteNodeError(f"{kind} staged reference has an invalid sha256")


def staged_reference_from_json(value: dict[str, Any], kind: str) -> dict[str, Any]:
    candidate = value
    if isinstance(value.get("reference"), dict):
        candidate = value["reference"]
    elif isinstance(value.get("input"), dict):
        candidate = value["input"]
    validate_staged_media_reference(candidate, kind)
    return candidate


def require_media_reference(value: str, kind: str) -> str | dict[str, Any]:
    reference = value.strip()
    if not reference:
        raise B1RemoteNodeError(f"{kind} reference is required")
    if reference.startswith("/artifacts/"):
        return require_internal_artifact_path(reference)
    if reference.startswith("data:"):
        validate_data_url_reference(reference, kind)
        return reference
    if reference.startswith("{"):
        return staged_reference_from_json(parse_json_object(reference, f"{kind} reference"), kind)
    if reference.startswith("http://") or reference.startswith("https://"):
        raise B1RemoteNodeError(f"{kind} must be uploaded to B1 or referenced as an internal artifact; external URLs are not accepted")
    raise B1RemoteNodeError(f"{kind} must be a staged JSON reference, internal /artifacts URL, or data URL")


def require_string_media_reference(value: str, kind: str) -> str:
    reference = require_media_reference(value, kind)
    if isinstance(reference, dict):
        raise B1RemoteNodeError(f"{kind} staged upload JSON is supported for media jobs; use an internal artifact path or data URL for this request")
    return reference


def decode_base64_payload(value: str, expected_kind: str) -> bytes:
    raw = value.strip()
    if not raw:
        raise B1RemoteNodeError(f"{expected_kind} base64 input is required")
    if raw.startswith("data:"):
        media_type, separator, data = raw.partition(",")
        if not separator or ";base64" not in media_type:
            raise B1RemoteNodeError("data URL must be base64 encoded")
        raw = data
    try:
        content = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise B1RemoteNodeError(f"{expected_kind} input is not valid base64") from exc
    if not content:
        raise B1RemoteNodeError(f"{expected_kind} input is empty")
    return content


def safe_form_name(value: str) -> str:
    name = value.strip()
    if not SAFE_FORM_NAME_PATTERN.fullmatch(name):
        raise B1RemoteNodeError("multipart form field name is unsafe")
    return name


def safe_multipart_header_value(value: str | None, fallback: str, limit: int = 256) -> str:
    normalized = (value or fallback).strip() or fallback
    if "\r" in normalized or "\n" in normalized:
        raise B1RemoteNodeError("multipart header value is unsafe")
    return normalized[:limit]


def safe_mime_type(value: str | None, fallback: str = "application/octet-stream") -> str:
    normalized = (value or fallback).split(";", 1)[0].strip().lower() or fallback
    if not MIME_TYPE_PATTERN.fullmatch(normalized):
        raise B1RemoteNodeError("media MIME type is unsafe")
    return normalized


def media_kind_for_mime_type(mime_type: str) -> str:
    normalized = safe_mime_type(mime_type)
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return ""


def safe_upload_mime_type(value: str | None) -> str:
    normalized = safe_mime_type(value)
    if normalized not in ALLOWED_UPLOAD_MIME_TYPES:
        raise B1RemoteNodeError("upload MIME type is not supported by B1 media uploads")
    return normalized


def safe_filename(value: str | None, fallback: str) -> str:
    leaf = (value or "").replace("\\", "/").split("/")[-1]
    cleaned = SAFE_FILENAME_PATTERN.sub("_", leaf).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:160]


def safe_path_segment(value: str, label: str) -> str:
    segment = value.strip()
    if not SAFE_ID_PATTERN.fullmatch(segment):
        raise B1RemoteNodeError(f"{label} must be a B1 identifier, not a path or URL")
    return urllib.parse.quote(segment, safe="")


def require_internal_media_job_route(value: str, link_name: str) -> str:
    expected_suffix = MEDIA_JOB_LINK_SUFFIXES.get(link_name)
    if expected_suffix is None:
        raise B1RemoteNodeError("media job link name is unsupported")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith(MEDIA_JOB_ROUTE_PREFIX):
        raise B1RemoteNodeError("media job links must be internal /v1/media/jobs paths without query or fragment")
    if "\\" in parsed.path or any(char.isspace() or ord(char) < 32 for char in parsed.path):
        raise B1RemoteNodeError("media job link path is unsafe")
    expected_parts = ["v1", "media", "jobs"]
    parts = parsed.path.strip("/").split("/")
    suffix_parts = [part for part in expected_suffix.strip("/").split("/") if part]
    if len(parts) != 4 + len(suffix_parts) or parts[:3] != expected_parts or parts[4:] != suffix_parts:
        raise B1RemoteNodeError(f"media job {link_name} link does not match the expected route")
    job_segment = parts[3]
    if not job_segment or len(job_segment) > 256 or BAD_PERCENT_ESCAPE_PATTERN.search(job_segment):
        raise B1RemoteNodeError("media job link id segment is unsafe")
    try:
        decoded = urllib.parse.unquote(job_segment, errors="strict")
    except UnicodeDecodeError as exc:
        raise B1RemoteNodeError("media job link id segment is unsafe") from exc
    decoded_parts = decoded.split("/")
    if (
        any(part in {"", ".", ".."} for part in decoded_parts)
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
    ):
        raise B1RemoteNodeError("media job link id segment is unsafe")
    return parsed.path


def media_job_route(job_reference: str, link_name: str, fallback_suffix: str = "") -> str:
    raw = job_reference.strip()
    if not raw:
        raise B1RemoteNodeError("job_id is required")
    if raw.startswith("{"):
        payload = parse_json_object(raw, "job_id")
        links = payload.get("links")
        if isinstance(links, dict) and link_name in links:
            candidate = links[link_name]
            if not isinstance(candidate, str):
                raise B1RemoteNodeError(f"media job {link_name} link must be a string")
            return require_internal_media_job_route(candidate, link_name)
        raw_id = payload.get("id")
        if not isinstance(raw_id, str):
            raise B1RemoteNodeError("job JSON must include an id string when no usable link is present")
        raw = raw_id
    elif raw.startswith(MEDIA_JOB_ROUTE_PREFIX):
        return require_internal_media_job_route(raw, link_name)
    return f"/v1/media/jobs/{safe_path_segment(raw, 'job_id')}{fallback_suffix}"


def multipart_form_data(
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    marker = boundary or f"----b1-ai-hub-{secrets.token_hex(16)}"
    if "\r" in marker or "\n" in marker:
        raise B1RemoteNodeError("multipart boundary is unsafe")
    parts: list[bytes] = []
    for name, value in fields.items():
        field_name = safe_form_name(name)
        parts.append(f"--{marker}\r\n".encode("ascii"))
        parts.append(f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("ascii"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for field_name, filename, content_type, content in files:
        safe_field = safe_form_name(field_name)
        safe_file = safe_filename(filename, "upload.bin")
        safe_content_type = safe_mime_type(content_type)
        parts.append(f"--{marker}\r\n".encode("ascii"))
        parts.append(f'Content-Disposition: form-data; name="{safe_field}"; filename="{safe_file}"\r\n'.encode("ascii"))
        parts.append(f"Content-Type: {safe_content_type}\r\n\r\n".encode("ascii"))
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{marker}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={marker}"


def extension_for_content_type(content_type: str | None, fallback: str = ".bin") -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return mimetypes.guess_extension(normalized) or fallback


def require_internal_artifact_path(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith("/artifacts/"):
        raise B1RemoteNodeError("artifact_url must be an internal /artifacts path without query or fragment")
    if "\\" in parsed.path or any(char.isspace() or ord(char) < 32 for char in parsed.path):
        raise B1RemoteNodeError("artifact_url path is unsafe")
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "artifacts":
        raise B1RemoteNodeError("artifact_url must name an artifact below /artifacts")
    for part in parts:
        if BAD_PERCENT_ESCAPE_PATTERN.search(part):
            raise B1RemoteNodeError("artifact_url path is unsafe")
        try:
            decoded = urllib.parse.unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise B1RemoteNodeError("artifact_url path is unsafe") from exc
        if not part or decoded in {".", ".."} or "/" in decoded or "\\" in decoded or any(ord(char) < 32 for char in decoded):
            raise B1RemoteNodeError("artifact_url path is unsafe")
    return parsed.path


def expected_byte_count(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise B1RemoteNodeError(f"{label} must be a non-negative integer")
    return value


def expected_sha256(value: Any, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise B1RemoteNodeError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


def validate_download_integrity(content: bytes, expected_bytes: int | None = None, expected_digest: str | None = None) -> str:
    if expected_bytes is not None and len(content) != expected_bytes:
        raise B1RemoteNodeError(f"artifact download size mismatch: expected {expected_bytes} bytes, got {len(content)}")
    digest = hashlib.sha256(content).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise B1RemoteNodeError("artifact download SHA-256 mismatch")
    return digest


def artifact_reference_path_from_record(record: dict[str, Any]) -> str:
    for key in ARTIFACT_URL_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return require_internal_artifact_path(value)
    path_value = record.get("path")
    if isinstance(path_value, str) and path_value.strip():
        raw = path_value.strip()
        if raw.startswith("/artifacts/"):
            return require_internal_artifact_path(raw)
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or raw.startswith("/"):
            raise B1RemoteNodeError("artifact record path must be a relative artifact path")
        return require_internal_artifact_path(f"/artifacts/{raw}")
    raise B1RemoteNodeError("artifact record must include an internal artifact url or path")


def artifact_reference_from_json(payload: dict[str, Any], artifact_index: int = 0) -> dict[str, Any]:
    if isinstance(artifact_index, bool) or not isinstance(artifact_index, int):
        raise B1RemoteNodeError("artifact index must be an integer")
    artifacts = payload.get("artifacts")
    if artifacts is None and isinstance(payload.get("data"), list):
        artifacts = payload.get("data")
    if artifacts is not None:
        if not isinstance(artifacts, list):
            raise B1RemoteNodeError("artifact list must be an array")
        if artifact_index < 0 or artifact_index >= len(artifacts):
            raise B1RemoteNodeError("artifact index is out of range")
        selected = artifacts[artifact_index]
        if not isinstance(selected, dict):
            raise B1RemoteNodeError("selected artifact record must be an object")
        return selected
    return payload


def artifact_reference(value: str, artifact_index: int = 0) -> tuple[str, int | None, str | None, str | None]:
    raw = value.strip()
    if not raw:
        raise B1RemoteNodeError("artifact_url is required")
    if raw.startswith("{"):
        record = artifact_reference_from_json(parse_json_object(raw, "artifact_url"), artifact_index)
        path = artifact_reference_path_from_record(record)
        preferred_name = record.get("filename") if isinstance(record.get("filename"), str) else path.rsplit("/", 1)[-1]
        return (
            path,
            expected_byte_count(record.get("bytes"), "artifact bytes"),
            expected_sha256(record.get("sha256"), "artifact sha256"),
            preferred_name,
        )
    return require_internal_artifact_path(raw), None, None, raw.rsplit("/", 1)[-1]


def write_download(
    content: bytes,
    headers: dict[str, str],
    preferred_name: str | None = None,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[str, int, str]:
    output_dir = configured_download_dir().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = extension_for_content_type(headers.get("content-type"))
    fallback = f"b1-artifact{extension}"
    filename = safe_filename(preferred_name, fallback)
    if "." not in filename:
        filename = f"{filename}{extension}"
    target = (output_dir / filename).resolve()
    if output_dir not in target.parents and target != output_dir:
        raise B1RemoteNodeError("artifact download path escapes configured output directory")
    digest = validate_download_integrity(content, expected_bytes, expected_sha256)
    target = unique_download_target(target, digest)
    write_private_download_file(target, content)
    return str(target), len(content), digest


def download_file_proof(
    file_path: str,
    byte_count: int,
    digest: str,
    *,
    source_path: str = "",
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
    placeholder_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    original_path = Path(file_path)
    resolved_path = original_path.resolve()
    output_dir = configured_download_dir().resolve()
    try:
        relative_path = resolved_path.relative_to(output_dir)
    except ValueError as exc:
        raise B1RemoteNodeError("downloaded artifact path escaped the configured output directory") from exc
    if original_path.is_symlink():
        raise B1RemoteNodeError("downloaded artifact must not be a symlink")
    stat_result = resolved_path.stat()
    content = resolved_path.read_bytes()
    file_digest = validate_download_integrity(content, byte_count, digest)
    mode = stat_result.st_mode & 0o777
    private_file_mode = os.name == "nt" or (mode & 0o077) == 0
    if not private_file_mode:
        raise B1RemoteNodeError(f"downloaded artifact mode is too broad: {oct(mode)}")
    proof = {
        "filename": resolved_path.name,
        "relative_path": relative_path.as_posix(),
        "byte_count": byte_count,
        "stat_size": stat_result.st_size,
        "sha256": digest,
        "file_sha256": file_digest,
        "path_within_download_dir": True,
        "symlink": False,
        "private_file_mode": private_file_mode,
        "file_mode": oct(mode),
    }
    if source_path:
        proof["source_path"] = source_path
    if expected_bytes is not None:
        proof["expected_bytes"] = expected_bytes
    if expected_sha256 is not None:
        proof["expected_sha256"] = expected_sha256
    if placeholder_proof is not None:
        proof["placeholder_proof"] = placeholder_proof
    return proof


def unique_download_target(target: Path, digest: str) -> Path:
    if not target.exists() and not target.is_symlink():
        return target
    stem = target.stem[:120] or "b1-artifact"
    suffix = target.suffix
    for index in range(1, 1000):
        serial = "" if index == 1 else f"-{index}"
        candidate = target.with_name(f"{stem}-{digest[:12]}{serial}{suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise B1RemoteNodeError("artifact download target already exists too many times")


def write_private_download_file(target: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
    finally:
        if fd >= 0:
            os.close(fd)
    if os.name != "nt":
        target.chmod(0o600)


def extract_chat_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    output = response.get("output")
    if isinstance(output, list):
        fragments: list[str] = []
        for item in output:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        fragments.append(part["text"])
        if fragments:
            return "\n".join(fragments)
    return json_output(response)


def submit_media_job(modality: str, operation: str, model: str, input_payload: dict[str, Any], priority: str = "single_image", runtime_policy: str = "any") -> dict[str, Any]:
    return request_json(
        "/v1/media/jobs",
        {
            "modality": modality,
            "operation": operation,
            "model": model,
            "input": input_payload,
            "priority": priority,
            "runtime_policy": runtime_policy,
        },
        method="POST",
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )


def wait_for_job(job_reference: str, timeout_seconds: int, poll_interval_seconds: float) -> dict[str, Any]:
    path = media_job_route(job_reference, "self")
    deadline = time.monotonic() + max(1, timeout_seconds)
    interval = max(0.25, poll_interval_seconds)
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        last = request_json(path)
        if isinstance(last.get("links"), dict):
            path = media_job_route(json_output(last), "self")
        if str(last.get("state")) in TERMINAL_JOB_STATES:
            return last
        time.sleep(interval)
    raise B1RemoteNodeError(f"job did not finish before timeout; last state={last.get('state') if last else 'unknown'}")


class B1ListModels:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("aliases_json", "raw_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self):
        payload = request_json("/v1/models")
        aliases = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
        return (json_output(aliases), json_output(payload))


class B1SelectModelAlias:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"alias": ("STRING", {"default": "chat-default"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("model",)
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, alias: str):
        model = alias.strip()
        if not model:
            raise B1RemoteNodeError("model alias is required")
        return (model,)


class B1ChatText:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "chat-default"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "system": ("STRING", {"multiline": True, "default": ""}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 512, "min": 1, "max": 16384}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "raw_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, system: str = "", temperature: float = 0.7, max_tokens: int = 512):
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = request_json(
            "/v1/chat/completions",
            {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            method="POST",
            timeout_seconds=1800,
        )
        return (extract_chat_text(response), json_output(response))


class B1VisionAnalysis:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "vision-default"}),
                "prompt": ("STRING", {"multiline": True, "default": "Describe this image."}),
                "image_reference": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "raw_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, image_reference: str):
        image = require_string_media_reference(image_reference, "image")
        response = request_json(
            "/v1/responses",
            {
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image},
                        ],
                    }
                ],
            },
            method="POST",
            timeout_seconds=1800,
        )
        return (extract_chat_text(response), json_output(response))


class B1Embeddings:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "embedding-default"}),
                "text": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("embeddings_json",)
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, text: str):
        return (json_output(request_json("/v1/embeddings", {"model": model, "input": text}, method="POST", timeout_seconds=1800)),)


class B1SubmitMediaJob:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "modality": ("STRING", {"default": "image"}),
                "operation": ("STRING", {"default": "generation"}),
                "model": ("STRING", {"default": "image-default"}),
                "input_json": ("STRING", {"multiline": True, "default": "{\"prompt\":\"\"}"}),
            },
            "optional": {
                "priority": ("STRING", {"default": "single_image"}),
                "runtime_policy": ("STRING", {"default": "any"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, modality: str, operation: str, model: str, input_json: str, priority: str = "single_image", runtime_policy: str = "any"):
        job = submit_media_job(modality, operation, model, parse_json_object(input_json, "input_json"), priority=priority, runtime_policy=runtime_policy)
        return (str(job.get("id", "")), json_output(job))


class B1TextToImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "image-default"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "parameters_json": ("STRING", {"multiline": True, "default": "{}"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, parameters_json: str = "{}"):
        input_payload = {"prompt": prompt, **parse_json_object(parameters_json, "parameters_json")}
        job = submit_media_job("image", "generation", model, input_payload, priority="single_image")
        return (str(job.get("id", "")), json_output(job))


class B1ImageToImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "image-edit"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "image_reference": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "parameters_json": ("STRING", {"multiline": True, "default": "{}"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, image_reference: str, parameters_json: str = "{}"):
        input_payload = {"prompt": prompt, "image": require_media_reference(image_reference, "image"), **parse_json_object(parameters_json, "parameters_json")}
        job = submit_media_job("image", "edit", model, input_payload, priority="single_image")
        return (str(job.get("id", "")), json_output(job))


class B1TextToVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "video-text"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "parameters_json": ("STRING", {"multiline": True, "default": "{}"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, parameters_json: str = "{}"):
        input_payload = {"prompt": prompt, **parse_json_object(parameters_json, "parameters_json")}
        job = submit_media_job("video", "text-to-video", model, input_payload, priority="video")
        return (str(job.get("id", "")), json_output(job))


class B1ImageToVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "video-image"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "image_reference": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "parameters_json": ("STRING", {"multiline": True, "default": "{}"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, prompt: str, image_reference: str, parameters_json: str = "{}"):
        input_payload = {"prompt": prompt, "image": require_media_reference(image_reference, "image"), **parse_json_object(parameters_json, "parameters_json")}
        job = submit_media_job("video", "image-to-video", model, input_payload, priority="video")
        return (str(job.get("id", "")), json_output(job))


class B1TextToSpeech:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "tts-fast"}),
                "text": ("STRING", {"multiline": True, "default": ""}),
                "voice": ("STRING", {"default": "default"}),
            },
            "optional": {
                "response_format": ("STRING", {"default": "wav"}),
                "runtime_policy": ("STRING", {"default": "any"}),
                "filename": ("STRING", {"default": "speech.wav"}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("file_path", "bytes", "sha256", "proof_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, text: str, voice: str, response_format: str = "wav", runtime_policy: str = "any", filename: str = "speech.wav"):
        file_path, byte_count, digest, placeholder_proof = text_to_speech_download(model, text, voice, response_format, runtime_policy, filename)
        proof = download_file_proof(
            file_path,
            byte_count,
            digest,
            source_path="/v1/audio/speech",
            placeholder_proof=placeholder_proof,
        )
        return file_path, byte_count, digest, json_output(proof)


class B1SpeechToText:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "stt-default"}),
                "audio_base64": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "language": ("STRING", {"default": ""}),
                "runtime_policy": ("STRING", {"default": "any"}),
                "audio_mime_type": ("STRING", {"default": "audio/wav"}),
                "filename": ("STRING", {"default": "audio.wav"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "raw_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, audio_base64: str, language: str = "", runtime_policy: str = "any", audio_mime_type: str = "audio/wav", filename: str = "audio.wav"):
        audio = decode_base64_payload(audio_base64, "audio")
        fields = {"model": model.strip() or "stt-default"}
        if language.strip():
            fields["language"] = language.strip()
        if runtime_policy.strip():
            fields["runtime_policy"] = runtime_policy.strip()
        body, content_type = multipart_form_data(
            fields,
            [
                (
                    "file",
                    filename.strip() or "audio.wav",
                    audio_mime_type.strip() or "audio/wav",
                    audio,
                )
            ],
        )
        response = request_json(
            "/v1/audio/transcriptions",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
            timeout_seconds=1800,
        )
        return (str(response.get("text", "")), json_output(response))


class B1UploadMediaBase64:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "field_name": ("STRING", {"default": "image"}),
                "mime_type": ("STRING", {"default": "image/png"}),
                "filename": ("STRING", {"default": "input.png"}),
                "base64_data": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("reference_json", "upload_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, field_name: str, mime_type: str, filename: str, base64_data: str):
        field = safe_form_name(field_name or "media")
        content_type = safe_upload_mime_type(mime_type)
        upload_kind = media_kind_for_mime_type(content_type)
        expected_kind = expected_media_kind(field)
        if expected_kind and upload_kind != expected_kind:
            raise B1RemoteNodeError(f"{field} upload must use a {expected_kind}/ media type")
        default_filename = f"{field}{extension_for_content_type(content_type)}"
        upload_filename = safe_filename(filename, default_filename)
        content = decode_base64_payload(base64_data, field)
        upload = request_json(
            "/v1/media/uploads",
            method="POST",
            data=content,
            headers={
                "Content-Type": content_type,
                "X-B1-Field": field,
                "X-B1-Filename": upload_filename,
            },
        )
        reference = staged_reference_from_json(upload, upload_kind or field)
        return (json_output(reference), json_output(upload))


class B1WaitMediaJob:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "job_id": ("STRING", {"default": ""}),
                "timeout_seconds": ("INT", {"default": 600, "min": 1, "max": 86400}),
                "poll_interval_seconds": ("FLOAT", {"default": DEFAULT_POLL_INTERVAL_SECONDS, "min": 0.25, "max": 60.0, "step": 0.25}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("state", "job_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, job_id: str, timeout_seconds: int, poll_interval_seconds: float):
        job = wait_for_job(job_id.strip(), timeout_seconds, poll_interval_seconds)
        return (str(job.get("state", "")), json_output(job))


class B1CancelMediaJob:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"job_id": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_json",)
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, job_id: str):
        return (json_output(request_json(media_job_route(job_id, "cancel"), method="DELETE")),)


class B1ListJobArtifacts:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"job_id": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("artifacts_json",)
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, job_id: str):
        return (json_output(request_json(media_job_route(job_id, "artifacts", "/artifacts"))),)


class B1DownloadArtifact:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "artifact_url": ("STRING", {"default": "/artifacts/"}),
                "filename": ("STRING", {"default": ""}),
            },
            "optional": {
                "artifact_index": ("INT", {"default": 0, "min": 0, "max": 1000}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("file_path", "bytes", "sha256", "proof_json")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, artifact_url: str, filename: str = "", artifact_index: int = 0):
        path, expected_bytes, expected_digest, record_name = artifact_reference(artifact_url, artifact_index)
        content, headers = request_bytes(path, method="GET", timeout_seconds=1800)
        preferred = filename.strip() or record_name or path.rsplit("/", 1)[-1]
        file_path, byte_count, digest = write_download(content, headers, preferred, expected_bytes=expected_bytes, expected_sha256=expected_digest)
        proof = download_file_proof(
            file_path,
            byte_count,
            digest,
            source_path=path,
            expected_bytes=expected_bytes,
            expected_sha256=expected_digest,
        )
        return file_path, byte_count, digest, json_output(proof)


NODE_CLASS_MAPPINGS = {
    "B1ListModels": B1ListModels,
    "B1SelectModelAlias": B1SelectModelAlias,
    "B1ChatText": B1ChatText,
    "B1VisionAnalysis": B1VisionAnalysis,
    "B1Embeddings": B1Embeddings,
    "B1SubmitMediaJob": B1SubmitMediaJob,
    "B1TextToImage": B1TextToImage,
    "B1ImageToImage": B1ImageToImage,
    "B1TextToVideo": B1TextToVideo,
    "B1ImageToVideo": B1ImageToVideo,
    "B1TextToSpeech": B1TextToSpeech,
    "B1SpeechToText": B1SpeechToText,
    "B1UploadMediaBase64": B1UploadMediaBase64,
    "B1WaitMediaJob": B1WaitMediaJob,
    "B1CancelMediaJob": B1CancelMediaJob,
    "B1ListJobArtifacts": B1ListJobArtifacts,
    "B1DownloadArtifact": B1DownloadArtifact,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "B1ListModels": "B1 List Models",
    "B1SelectModelAlias": "B1 Select Model Alias",
    "B1ChatText": "B1 Chat / Text",
    "B1VisionAnalysis": "B1 Vision Analysis",
    "B1Embeddings": "B1 Embeddings",
    "B1SubmitMediaJob": "B1 Submit Media Job",
    "B1TextToImage": "B1 Text To Image",
    "B1ImageToImage": "B1 Image To Image",
    "B1TextToVideo": "B1 Text To Video",
    "B1ImageToVideo": "B1 Image To Video",
    "B1TextToSpeech": "B1 Text To Speech",
    "B1SpeechToText": "B1 Speech To Text",
    "B1UploadMediaBase64": "B1 Upload Media Base64",
    "B1WaitMediaJob": "B1 Wait Media Job",
    "B1CancelMediaJob": "B1 Cancel Media Job",
    "B1ListJobArtifacts": "B1 List Job Artifacts",
    "B1DownloadArtifact": "B1 Download Artifact",
}
