from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import time
import urllib.error
from urllib.parse import unquote, urlsplit, urlunsplit
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CHUNK_SIZE = 1024 * 1024
CACHE_STATE_VERSION = "b1-model-client-cache/v1"
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
MODELHUB_URL_ENV = "B1_MODELHUB_URL"
TOKEN_ENV = "B1_MODELHUB_TOKEN"
TOKEN_FILE_ENV = "B1_MODELHUB_TOKEN_FILE"
CA_FILE_ENV = "B1_MODELHUB_CA_FILE"
ALLOW_INSECURE_HTTP_ENV = "B1_MODEL_CLIENT_ALLOW_INSECURE_HTTP"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def has_unsafe_path_segment(path: str) -> bool:
    for segment in path.split("/"):
        if not segment:
            continue
        if BAD_PERCENT_ESCAPE_RE.search(segment):
            return True
        try:
            decoded = unquote(segment, errors="strict")
        except UnicodeDecodeError:
            return True
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded:
            return True
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
            return True
    return False


def normalize_model_id(value: str) -> str:
    model_id = str(value or "").strip()
    if not MODEL_ID_RE.fullmatch(model_id):
        raise RuntimeError(f"model id or alias is unsafe: {value}")
    return model_id


def validate_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError("Model Hub base URL is required")
    if "\\" in raw or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RuntimeError("Model Hub base URL contains unsafe characters")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"invalid Model Hub base URL: {raw}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Model Hub base URL must be an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise RuntimeError("Model Hub base URL must not contain credentials")
    if parsed.query or parsed.fragment or "?" in raw or "#" in raw:
        raise RuntimeError("Model Hub base URL must not contain a query string or fragment")
    if "\\" in parsed.netloc or "\\" in parsed.path or not parsed.hostname:
        raise RuntimeError("Model Hub base URL contains unsafe characters")
    path = parsed.path.rstrip("/")
    if has_unsafe_path_segment(path):
        raise RuntimeError("Model Hub base URL path contains unsafe traversal segments")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def request_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or "?" in path or "#" in path or "\\" in path or has_unsafe_path_segment(path):
        raise RuntimeError("Model Hub request path is unsafe")
    return validate_base_url(base_url) + path


def enforce_token_transport_security(url: str, token: str | None) -> None:
    if not token:
        return
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and env_bool(ALLOW_INSECURE_HTTP_ENV):
        return
    raise RuntimeError(
        "refusing to send a Model Hub bearer token over plain HTTP; use HTTPS "
        f"or set {ALLOW_INSECURE_HTTP_ENV}=true only for isolated development"
    )


def modelhub_request_url(base_url: str, path: str, token: str | None) -> str:
    url = request_url(base_url, path)
    enforce_token_transport_security(url, token)
    return url


def resolve_ca_file(args: argparse.Namespace) -> str | None:
    value = getattr(args, "ca_file", None)
    if value is None:
        value = os.getenv(CA_FILE_ENV)
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"CA file is not a regular file: {path}")
    return str(path)


def modelhub_urlopen(request: urllib.request.Request, *, timeout: int, ca_file: str | None = None) -> Any:
    if not ca_file:
        return urllib.request.urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=ca_file)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def require_private_token_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise RuntimeError(f"cannot inspect token file permissions: {path}") from exc
    if mode & 0o077:
        raise RuntimeError(f"token file must be private to the current user, for example chmod 0600: {path}")


