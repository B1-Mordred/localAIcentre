from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path


SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
CHUNK_SIZE = 1024 * 1024


class BlobStoreError(ValueError):
    pass


class RangeNotSatisfiable(BlobStoreError):
    pass


def validate_sha256(value: str) -> str:
    if not SHA256_RE.match(value):
        raise BlobStoreError("invalid SHA-256")
    return value.lower()


def resolve_inside(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if resolved_root not in candidate.parents and candidate != resolved_root:
        raise BlobStoreError("path escapes configured root")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
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
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
