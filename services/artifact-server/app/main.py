from __future__ import annotations

import os
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .blobstore import (
    BlobStoreError,
    RangeNotSatisfiable,
    etag_for_sha256,
    if_none_match_matches,
    iter_file_range,
    parse_byte_range,
    resolve_inside as safe_resolve_inside,
    sha256_file,
    validate_sha256,
)


app = FastAPI(title="B1 AI Hub Artifact Server", version="0.1.0")

ARTIFACT_ROOT = Path(os.getenv("B1_ARTIFACT_ROOT", "/srv/b1-ai-hub/artifacts")).resolve()
BLOB_ROOT = Path(os.getenv("B1_MODEL_BLOB_ROOT", "/srv/b1-ai-hub/models/blobs")).resolve()


def resolve_inside(root: Path, relative: str) -> Path:
    try:
        candidate = safe_resolve_inside(root, relative)
    except BlobStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="path escapes configured root")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return candidate


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "artifact-server", "artifact_root": str(ARTIFACT_ROOT), "blob_root": str(BLOB_ROOT)}


def artifact_etag(path: Path) -> str:
    stat = path.stat()
    return f'"artifact:{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def artifact_headers(path: Path) -> dict[str, str]:
    stat = path.stat()
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "Content-Length": str(stat.st_size),
        "ETag": artifact_etag(path),
        "Last-Modified": str(int(stat.st_mtime)),
    }


def media_type_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def artifact_response(path: Path, request: Request, head_only: bool = False) -> Response:
    size = path.stat().st_size
    headers = artifact_headers(path)
    if if_none_match_matches(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=304, headers={key: value for key, value in headers.items() if key != "Content-Length"})
    try:
        requested_range = parse_byte_range(request.headers.get("range"), size)
    except RangeNotSatisfiable as exc:
        error_headers = {key: value for key, value in headers.items() if key != "Content-Length"}
        error_headers["Content-Range"] = f"bytes */{size}"
        return Response(status_code=416, content=str(exc), headers=error_headers)
    if requested_range is None:
        if head_only:
            return Response(status_code=200, headers=headers, media_type=media_type_for(path))
        return StreamingResponse(
            iter_file_range(path, 0, max(0, size - 1)),
            status_code=200,
            headers=headers,
            media_type=media_type_for(path),
        )
    start, end = requested_range
    content_length = end - start + 1
    range_headers = {
        **headers,
        "Content-Length": str(content_length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    if head_only:
        return Response(status_code=206, headers=range_headers, media_type=media_type_for(path))
    return StreamingResponse(iter_file_range(path, start, end), status_code=206, headers=range_headers, media_type=media_type_for(path))


@app.get("/artifacts/{artifact_path:path}")
async def artifact_get(artifact_path: str, request: Request) -> Response:
    path = resolve_inside(ARTIFACT_ROOT, artifact_path)
    return artifact_response(path, request)


@app.head("/artifacts/{artifact_path:path}")
async def artifact_head(artifact_path: str, request: Request) -> Response:
    path = resolve_inside(ARTIFACT_ROOT, artifact_path)
    return artifact_response(path, request, head_only=True)


def blob_headers(digest: str, size: int) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "Content-Length": str(size),
        "ETag": etag_for_sha256(digest),
        "X-Checksum-SHA256": digest,
    }


def blob_response(request: Request, sha256: str, head_only: bool = False) -> Response:
    try:
        digest = validate_sha256(sha256)
    except BlobStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    path = resolve_inside(BLOB_ROOT, digest)
    actual_digest = sha256_file(path)
    if actual_digest != digest:
        raise HTTPException(status_code=409, detail="blob checksum mismatch")
    size = path.stat().st_size
    headers = blob_headers(digest, size)

    if if_none_match_matches(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=304, headers={key: value for key, value in headers.items() if key != "Content-Length"})

    try:
        requested_range = parse_byte_range(request.headers.get("range"), size)
    except RangeNotSatisfiable as exc:
        error_headers = {key: value for key, value in headers.items() if key != "Content-Length"}
        error_headers["Content-Range"] = f"bytes */{size}"
        return Response(status_code=416, content=str(exc), headers=error_headers)

    if requested_range is None:
        if head_only:
            return Response(status_code=200, headers=headers, media_type="application/octet-stream")
        return StreamingResponse(iter_file_range(path, 0, max(0, size - 1)), status_code=200, headers=headers, media_type="application/octet-stream")

    start, end = requested_range
    content_length = end - start + 1
    range_headers = {
        **headers,
        "Content-Length": str(content_length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    if head_only:
        return Response(status_code=206, headers=range_headers, media_type="application/octet-stream")
    return StreamingResponse(iter_file_range(path, start, end), status_code=206, headers=range_headers, media_type="application/octet-stream")


@app.get("/modelhub/v1/blobs/{sha256}")
async def blob_get(sha256: str, request: Request) -> Response:
    return blob_response(request, sha256)


@app.head("/modelhub/v1/blobs/{sha256}")
async def blob_head(sha256: str, request: Request) -> Response:
    return blob_response(request, sha256, head_only=True)
