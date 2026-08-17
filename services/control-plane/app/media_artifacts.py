from __future__ import annotations

import errno
import hashlib
import hmac
import mimetypes
import os
import stat
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

ALLOWED_AUDIO_INPUT_MIME_TYPES = {mime_type for mime_type in ALLOWED_INPUT_MIME_TYPES if mime_type.startswith("audio/")}
UPLOAD_ID_PATTERN = "upload_"


def safe_artifact_segment(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in value)
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or fallback


def artifact_store_path(artifact_root: Path, relative_path: str) -> Path:
    artifact_url = artifact_policy.artifact_url_for_path(relative_path)
    normalized_relative = artifact_url.removeprefix("/artifacts/")
    if artifact_root.is_symlink():
        raise ValueError("artifact root is a symlink")
    root = artifact_root.resolve()
    current = root
    for segment in normalized_relative.split("/"):
        current = current / segment
        if current.is_symlink():
            raise ValueError("artifact path contains a symlink")
    path = current.resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("artifact path escapes artifact root")
    return path


def read_regular_file_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("artifact file is a symlink") from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("artifact file is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_regular_file_replace_target(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode):
        raise ValueError("artifact file is a symlink")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("artifact file is not a regular file")


def write_regular_file_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
        ensure_regular_file_replace_target(path)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)


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


def _u16le(content: bytes, offset: int) -> int:
    return int.from_bytes(content[offset : offset + 2], "little")


def _u32be(content: bytes, offset: int) -> int:
    return int.from_bytes(content[offset : offset + 4], "big")


def _u32le(content: bytes, offset: int) -> int:
    return int.from_bytes(content[offset : offset + 4], "little")


def _looks_like_png(content: bytes) -> bool:
    if len(content) < 45 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    seen_ihdr = False
    chunks_seen = 0
    while offset + 12 <= len(content) and chunks_seen < 256:
        chunk_length = _u32be(content, offset)
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_length
        crc_end = data_end + 4
        if data_end < data_start or crc_end > len(content):
            return False
        if chunks_seen == 0:
            if chunk_type != b"IHDR" or chunk_length != 13:
                return False
            width = _u32be(content, data_start)
            height = _u32be(content, data_start + 4)
            bit_depth = content[data_start + 8]
            color_type = content[data_start + 9]
            if width <= 0 or height <= 0 or bit_depth not in {1, 2, 4, 8, 16} or color_type not in {0, 2, 3, 4, 6}:
                return False
            seen_ihdr = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"IEND":
            return seen_ihdr and chunk_length == 0 and crc_end == len(content)
        offset = crc_end
        chunks_seen += 1
    return False


def _looks_like_jpeg(content: bytes) -> bool:
    return len(content) >= 4 and content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")


def _looks_like_gif(content: bytes) -> bool:
    if len(content) < 14 or not content.startswith((b"GIF87a", b"GIF89a")) or not content.endswith(b";"):
        return False
    width = _u16le(content, 6)
    height = _u16le(content, 8)
    if width <= 0 or height <= 0:
        return False
    packed = content[10]
    global_color_table_size = 3 * (2 << (packed & 0x07)) if packed & 0x80 else 0
    return len(content) >= 13 + global_color_table_size + 1


def _riff_declared_end(content: bytes, form_type: bytes) -> int | None:
    if len(content) < 12 or not content.startswith(b"RIFF") or content[8:12] != form_type:
        return None
    declared_size = _u32le(content, 4)
    declared_end = declared_size + 8
    if declared_end != len(content) or declared_end < 12:
        return None
    return declared_end


def _looks_like_webp(content: bytes) -> bool:
    declared_end = _riff_declared_end(content, b"WEBP")
    if declared_end is None or declared_end < 20:
        return False
    chunk_type = content[12:16]
    chunk_size = _u32le(content, 16)
    return chunk_type in {b"VP8 ", b"VP8L", b"VP8X"} and 20 + chunk_size + (chunk_size % 2) <= declared_end


