from __future__ import annotations

import hashlib
import hmac
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from . import artifacts as artifact_policy


DEFAULT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/json": ".json",
}

ALLOWED_INPUT_MIME_TYPES = {
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


def safe_artifact_segment(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in value)
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or fallback


def artifact_store_path(artifact_root: Path, relative_path: str) -> Path:
    artifact_policy.artifact_url_for_path(relative_path)
    root = artifact_root.resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents and path != root:
        raise ValueError("artifact path escapes artifact root")
    return path


def extension_for_mime_type(mime_type: str, fallback: str = ".bin") -> str:
    guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip().lower())
    return guessed or DEFAULT_EXTENSIONS.get(mime_type.split(";")[0].strip().lower(), fallback)


def kind_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";")[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return "file"


def normalize_mime_type(value: str | None) -> str:
    return (value or "").split(";")[0].strip().lower()


def sniff_media_mime_type(content: bytes, declared_mime_type: str | None = None) -> str:
    del declared_mime_type
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        return "audio/wav"
    if content.startswith(b"ID3") or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if content.startswith(b"OggS"):
        return "audio/ogg"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "video/mp4"
    if content.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return "application/octet-stream"


def require_allowed_input_mime_type(content: bytes, declared_mime_type: str | None = None) -> str:
    mime_type = sniff_media_mime_type(content, declared_mime_type)
    if mime_type not in ALLOWED_INPUT_MIME_TYPES:
        raise ValueError(f"unsupported media input type: {mime_type}")
    return mime_type


def safe_filename(value: str | None, fallback: str) -> str:
    name = (value or "").replace("\\", "/").split("/")[-1]
    cleaned = safe_artifact_segment(name, fallback)
    if "." not in cleaned:
        return fallback
    return cleaned


def write_staged_input_bytes(
    artifact_root: Path,
    *,
    owner_id: str,
    field_name: str,
    content: bytes,
    declared_mime_type: str | None = None,
    filename: str | None = None,
    max_size_bytes: int = 256 * 1024 * 1024,
) -> dict[str, Any]:
    if not content:
        raise ValueError("media input is empty")
    if len(content) > max_size_bytes:
        raise ValueError(f"media input exceeds {max_size_bytes} bytes")
    mime_type = require_allowed_input_mime_type(content, declared_mime_type)
    field_segment = safe_artifact_segment(field_name, "media")
    owner_segment = safe_artifact_segment(owner_id, "owner")
    extension = extension_for_mime_type(mime_type)
    stored_name = safe_filename(filename, f"{field_segment}{extension}")
    if not stored_name.endswith(extension):
        stored_name = f"{stored_name}{extension}"
    upload_id = f"upload_{uuid.uuid4().hex}"
    relative = f"inputs/{owner_segment}/{upload_id}/{field_segment}-{stored_name}"
    target = artifact_store_path(artifact_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    digest = hashlib.sha256(content)
    temporary.write_bytes(content)
    temporary.replace(target)
    return {
        "source": "staged_upload",
        "id": upload_id,
        "field": field_segment,
        "kind": kind_for_mime_type(mime_type),
        "mime_type": mime_type,
        "filename": stored_name,
        "path": relative,
        "bytes": len(content),
        "sha256": digest.hexdigest(),
    }


def read_staged_input_bytes(artifact_root: Path, reference: dict[str, Any]) -> tuple[bytes, str, str]:
    if reference.get("source") != "staged_upload":
        raise ValueError("input reference is not a staged upload")
    relative = reference.get("path")
    if not isinstance(relative, str) or not relative.startswith("inputs/"):
        raise ValueError("staged upload path is invalid")
    target = artifact_store_path(artifact_root, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    content = target.read_bytes()
    expected_size = reference.get("bytes")
    if isinstance(expected_size, int) and expected_size != len(content):
        raise ValueError("staged upload size mismatch")
    expected_sha256 = reference.get("sha256")
    digest = hashlib.sha256(content).hexdigest()
    if isinstance(expected_sha256, str) and expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
        raise ValueError("staged upload checksum mismatch")
    mime_type = reference.get("mime_type")
    if not isinstance(mime_type, str) or normalize_mime_type(mime_type) not in ALLOWED_INPUT_MIME_TYPES:
        mime_type = require_allowed_input_mime_type(content, mime_type if isinstance(mime_type, str) else None)
    filename = safe_filename(reference.get("filename") if isinstance(reference.get("filename"), str) else None, "media.bin")
    return content, normalize_mime_type(mime_type), filename


def write_artifact_bytes(
    artifact_root: Path,
    *,
    namespace: str,
    job_id: str,
    index: int,
    content: bytes,
    mime_type: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_segment = safe_artifact_segment(job_id, "job")
    namespace_segment = safe_artifact_segment(namespace, "runtime")
    extension = extension_for_mime_type(mime_type)
    relative = f"{namespace_segment}/{job_segment}/{index}{extension}"
    target = artifact_store_path(artifact_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    digest = hashlib.sha256(content)
    temporary.write_bytes(content)
    temporary.replace(target)
    return {
        "id": f"artifact_{namespace_segment}_{job_segment}_{index}",
        "kind": kind_for_mime_type(mime_type),
        "mime_type": mime_type,
        "path": relative,
        "url": f"/artifacts/{relative}",
        "bytes": len(content),
        "sha256": digest.hexdigest(),
        "source": source,
        "storage": "artifact-server",
        **(metadata or {}),
    }