def read_token_file(path_value: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise RuntimeError(f"token file is not a regular file: {path}")
    require_private_token_file(path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read token file: {path}") from exc
    if not token:
        raise RuntimeError(f"token file is empty: {path}")
    return token


def resolve_token(args: argparse.Namespace) -> str | None:
    token_value = args.token if getattr(args, "token", None) is not None else os.getenv(TOKEN_ENV)
    token = str(token_value).strip() if token_value else None
    token_file_value = getattr(args, "token_file", None)
    if token_file_value is None:
        token_file_value = os.getenv(TOKEN_FILE_ENV)
    token_file = str(token_file_value).strip() if token_file_value else None
    if token and token_file:
        raise RuntimeError(f"ambiguous Model Hub credentials: use either --token/{TOKEN_ENV} or --token-file/{TOKEN_FILE_ENV}, not both")
    if token_file:
        return read_token_file(token_file)
    return token


def request_json(
    base_url: str,
    path: str,
    token: str | None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    ca_file: str | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(modelhub_request_url(base_url, path, token), data=data, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with modelhub_urlopen(request, timeout=30, ca_file=ca_file) as response:
        return json.loads(response.read().decode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def state_path(cache: Path) -> Path:
    return cache / "b1-model-client-state.json"


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(PRIVATE_DIR_MODE)


def chmod_private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(PRIVATE_FILE_MODE)


def is_regular_file_no_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def require_regular_file_no_symlink(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"{label} must be a regular file, not a symlink or special file: {path}")


def existing_regular_file_size(path: Path, label: str) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    require_regular_file_no_symlink(path, label)
    return path.stat().st_size


def private_open_binary(path: Path, *, append: bool = False) -> Any:
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        require_regular_file_no_symlink(path, "cache file")
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_APPEND if append else os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if os.name != "nt":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        return os.fdopen(fd, "ab" if append else "wb")
    except Exception:
        os.close(fd)
        raise


def write_private_text_file(path: Path, text: str) -> None:
    with private_open_binary(path, append=False) as handle:
        handle.write(text.encode("utf-8"))


def empty_state() -> dict[str, Any]:
    return {"format": CACHE_STATE_VERSION, "pins": {}, "managed_blobs": {}, "updated_at": utc_now()}


def load_state(cache: Path) -> dict[str, Any]:
    path = state_path(cache)
    if not path.is_file():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read cache state: {path}") from exc
    if state.get("format") != CACHE_STATE_VERSION:
        raise RuntimeError(f"unsupported cache state format: {path}")
    state.setdefault("pins", {})
    state.setdefault("managed_blobs", {})
    if not isinstance(state["pins"], dict):
        raise RuntimeError(f"cache state pins must be an object: {path}")
    if not isinstance(state["managed_blobs"], dict):
        raise RuntimeError(f"cache state managed_blobs must be an object: {path}")
    return state


def save_state(cache: Path, state: dict[str, Any]) -> None:
    ensure_private_directory(cache)
    state["format"] = CACHE_STATE_VERSION
    state["updated_at"] = utc_now()
    tmp = state_path(cache).with_suffix(".json.partial")
    write_private_text_file(tmp, json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(state_path(cache))
    chmod_private_file(state_path(cache))


def normalize_model_list(models: list[str]) -> list[str]:
    return sorted({normalize_model_id(model) for model in models if str(model or "").strip()})


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def catalog_default_models(base_url: str, token: str | None, ca_file: str | None = None) -> list[str]:
    aliases = request_json(base_url, "/modelhub/v1/catalog", token, ca_file=ca_file).get("aliases", [])
    models: list[str] = []
    for item in aliases:
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("alias")
        if isinstance(value, str) and value.strip():
            models.append(value.strip())
    return normalize_model_list(models)


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def normalize_sha256(value: Any, *, label: str = "sha256") -> str:
    digest = str(value or "").strip().lower()
    if not is_sha256(digest):
        raise RuntimeError(f"{label} must be a 64-character SHA-256 hex digest")
    return digest


def blob_target(cache: Path, sha256: str) -> Path:
    return cache / "blobs" / normalize_sha256(sha256, label="blob")


def model_record(base_url: str, token: str | None, model_id: str, ca_file: str | None = None) -> dict[str, Any]:
    safe_model_id = normalize_model_id(model_id)
    record = request_json(base_url, f"/modelhub/v1/models/{safe_model_id}", token, ca_file=ca_file)
    if record.get("resolved_model"):
        versions = request_json(base_url, f"/modelhub/v1/models/{safe_model_id}/versions", token, ca_file=ca_file).get("versions", [])
        if not versions:
            raise RuntimeError(f"{safe_model_id}: alias has no manifest versions")
        return versions[0]
    return record


def redacted_source_metadata(source: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    metadata = {key: value for key, value in source.items() if key != "url"}
    url = source.get("url")
    if isinstance(url, str) and url:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            netloc = hostname
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except ValueError:
            metadata["url"] = ""
            metadata["url_redacted"] = True
            return metadata
        metadata["url"] = safe_url
        metadata["url_redacted"] = safe_url != url
    return metadata


def plan_model_metadata(record: dict[str, Any]) -> dict[str, Any]:
    resolved = record.get("resolved_model") if isinstance(record.get("resolved_model"), dict) else {}
    license_info = dict(record.get("license") or {})
    model_id = record.get("id") or resolved.get("id") or record.get("root")
    version = record.get("version") or resolved.get("version")
    display_name = record.get("display_name") or resolved.get("display_name") or model_id
    requires_license_acceptance = bool(record.get("requires_license_acceptance") or license_info.get("acceptance_required"))
    return without_none(
        {
            "id": model_id,
            "version": version,
            "display_name": display_name,
            "modality": record.get("modality"),
            "operations": list(record.get("operations") or []),
            "preferred_runtime": record.get("preferred_runtime"),
            "source": redacted_source_metadata(record.get("source")),
            "license": license_info,
            "execution_modes": list(record.get("execution_modes") or []),
            "resource_estimate": record.get("resource_estimate") or {},
            "resource_label": record.get("resource_label"),
            "downloadable": bool(record.get("downloadable")),
            "requires_license_acceptance": requires_license_acceptance,
            "aliases": list(record.get("aliases") or []),
        }
    )


def without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def plan_action_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = plan_model_metadata(record)
    return {
        "model_metadata": metadata,
        "license": metadata.get("license", {}),
        "source": metadata.get("source", {}),
        "resource_estimate": metadata.get("resource_estimate", {}),
        "requires_license_acceptance": bool(metadata.get("requires_license_acceptance")),
    }


def local_blob_inventory(cache: Path) -> list[dict[str, Any]]:
    blobs = cache / "blobs"
    if not blobs.is_dir():
        return []
    inventory: list[dict[str, Any]] = []
    for path in sorted(blobs.iterdir()):
        if not is_regular_file_no_symlink(path) or path.name.endswith(".partial"):
            continue
        digest = path.name.lower()
        if not is_sha256(digest):
            continue
        if sha256_file(path) != digest:
            continue
        inventory.append({"sha256": digest, "size_bytes": path.stat().st_size})
    return inventory


def sync_plan_from_server(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
    payload = {"models": models, "installed_blobs": local_blob_inventory(cache)}
    try:
        response = request_json(base_url, "/modelhub/v1/sync/plan", token, method="POST", payload=payload, ca_file=ca_file)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise
    actions = response.get("actions")
    if not isinstance(actions, list):
        raise RuntimeError("Model Hub sync plan response did not include actions")
    normalized: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("action") in {"download", "replace", "keep"}:
            blob = normalize_sha256(action.get("blob"), label="sync plan blob")
            target = blob_target(cache, blob)
            action = {
                **action,
                "blob": blob,
                "path": str(target),
                "partial": str(target.with_suffix(".partial")),
                "resume_from": target.with_suffix(".partial").stat().st_size if target.with_suffix(".partial").exists() else 0,
            }
        normalized.append(action)
    return normalized


def planned_actions(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
    try:
        return sync_plan_from_server(base_url, token, cache, models, ca_file=ca_file)
    except urllib.error.HTTPError as exc:
        if exc.code not in {404, 405}:
            raise
        close = getattr(exc, "close", None)
        if callable(close):
            close()
    except urllib.error.URLError:
        raise
    actions: list[dict[str, Any]] = []
    for model_id in models:
        record = model_record(base_url, token, model_id, ca_file=ca_file)
        metadata = plan_action_metadata(record)
        if not record.get("downloadable"):
            actions.append({"model": model_id, "action": "skip", "reason": "model is not downloadable", **metadata})
            continue
        for file_record in record.get("files", []):
            sha256 = normalize_sha256(file_record["sha256"], label=f"{model_id} file sha256")
            target = blob_target(cache, sha256)
            if target.exists() and sha256_file(target) == sha256:
                actions.append({"model": model_id, "blob": sha256, "action": "keep", "path": str(target), **metadata})
                continue
            partial = target.with_suffix(".partial")
            resume_from = partial.stat().st_size if partial.exists() else 0
            actions.append(
                {
                    "model": model_id,
                    "blob": sha256,
                    "action": "download",
                    "path": str(target),
                    "partial": str(partial),
                    "expected_size": file_record["size_bytes"],
                    "resume_from": resume_from,
                    **metadata,
                }
            )
    return actions


def mark_managed_blob(state: dict[str, Any], sha256: str, expected_size: int, source: dict[str, Any] | None = None) -> None:
    managed = state.setdefault("managed_blobs", {})
    existing = managed.get(sha256, {})
    managed[sha256] = {
        **existing,
        "sha256": sha256,
        "size_bytes": expected_size,
        "last_verified_at": utc_now(),
        "source": source or existing.get("source") or {},
    }


def selected_models(base_url: str, token: str | None, cache: Path, requested_models: list[str], ca_file: str | None = None) -> list[str]:
    return normalize_model_list(requested_models) or pinned_models(cache) or catalog_default_models(base_url, token, ca_file=ca_file)


def expected_etag(sha256: str) -> str:
    return f'"sha256:{sha256.lower()}"'


def header_value(headers: Any, name: str) -> str:
    value = headers.get(name) if hasattr(headers, "get") else None
    if value is None and hasattr(headers, "items"):
        lowered = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lowered:
                value = candidate
                break
    return str(value).strip() if value is not None else ""


def validate_blob_response_headers(sha256: str, expected_size: int, status: int, headers: Any, resume_from: int) -> None:
    expected_statuses = {206} if resume_from else {200}
    if status not in expected_statuses:
        expected_text = "206" if resume_from else "200"
        raise RuntimeError(f"{sha256}: unexpected HTTP status {status}; expected {expected_text}")
    etag = header_value(headers, "ETag")
    if not etag:
        raise RuntimeError(f"{sha256}: missing ETag")
    if etag != expected_etag(sha256):
        raise RuntimeError(f"{sha256}: unexpected ETag {etag}")
    checksum = header_value(headers, "X-Checksum-SHA256")
    if not checksum:
        raise RuntimeError(f"{sha256}: missing X-Checksum-SHA256")
    if checksum.lower() != sha256:
        raise RuntimeError(f"{sha256}: unexpected X-Checksum-SHA256 {checksum}")
    length = header_value(headers, "Content-Length")
    if not length:
        raise RuntimeError(f"{sha256}: missing Content-Length")
    try:
        actual_length = int(length)
    except ValueError as exc:
        raise RuntimeError(f"{sha256}: invalid Content-Length {length}") from exc
    expected_remaining = expected_size - resume_from if resume_from and status == 206 else expected_size
    if actual_length != expected_remaining:
        raise RuntimeError(f"{sha256}: expected Content-Length {expected_remaining}, got {actual_length}")
    if not (resume_from and status == 206):
        return
    content_range = header_value(headers, "Content-Range")
    match = CONTENT_RANGE_RE.fullmatch(content_range)
    if not match:
        raise RuntimeError(f"{sha256}: unexpected Content-Range {content_range or '<missing>'}")
    start, end, total = (int(part) for part in match.groups())
    if start != resume_from or end != expected_size - 1 or total != expected_size:
        raise RuntimeError(f"{sha256}: unexpected Content-Range {content_range}; expected bytes {resume_from}-{expected_size - 1}/{expected_size}")


def download_blob(
    base_url: str,
    token: str | None,
    sha256: str,
    expected_size: int,
    target: Path,
    *,
    source: dict[str, Any] | None = None,
    accept_licenses: bool = False,
    ca_file: str | None = None,
) -> dict[str, Any]:
    sha256 = normalize_sha256(sha256, label="blob")
    ensure_private_directory(target.parent)
    partial = target.with_suffix(".partial")
    if target.exists() or target.is_symlink():
        require_regular_file_no_symlink(target, "cache blob target")
        if sha256_file(target) == sha256:
            chmod_private_file(target)
            return {"blob": sha256, "status": "kept", "path": str(target), "size_bytes": target.stat().st_size}

    resume_from = existing_regular_file_size(partial, "partial cache blob")
    if resume_from >= expected_size:
        if sha256_file(partial) == sha256:
            partial.replace(target)
            chmod_private_file(target)
            return {"blob": sha256, "status": "published", "path": str(target)}
        partial.unlink()
        resume_from = 0

    while True:
        request = urllib.request.Request(modelhub_request_url(base_url, f"/modelhub/v1/blobs/{sha256}", token))
        request.add_header("Accept", "application/octet-stream")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if accept_licenses and source:
            accepted_refs = accepted_license_refs_for_action(source)
            if accepted_refs:
                request.add_header("X-B1-Accept-License", ", ".join(sorted(accepted_refs)))
        if resume_from:
            request.add_header("Range", f"bytes={resume_from}-")

        try:
            response_context = modelhub_urlopen(request, timeout=120, ca_file=ca_file)
        except urllib.error.HTTPError as exc:
            if resume_from and exc.code == 416:
                partial.unlink(missing_ok=True)
                resume_from = 0
                close = getattr(exc, "close", None)
                if callable(close):
                    close()
                continue
            raise

        with response_context as response:
            status = getattr(response, "status", response.getcode())
            if resume_from and status != 206:
                if status != 200:
                    raise RuntimeError(f"{sha256}: unexpected HTTP status {status}; expected 206 or 200 when resuming")
                partial.unlink(missing_ok=True)
                resume_from = 0
            validate_blob_response_headers(sha256, expected_size, status, getattr(response, "headers", {}), resume_from)
            append = bool(resume_from and status == 206)
            with private_open_binary(partial, append=append) as handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
        break

    actual_size = partial.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(f"{sha256}: expected {expected_size} bytes, downloaded {actual_size}")
    actual_sha = sha256_file(partial)
    if actual_sha != sha256:
        raise RuntimeError(f"{sha256}: checksum mismatch, got {actual_sha}")
    partial.replace(target)
    chmod_private_file(target)
    return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size, "source": source or {}}


def list_catalog(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    print(json.dumps(request_json(args.base_url, "/modelhub/v1/catalog", args.token, ca_file=ca_file), indent=2))
    return 0


def plan(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    cache = Path(args.cache).resolve()
    models = selected_models(args.base_url, args.token, cache, args.model, ca_file=ca_file)
    actions = planned_actions(args.base_url, args.token, cache, models, ca_file=ca_file)
    print(json.dumps({"action": "plan", "dry_run": True, "cache": str(cache), "models": models, "changes": actions}, indent=2))
    return 0


def pinned_models(cache: Path) -> list[str]:
    return normalize_model_list(list((load_state(cache).get("pins") or {}).keys()))


def pin(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    cache = Path(args.cache).resolve()
    models = normalize_model_list(args.model)
    if not models:
        raise RuntimeError("pin requires at least one model")
    state = load_state(cache)
    pins = state.setdefault("pins", {})
    for model_id in models:
        record = model_record(args.base_url, args.token, model_id, ca_file=ca_file)
        metadata = plan_model_metadata(record)
        pins[model_id] = {
            "model": model_id,
            "display_name": metadata.get("display_name") or model_id,
            "pinned_at": utc_now(),
            "downloadable": bool(record.get("downloadable")),
            "model_metadata": metadata,
        }
    save_state(cache, state)
    print(json.dumps({"action": "pin", "cache": str(cache), "models": models, "pins": pins}, indent=2, sort_keys=True))
    return 0


def unpin(args: argparse.Namespace) -> int:
    cache = Path(args.cache).resolve()
    models = normalize_model_list(args.model)
    state = load_state(cache)
    pins = state.setdefault("pins", {})
    removed = []
    for model_id in models:
        if model_id in pins:
            removed.append(model_id)
            pins.pop(model_id, None)
    save_state(cache, state)
    print(json.dumps({"action": "unpin", "cache": str(cache), "removed": removed, "pins": sorted(pins)}, indent=2, sort_keys=True))
    return 0


def sync(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    cache = Path(args.cache).resolve()
    models = selected_models(args.base_url, args.token, cache, args.model, ca_file=ca_file)
    payload = sync_once(args.base_url, args.token, cache, models, dry_run=args.dry_run, accept_licenses=getattr(args, "accept_license", False), ca_file=ca_file)
    print(json.dumps(payload, indent=2))
    return 0


def action_requires_license_acceptance(action: dict[str, Any]) -> bool:
    metadata = action.get("model_metadata") if isinstance(action.get("model_metadata"), dict) else {}
    license_info = action.get("license") if isinstance(action.get("license"), dict) else metadata.get("license", {})
    return bool(action.get("requires_license_acceptance") or metadata.get("requires_license_acceptance") or license_info.get("acceptance_required"))


def accepted_license_refs_for_action(action: dict[str, Any]) -> list[str]:
    if not action_requires_license_acceptance(action):
        return []
    metadata = action.get("model_metadata") if isinstance(action.get("model_metadata"), dict) else {}
    model_id = metadata.get("id") or action.get("model_id")
    version = metadata.get("version") or action.get("version")
    if isinstance(model_id, str) and model_id and isinstance(version, str) and version:
        return [f"{model_id}@{version}"]
    return []


def license_acceptance_required_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        action
        for action in actions
        if action.get("action") in {"keep", "download", "replace"} and action_requires_license_acceptance(action)
    ]


def model_label_for_action(action: dict[str, Any]) -> str:
    metadata = action.get("model_metadata") if isinstance(action.get("model_metadata"), dict) else {}
    display_name = metadata.get("display_name") or action.get("model")
    model_id = metadata.get("id") or action.get("model")
    version = metadata.get("version")
    if version:
        return f"{display_name} ({model_id}@{version})"
    return str(display_name or model_id)


def require_license_acceptance(actions: list[dict[str, Any]], *, accepted: bool) -> None:
    if accepted:
        return
    required = license_acceptance_required_actions(actions)
    if not required:
        return
    labels = sorted({model_label_for_action(action) for action in required})
    raise RuntimeError(
        "licence acceptance required for model(s): "
        + ", ".join(labels)
        + "; run `b1-model-client plan` to review terms, then rerun sync with --accept-license"
    )


def sync_once(base_url: str, token: str | None, cache: Path, models: list[str], *, dry_run: bool = False, accept_licenses: bool = False, ca_file: str | None = None) -> dict[str, Any]:
    actions = planned_actions(base_url, token, cache, models, ca_file=ca_file)
    if dry_run:
        return {"action": "sync", "dry_run": True, "cache": str(cache), "models": models, "changes": actions}
    require_license_acceptance(actions, accepted=accept_licenses)
    ensure_private_directory(cache)
    state = load_state(cache)
    results = []
    for action in actions:
        if action["action"] == "keep" and action.get("blob"):
            blob = normalize_sha256(action["blob"], label="sync action blob")
            path = blob_target(cache, blob)
            size = action.get("expected_size")
            if size is None:
                size = path.stat().st_size if path.is_file() else 0
            if size:
                mark_managed_blob(state, blob, int(size), action)
            results.append({**action, "blob": blob, "path": str(path), "partial": str(path.with_suffix(".partial"))})
            continue
        if action["action"] not in {"download", "replace"}:
            results.append(action)
            continue
        blob = normalize_sha256(action["blob"], label="sync action blob")
        target = blob_target(cache, blob)
        normalized_action = {**action, "blob": blob, "path": str(target), "partial": str(target.with_suffix(".partial"))}
        result = download_blob(
            base_url,
            token,
            blob,
            int(action["expected_size"]),
            target,
            source=normalized_action,
            accept_licenses=accept_licenses,
            ca_file=ca_file,
        )
        mark_managed_blob(state, blob, int(action["expected_size"]), normalized_action)
        results.append(result)
    save_state(cache, state)
    return {"action": "sync", "dry_run": False, "cache": str(cache), "models": models, "changes": results}


def required_blobs_for_models(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> set[str]:
    required: set[str] = set()
    for action in planned_actions(base_url, token, cache, models, ca_file=ca_file):
        if action.get("action") in {"keep", "download", "replace"} and action.get("blob"):
            required.add(normalize_sha256(action["blob"], label="required blob"))
    return required


def prune_plan(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> dict[str, Any]:
    state = load_state(cache)
    managed = state.get("managed_blobs") or {}
    required = required_blobs_for_models(base_url, token, cache, models, ca_file=ca_file)
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for sha256, metadata in sorted(managed.items()):
        if not isinstance(sha256, str) or not is_sha256(sha256):
            continue
        path = cache / "blobs" / sha256
        entry = {"blob": sha256, "path": str(path), "size_bytes": metadata.get("size_bytes")}
        if sha256 in required:
            kept.append({**entry, "reason": "required by pinned model"})
        elif is_regular_file_no_symlink(path):
            candidates.append({**entry, "reason": "managed blob is no longer required"})
        else:
            missing.append({**entry, "reason": "managed blob is missing on disk"})
    return {
        "action": "prune",
        "cache": str(cache),
        "models": models,
        "required_blobs": sorted(required),
        "candidates": candidates,
        "kept": kept,
        "missing_managed_blobs": missing,
        "unmanaged_files_ignored": True,
    }


def apply_prune_plan(cache: Path, plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    state = load_state(cache)
    deleted: list[dict[str, Any]] = []
    for candidate in plan_payload["candidates"]:
        path = Path(candidate["path"])
        if is_regular_file_no_symlink(path) and path.parent.resolve() == (cache / "blobs").resolve() and is_sha256(path.name.lower()):
            path.unlink()
            deleted.append(candidate)
        state.get("managed_blobs", {}).pop(candidate["blob"], None)
    for missing in plan_payload["missing_managed_blobs"]:
        state.get("managed_blobs", {}).pop(missing["blob"], None)
    save_state(cache, state)
    return deleted


def prune(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    cache = Path(args.cache).resolve()
    models = normalize_model_list(args.model) or pinned_models(cache)
    if not models:
        raise RuntimeError("prune requires pinned models or explicit model arguments")
    plan_payload = prune_plan(args.base_url, args.token, cache, models, ca_file=ca_file)
    if args.dry_run:
        print(json.dumps({**plan_payload, "dry_run": True}, indent=2, sort_keys=True))
        return 0
    deleted = apply_prune_plan(cache, plan_payload)
    print(json.dumps({**plan_payload, "dry_run": False, "deleted": deleted}, indent=2, sort_keys=True))
    return 0


def daemon(args: argparse.Namespace) -> int:
    ca_file = getattr(args, "ca_file", None)
    cache = Path(args.cache).resolve()
    interval = max(1, int(args.interval_seconds))
    while True:
        models = selected_models(args.base_url, args.token, cache, args.model, ca_file=ca_file)
        sync_payload = sync_once(args.base_url, args.token, cache, models, dry_run=args.dry_run, accept_licenses=getattr(args, "accept_license", False), ca_file=ca_file)
        prune_payload = None
        if args.prune:
            prune_payload = prune_plan(args.base_url, args.token, cache, models, ca_file=ca_file)
            if not args.dry_run:
                prune_payload = {
                    **prune_payload,
                    "deleted": apply_prune_plan(cache, prune_payload),
                    "dry_run": False,
                }
            else:
                prune_payload = {**prune_payload, "dry_run": True}
        print(
            json.dumps(
                {
                    "action": "daemon-cycle",
                    "cache": str(cache),
                    "models": models,
                    "sync": sync_payload,
                    "prune": prune_payload,
                    "next_interval_seconds": interval,
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        if args.once:
            return 0
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B1 AI Hub model synchronisation client.")
    parser.add_argument("--base-url", default=os.getenv(MODELHUB_URL_ENV, "https://models.ai.b1.germering"))
    parser.add_argument("--token", default=None, help=f"Model Hub bearer token. Defaults to {TOKEN_ENV} when omitted.")
    parser.add_argument("--token-file", default=None, help=f"Read the Model Hub bearer token from a private file. Defaults to {TOKEN_FILE_ENV}.")
    parser.add_argument("--ca-file", default=None, help=f"Trust this PEM CA bundle for Model Hub TLS. Defaults to {CA_FILE_ENV}.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list")
    list_cmd.set_defaults(func=list_catalog)

    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("model", nargs="*")
    plan_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    plan_cmd.set_defaults(func=plan)

    pin_cmd = sub.add_parser("pin")
    pin_cmd.add_argument("model", nargs="+")
    pin_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    pin_cmd.set_defaults(func=pin)

    unpin_cmd = sub.add_parser("unpin")
    unpin_cmd.add_argument("model", nargs="+")
    unpin_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    unpin_cmd.set_defaults(func=unpin)

    sync_cmd = sub.add_parser("sync")
    sync_cmd.add_argument("model", nargs="*")
    sync_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    sync_cmd.add_argument("--dry-run", action="store_true")
    sync_cmd.add_argument(
        "--accept-license",
        action="store_true",
        default=env_bool("B1_MODEL_CLIENT_ACCEPT_LICENSES"),
        help="Allow syncing models whose manifests require licence acceptance after reviewing the plan.",
    )
    sync_cmd.set_defaults(func=sync)

    prune_cmd = sub.add_parser("prune")
    prune_cmd.add_argument("model", nargs="*")
    prune_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    prune_cmd.add_argument("--dry-run", action="store_true")
    prune_cmd.set_defaults(func=prune)

    daemon_cmd = sub.add_parser("daemon")
    daemon_cmd.add_argument("model", nargs="*")
    daemon_cmd.add_argument("--cache", default=os.getenv("B1_MODEL_CACHE", "./b1-model-cache"))
    daemon_cmd.add_argument("--interval-seconds", type=int, default=int(os.getenv("B1_MODEL_CLIENT_INTERVAL_SECONDS", "3600")))
    daemon_cmd.add_argument("--dry-run", action="store_true")
    daemon_cmd.add_argument(
        "--accept-license",
        action="store_true",
        default=env_bool("B1_MODEL_CLIENT_ACCEPT_LICENSES"),
        help="Allow daemon sync of models whose manifests require licence acceptance.",
    )
    daemon_cmd.add_argument("--prune", action="store_true", help="Remove managed blobs no longer required after each sync cycle.")
    daemon_cmd.add_argument("--once", action="store_true", help="Run one cycle and exit; useful for cron and smoke tests.")
    daemon_cmd.set_defaults(func=daemon)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.base_url = validate_base_url(args.base_url)
        args.token = resolve_token(args)
        args.ca_file = resolve_ca_file(args)
        return int(args.func(args))
    except urllib.error.URLError as exc:
        print(f"b1-model-client: request failed: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"b1-model-client: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