def _looks_like_wav(content: bytes) -> bool:
    # WAV producers sometimes append a non-RIFF trailer (for example, capture
    # metadata) after the declared RIFF container.  Trust neither the header
    # nor the trailer: validate the complete declared container and ignore only
    # bytes beyond that container.
    if len(content) < 12 or not content.startswith(b"RIFF") or content[8:12] != b"WAVE":
        return False
    declared_end = _u32le(content, 4) + 8
    if declared_end < 12:
        return False
    # Streaming WAV encoders commonly use ``0xffffffff`` for the RIFF size
    # because the final length is not known when the header is emitted.  Scan
    # only the bounded bytes we received; every chunk still has to fit within
    # that actual buffer before the upload is accepted.
    declared_end = min(declared_end, len(content))
    offset = 12
    has_fmt = False
    has_data = False
    while offset + 8 <= declared_end:
        chunk_type = content[offset : offset + 4]
        chunk_size = _u32le(content, offset + 4)
        data_start = offset + 8
        # Streaming WAV writers can leave both RIFF and data sizes as
        # ``0xffffffff``.  Treat that sentinel as the remaining bounded input
        # only for the audio data chunk; an unknown chunk with this size is not
        # accepted because it cannot be structurally bounded safely.
        if chunk_type == b"data" and chunk_size == 0xFFFFFFFF:
            if data_start >= declared_end:
                return False
            has_data = True
            break
        data_end = data_start + chunk_size
        if data_end < data_start or data_end > declared_end:
            return False
        if chunk_type == b"fmt ":
            if chunk_size < 16:
                return False
            audio_format = _u16le(content, data_start)
            channels = _u16le(content, data_start + 2)
            sample_rate = _u32le(content, data_start + 4)
            bits_per_sample = _u16le(content, data_start + 14)
            if audio_format not in {1, 3, 65534} or channels <= 0 or channels > 8 or sample_rate <= 0 or bits_per_sample <= 0:
                return False
            has_fmt = True
        elif chunk_type == b"data":
            has_data = True
        offset = data_end + (chunk_size % 2)
    return has_fmt and has_data


def _syncsafe_int(content: bytes) -> int | None:
    if len(content) != 4 or any(byte & 0x80 for byte in content):
        return None
    return (content[0] << 21) | (content[1] << 14) | (content[2] << 7) | content[3]


def _looks_like_mpeg_audio_frame(content: bytes, offset: int = 0) -> bool:
    if offset + 4 > len(content):
        return False
    header = int.from_bytes(content[offset : offset + 4], "big")
    sync = (header >> 21) & 0x7FF
    version = (header >> 19) & 0x03
    layer = (header >> 17) & 0x03
    bitrate = (header >> 12) & 0x0F
    sample_rate = (header >> 10) & 0x03
    return sync == 0x7FF and version != 0x01 and layer != 0 and bitrate not in {0, 0x0F} and sample_rate != 0x03


def _looks_like_mp3(content: bytes) -> bool:
    if content.startswith(b"ID3"):
        if len(content) < 14:
            return False
        tag_size = _syncsafe_int(content[6:10])
        if tag_size is None:
            return False
        audio_offset = 10 + tag_size
        return audio_offset < len(content) and _looks_like_mpeg_audio_frame(content, audio_offset)
    return _looks_like_mpeg_audio_frame(content)


def _looks_like_ogg(content: bytes) -> bool:
    if len(content) < 27 or not content.startswith(b"OggS") or content[4] != 0:
        return False
    page_segments = content[26]
    segment_table_end = 27 + page_segments
    if segment_table_end > len(content):
        return False
    body_size = sum(content[27:segment_table_end])
    return segment_table_end + body_size <= len(content)


def _looks_like_mp4(content: bytes) -> bool:
    if len(content) < 24:
        return False
    box_size = _u32be(content, 0)
    box_header_size = 8
    if box_size == 1:
        if len(content) < 32:
            return False
        box_size = int.from_bytes(content[8:16], "big")
        box_header_size = 16
    if content[4:8] != b"ftyp" or box_size < box_header_size + 8 or box_size > len(content):
        return False
    brand_start = box_header_size
    major_brand = content[brand_start : brand_start + 4]
    compatible_brands = content[brand_start + 8 : box_size]
    if not all(32 <= byte <= 126 for byte in major_brand):
        return False
    return len(compatible_brands) >= 4 and len(compatible_brands) % 4 == 0 and all(
        32 <= byte <= 126 for byte in compatible_brands
    )


