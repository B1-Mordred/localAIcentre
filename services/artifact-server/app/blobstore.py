from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote


SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
CHUNK_SIZE = 1024 * 1024


class BlobStoreError(ValueError):
    pass


class RangeNotSatisfiable(BlobStoreError):
    pass


def validate_sha256(value: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise BlobStoreError("invalid SHA-256")
    return value.lower()


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_relative_segment(segment: str) -> None:
    if BAD_PERCENT_ESCAPE_RE.search(segment):
        raise BlobStoreError("path contains unsafe encoded segments")
    try:
        decoded = unquote(segment, errors="strict")
    except UnicodeDecodeError as exc:
        raise BlobStoreError("path contains unsafe encoded segments") from exc
    if decoded in {"", ".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded or _has_control_character(decoded):
        raise BlobStoreError("path contains unsafe encoded segments")


def resolve_inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise BlobStoreError("path must be a non-empty relative path")
    if relative.startswith("/") or "\\" in relative:
        raise BlobStoreError("path must be relative")
    segments = relative.split("/")
    for segment in segments:
        _validate_relative_segment(segment)
    resolved_root = root.resolve()
    current = resolved_root
    for segment in segments:
        current = current / segment
        if current.is_symlink():
            raise BlobStoreError("path contains symlink")
    candidate = current.resolve()
    if resolved_root not in candidate.parents and candidate != resolved_root:
        raise BlobStoreError("path escapes configured root")
    return candidate


def stat_regular_file(path: Path) -> os.stat_result:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise BlobStoreError("file not found") from exc
    except OSError as exc:
        raise BlobStoreError("file cannot be inspected safely") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise BlobStoreError("file path is a symlink")
    if not stat.S_ISREG(file_stat.st_mode):
        raise BlobStoreError("file is not a regular file")
    return file_stat


def _open_regular_file(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise BlobStoreError("file not found") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise BlobStoreError("file path is a symlink") from exc
        raise BlobStoreError("file cannot be opened safely") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise BlobStoreError("file is not a regular file")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular_file(path) as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def etag_for_sha256(sha256: str) -> str:
    return f'"sha256:{validate_sha256(sha256)}"'


def if_none_match_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    candidates = [candidate.strip() for candidate in header_value.split(",")]
    normalized_etag = normalize_entity_tag(etag)
    return "*" in candidates or any(normalize_entity_tag(candidate) == normalized_etag for candidate in candidates)


def normalize_entity_tag(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[:2].lower() == "w/":
        candidate = candidate[2:].strip()
    return candidate


def parse_byte_range(header_value: str | None, size: int) -> tuple[int, int] | None:
    if header_value is None or header_value.strip() == "":
        return None
    value = header_value.strip()
    if not value.startswith("bytes="):
        raise RangeNotSatisfiable("only bytes ranges are supported")
    ranges = value.removeprefix("bytes=").split(",")
    if len(ranges) != 1:
        raise RangeNotSatisfiable("multiple ranges are not supported")
    spec = ranges[0].strip()
    if "-" not in spec:
        raise RangeNotSatisfiable("invalid byte range")
    start_text, end_text = spec.split("-", 1)
    if start_text == "":
        if end_text == "" or not end_text.isdigit():
            raise RangeNotSatisfiable("invalid suffix range")
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise RangeNotSatisfiable("suffix range must be positive")
        if size == 0:
            raise RangeNotSatisfiable("empty resource")
        start = max(0, size - suffix_length)
        end = size - 1
        return start, end
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        raise RangeNotSatisfiable("invalid byte range")
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if size == 0 or start >= size or end < start:
        raise RangeNotSatisfiable("range outside resource")
    return start, min(end, size - 1)


def iter_file_range(path: Path, start: int, end: int, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    remaining = end - start + 1
    with _open_regular_file(path) as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
