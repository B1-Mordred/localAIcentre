from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
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
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
SAFE_FORM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class B1RemoteNodeError(RuntimeError):
    pass


def api_base() -> str:
    return os.getenv("B1_AI_HUB_API_BASE", "https://api.ai.b1.germering").rstrip("/")


def api_key() -> str:
    return os.getenv("B1_AI_HUB_API_KEY", "")


def configured_download_dir() -> Path:
    return Path(os.getenv("B1_AI_HUB_DOWNLOAD_DIR", DEFAULT_OUTPUT_DIR)).expanduser()


def request_url(path: str) -> str:
    if not path.startswith("/"):
        raise B1RemoteNodeError("B1 API path must start with /")
    return api_base() + path


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
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return request


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
        with urllib.request.urlopen(
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
        with urllib.request.urlopen(
            build_request(path, method=method, payload=payload, data=data, headers=headers),
            timeout=timeout_seconds,
        ) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.read(), response_headers
    except urllib.error.HTTPError as exc:
        raise B1RemoteNodeError(response_error_detail(exc)) from exc
    except urllib.error.URLError as exc:
        raise B1RemoteNodeError(f"B1 API request failed: {exc.reason}") from exc


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


def require_media_reference(value: str, kind: str) -> str:
    reference = value.strip()
    if not reference:
        raise B1RemoteNodeError(f"{kind} reference is required")
    if reference.startswith("/artifacts/") or reference.startswith("data:"):
        return reference
    if reference.startswith("{"):
        parse_json_object(reference, f"{kind} reference")
        return reference
    if reference.startswith("http://") or reference.startswith("https://"):
        raise B1RemoteNodeError(f"{kind} must be uploaded to B1 or referenced as an internal artifact; external URLs are not accepted")
    raise B1RemoteNodeError(f"{kind} must be a staged JSON reference, internal /artifacts URL, or data URL")


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


def safe_filename(value: str | None, fallback: str) -> str:
    leaf = (value or "").replace("\\", "/").split("/")[-1]
    cleaned = SAFE_FILENAME_PATTERN.sub("_", leaf).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:160]


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
        safe_content_type = safe_multipart_header_value(content_type, "application/octet-stream")
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


def write_download(content: bytes, headers: dict[str, str], preferred_name: str | None = None) -> tuple[str, int, str]:
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
    digest = hashlib.sha256(content).hexdigest()
    if target.exists():
        stem = target.stem[:120] or "b1-artifact"
        target = target.with_name(f"{stem}-{digest[:12]}{target.suffix}")
    target.write_bytes(content)
    return str(target), len(content), digest


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


def wait_for_job(job_id: str, timeout_seconds: int, poll_interval_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    interval = max(0.25, poll_interval_seconds)
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        last = request_json(f"/v1/media/jobs/{urllib.parse.quote(job_id)}")
        if str(last.get("state")) in TERMINAL_JOB_STATES:
            return last
        time.sleep(interval)
    raise B1RemoteNodeError(f"job {job_id} did not finish before timeout; last state={last.get('state') if last else 'unknown'}")


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
        image = require_media_reference(image_reference, "image")
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

    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("file_path", "bytes", "sha256")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, model: str, text: str, voice: str, response_format: str = "wav", runtime_policy: str = "any", filename: str = "speech.wav"):
        content, headers = request_bytes(
            "/v1/audio/speech",
            {"model": model, "input": text, "voice": voice, "response_format": response_format, "runtime_policy": runtime_policy},
            method="POST",
            timeout_seconds=1800,
        )
        return write_download(content, headers, filename)


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
        content = decode_base64_payload(base64_data, field_name or "media")
        upload = request_json(
            "/v1/media/uploads",
            method="POST",
            data=content,
            headers={
                "Content-Type": mime_type,
                "X-B1-Field": field_name,
                "X-B1-Filename": filename,
            },
        )
        return (json_output(upload), json_output(upload))


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
        return (json_output(request_json(f"/v1/media/jobs/{urllib.parse.quote(job_id.strip())}", method="DELETE")),)


class B1ListJobArtifacts:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"job_id": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("artifacts_json",)
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, job_id: str):
        return (json_output(request_json(f"/v1/media/jobs/{urllib.parse.quote(job_id.strip())}/artifacts")),)


class B1DownloadArtifact:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "artifact_url": ("STRING", {"default": "/artifacts/"}),
                "filename": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("file_path", "bytes", "sha256")
    FUNCTION = "run"
    CATEGORY = "B1 AI Hub"

    def run(self, artifact_url: str, filename: str = ""):
        parsed = urllib.parse.urlsplit(artifact_url.strip())
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/artifacts/"):
            raise B1RemoteNodeError("artifact_url must be an internal /artifacts path")
        content, headers = request_bytes(parsed.path, method="GET", timeout_seconds=1800)
        preferred = filename.strip() or parsed.path.rsplit("/", 1)[-1]
        return write_download(content, headers, preferred)


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