def _looks_like_webm(content: bytes) -> bool:
    if len(content) < 16 or not content.startswith(b"\x1a\x45\xdf\xa3"):
        return False
    return b"webm" in content[:4096].lower()


def sniff_media_mime_type(content: bytes, declared_mime_type: str | None = None) -> str:
    del declared_mime_type
    if _looks_like_png(content):
        return "image/png"
    if _looks_like_jpeg(content):
        return "image/jpeg"
    if _looks_like_webp(content):
        return "image/webp"
    if _looks_like_gif(content):
        return "image/gif"
    if _looks_like_wav(content):
        return "audio/wav"
    if _looks_like_mp3(content):
        return "audio/mpeg"
    if _looks_like_ogg(content):
        return "audio/ogg"
    if _looks_like_mp4(content):
        return "video/mp4"
    if _looks_like_webm(content):
        return "video/webm"
    return "application/octet-stream"


def require_allowed_input_mime_type(content: bytes, declared_mime_type: str | None = None) -> str:
    mime_type = sniff_media_mime_type(content, declared_mime_type)
    if mime_type not in ALLOWED_INPUT_MIME_TYPES:
        raise ValueError(f"unsupported media input type: {mime_type}")
    return mime_type


def require_allowed_audio_input_mime_type(content: bytes, declared_mime_type: str | None = None) -> str:
    mime_type = sniff_media_mime_type(content, declared_mime_type)
    if mime_type not in ALLOWED_AUDIO_INPUT_MIME_TYPES:
        raise ValueError(f"unsupported audio input type: {mime_type}")
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
    target = artifact_store_path(artifact_root, relative)
    digest = hashlib.sha256(content)
    write_regular_file_bytes(target, content)
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
    try:
        content = read_regular_file_bytes(target)
    except FileNotFoundError:
        raise FileNotFoundError(relative) from None
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


def staged_input_reference_for_upload_id(artifact_root: Path, *, owner_id: str, upload_id: str) -> dict[str, Any]:
    if not isinstance(upload_id, str) or not upload_id.startswith(UPLOAD_ID_PATTERN) or len(upload_id) != len(UPLOAD_ID_PATTERN) + 32:
        raise ValueError("staged upload id is invalid")
    suffix = upload_id.removeprefix(UPLOAD_ID_PATTERN)
    if any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("staged upload id is invalid")
    owner_segment = safe_artifact_segment(owner_id, "owner")
    directory = artifact_store_path(artifact_root, f"inputs/{owner_segment}/{upload_id}")
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"inputs/{owner_segment}/{upload_id}") from None
    if stat.S_ISLNK(directory_stat.st_mode):
        raise ValueError("staged upload directory is a symlink")
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ValueError("staged upload path is not a directory")
    files = []
    for candidate in directory.iterdir():
        if candidate.name.startswith("."):
            continue
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise ValueError("staged upload file is a symlink")
        if stat.S_ISREG(candidate_stat.st_mode):
            files.append(candidate)
    if not files:
        raise FileNotFoundError(f"inputs/{owner_segment}/{upload_id}")
    if len(files) != 1:
        raise ValueError("staged upload id contains multiple files")
    target = artifact_store_path(artifact_root, f"inputs/{owner_segment}/{upload_id}/{files[0].name}")
    content = read_regular_file_bytes(target)
    mime_type = require_allowed_input_mime_type(content, mimetypes.guess_type(target.name)[0])
    filename = safe_filename(target.name, f"media{extension_for_mime_type(mime_type)}")
    field = filename.split("-", 1)[0] if "-" in filename else kind_for_mime_type(mime_type)
    relative = target.relative_to(artifact_root.resolve()).as_posix()
    return {
        "source": "staged_upload",
        "id": upload_id,
        "field": safe_artifact_segment(field, "media"),
        "kind": kind_for_mime_type(mime_type),
        "mime_type": mime_type,
        "filename": filename,
        "path": relative,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


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
    target = artifact_store_path(artifact_root, relative)
    digest = hashlib.sha256(content)
    write_regular_file_bytes(target, content)
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
