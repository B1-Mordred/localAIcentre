from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from . import media_artifacts


COMFYUI_OUTPUT_KEYS = {
    "images": "image",
    "gifs": "video",
    "videos": "video",
    "audio": "audio",
}
TEMPLATE_PATTERN = re.compile(
    r"{{\s*(?:(?:parameters|input)\.)?([A-Za-z0-9_.-]+)\s*}}|\$(?:parameters|input)\.([A-Za-z0-9_.-]+)"
)


def safe_artifact_segment(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in value)
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or fallback


def prefer_artifact_update(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_stored = current.get("source") == "artifact_store" or current.get("ingest_status") == "stored"
    candidate_stored = candidate.get("source") == "artifact_store" or candidate.get("ingest_status") == "stored"
    if candidate_stored and not current_stored:
        return candidate
    if candidate_stored and current.get("bytes") is None and candidate.get("bytes") is not None:
        return candidate
    if current.get("ingest_status") == "failed" and candidate.get("ingest_status") != "failed":
        return candidate
    return current


def merge_job_artifacts(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing)
    by_url = {artifact.get("url"): index for index, artifact in enumerate(merged) if isinstance(artifact, dict) and artifact.get("url")}
    for artifact in additions:
        url = artifact.get("url")
        if url and url in by_url:
            existing_index = by_url[url]
            existing_artifact = merged[existing_index]
            if isinstance(existing_artifact, dict):
                merged[existing_index] = prefer_artifact_update(existing_artifact, artifact)
            continue
        if url:
            merged.append(artifact)
            by_url[url] = len(merged) - 1
    return merged


def comfyui_history_record(prompt_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(payload.get(prompt_id), dict):
        return payload[prompt_id]
    if isinstance(payload.get("outputs"), dict):
        return payload
    return None


def comfyui_artifacts_from_outputs(prompt_id: str, outputs: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for node_id, output in outputs.items():
        if not isinstance(output, dict):
            continue
        for output_key, kind in COMFYUI_OUTPUT_KEYS.items():
            items = output.get(output_key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                index = len(artifacts)
                filename = item["filename"]
                subfolder = item.get("subfolder") if isinstance(item.get("subfolder"), str) else ""
                file_type = item.get("type") if isinstance(item.get("type"), str) else "output"
                prompt_segment = safe_artifact_segment(prompt_id, "prompt")
                file_segment = safe_artifact_segment(filename, f"{kind}-{index}")
                relative = f"comfyui/{prompt_segment}/{index}-{file_segment}"
                mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                # SaveVideo currently reports its MP4 through an ``images`` output
                # key. Preserve native compatibility, but classify artifacts by
                # their actual media type for the B1 API and Media Studio.
                if mime_type.startswith("video/"):
                    kind = "video"
                elif mime_type.startswith("audio/"):
                    kind = "audio"
                elif mime_type.startswith("image/"):
                    kind = "image"
                query = urlencode({"filename": filename, "subfolder": subfolder, "type": file_type})
                artifacts.append(
                    {
                        "id": f"artifact_{prompt_segment}_{index}",
                        "kind": kind,
                        "mime_type": mime_type,
                        "path": relative,
                        "url": f"/artifacts/{relative}",
                        "runtime": "comfyui",
                        "source": "comfyui_view",
                        "source_url": f"/view?{query}",
                        "bytes": None,
                        "sha256": None,
                        "comfyui": {
                            "prompt_id": prompt_id,
                            "node_id": str(node_id),
                            "output_key": output_key,
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": file_type,
                        },
                    }
                )
    return artifacts


def comfyui_artifacts_from_history(prompt_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    record = comfyui_history_record(prompt_id, payload)
    outputs = record.get("outputs") if record else None
    return comfyui_artifacts_from_outputs(prompt_id, outputs) if isinstance(outputs, dict) else []


def job_artifact_by_url(job: dict[str, Any], artifact_url: str) -> dict[str, Any] | None:
    for artifact in job.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("url") == artifact_url:
            return artifact
    return None


def comfyui_view_path_for_artifact(artifact: dict[str, Any]) -> str | None:
    if artifact.get("source") != "comfyui_view":
        return None
    comfyui = artifact.get("comfyui")
    if not isinstance(comfyui, dict):
        return None
    filename = comfyui.get("filename")
    if not isinstance(filename, str) or not filename:
        return None
    subfolder = comfyui.get("subfolder") if isinstance(comfyui.get("subfolder"), str) else ""
    file_type = comfyui.get("type") if isinstance(comfyui.get("type"), str) else "output"
    return f"/view?{urlencode({'filename': filename, 'subfolder': subfolder, 'type': file_type})}"


def artifact_store_path(artifact_root: Path, relative_path: str) -> Path:
    return media_artifacts.artifact_store_path(artifact_root, relative_path)


async def ingest_comfyui_artifact(
    artifact: dict[str, Any],
    artifact_root: Path,
    comfyui_url: str,
    timeout_seconds: float = 120.0,
    headers: dict[str, str] | None = None,
    client_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if artifact.get("source") != "comfyui_view":
        return artifact
    relative = artifact.get("path")
    if not isinstance(relative, str) or not relative:
        return {**artifact, "ingest_status": "failed", "ingest_error": "missing artifact path"}
    view_path = comfyui_view_path_for_artifact(artifact)
    if not view_path:
        return {**artifact, "ingest_status": "failed", "ingest_error": "missing ComfyUI view metadata"}

    try:
        target = artifact_store_path(artifact_root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = artifact_store_path(artifact_root, relative)
    except (OSError, ValueError) as exc:
        return {**artifact, "ingest_status": "failed", "ingest_error": exc.__class__.__name__}

    digest = hashlib.sha256()
    byte_count = 0
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    url = f"{comfyui_url.rstrip('/')}/{view_path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, **(client_kwargs or {})) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code >= 400:
                    return {**artifact, "ingest_status": "failed", "ingest_error": f"ComfyUI /view returned HTTP {response.status_code}"}
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
        target = artifact_store_path(artifact_root, relative)
        media_artifacts.ensure_regular_file_replace_target(target)
        temporary.replace(target)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        temporary.unlink(missing_ok=True)
        return {**artifact, "ingest_status": "failed", "ingest_error": exc.__class__.__name__}

    return {
        **artifact,
        "source": "artifact_store",
        "storage": "artifact-server",
        "source_url": artifact.get("source_url"),
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
        "ingest_status": "stored",
    }


async def ingest_comfyui_artifacts(
    artifacts: list[dict[str, Any]],
    artifact_root: Path,
    comfyui_url: str,
    timeout_seconds: float = 120.0,
    headers: dict[str, str] | None = None,
    client_kwargs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ingested: list[dict[str, Any]] = []
    for artifact in artifacts:
        ingested.append(
            await ingest_comfyui_artifact(
                artifact,
                artifact_root,
                comfyui_url,
                timeout_seconds,
                headers=headers,
                client_kwargs=client_kwargs,
            )
        )
    return ingested


async def fetch_comfyui_history(
    comfyui_url: str,
    prompt_id: str,
    timeout_seconds: float = 10.0,
    headers: dict[str, str] | None = None,
    client_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    url = f"{comfyui_url.rstrip('/')}/history/{prompt_id}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, **(client_kwargs or {})) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) and comfyui_history_record(prompt_id, payload) is not None else None


def _parameter_value(parameters: dict[str, Any], path: str) -> Any:
    current: Any = parameters
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def render_comfyui_workflow_template(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_comfyui_workflow_template(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [render_comfyui_workflow_template(item, parameters) for item in value]
    if not isinstance(value, str):
        return value

    full = TEMPLATE_PATTERN.fullmatch(value.strip())
    if full:
        return deepcopy(_parameter_value(parameters, full.group(1) or full.group(2)))

    def replace(match: re.Match[str]) -> str:
        return str(_parameter_value(parameters, match.group(1) or match.group(2)))

    return TEMPLATE_PATTERN.sub(replace, value)


def set_json_path(target: Any, path: list[str], value: Any) -> None:
    current = target
    for segment in path[:-1]:
        if isinstance(current, dict):
            if segment not in current or not isinstance(current[segment], (dict, list)):
                current[segment] = {}
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdecimal():
            index = int(segment)
            if index >= len(current):
                raise ValueError(f"mapping path index {segment} is outside the workflow array")
            current = current[index]
            continue
        raise ValueError(f"mapping path segment {segment} cannot be applied")

    final = path[-1]
    if isinstance(current, dict):
        current[final] = deepcopy(value)
        return
    if isinstance(current, list) and final.isdecimal():
        index = int(final)
        if index >= len(current):
            raise ValueError(f"mapping path index {final} is outside the workflow array")
        current[index] = deepcopy(value)
        return
    raise ValueError(f"mapping path segment {final} cannot be applied")


def apply_comfyui_parameter_mappings(
    workflow_json: dict[str, Any],
    parameters: dict[str, Any],
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    rendered = deepcopy(workflow_json)
    for mapping in mappings:
        parameter = mapping.get("parameter")
        path = mapping.get("path")
        if not isinstance(parameter, str) or not isinstance(path, list) or not all(isinstance(segment, str) for segment in path):
            raise ValueError("invalid ComfyUI parameter mapping")
        set_json_path(rendered, path, _parameter_value(parameters, parameter))
    return rendered


def workflow_comfyui_staged_upload_specs(workflow_manifest: dict[str, Any]) -> dict[str, str]:
    """Return staged input names and their expected media type for ComfyUI.

    Workflows opt in by using ``{{source_image_filename}}`` in ``LoadImage`` or
    ``{{source_video_filename}}`` in ``LoadVideo``.  Only server-returned input
    filenames are rendered into the native prompt; caller supplied paths never
    reach ComfyUI.
    """
    workflow_json = workflow_manifest.get("workflow_json")
    if not isinstance(workflow_json, dict):
        return {}
    input_schema = workflow_manifest.get("input_schema")
    schema_properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    declared_parameters = set(schema_properties) if isinstance(schema_properties, dict) else None
    conventional_parameters = {"source_image", "source_video"}
    parameters: dict[str, str] = {}
    for node in workflow_json.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type in {"LoadImage", "LoadImageMask"}:
            input_name, media_type = "image", "image"
        elif class_type == "LoadVideo":
            input_name, media_type = "file", "video"
        else:
            continue
        inputs = node.get("inputs")
        value = inputs.get(input_name) if isinstance(inputs, dict) else None
        if not isinstance(value, str):
            continue
        match = TEMPLATE_PATTERN.fullmatch(value.strip())
        parameter = (match.group(1) or match.group(2)) if match else None
        if parameter and parameter.endswith("_filename"):
            field_name = parameter.removesuffix("_filename")
            if declared_parameters is not None and field_name not in declared_parameters:
                continue
            if declared_parameters is None and field_name not in conventional_parameters:
                continue
            parameters[field_name] = media_type
    return parameters


def workflow_comfyui_staged_upload_parameters(workflow_manifest: dict[str, Any]) -> set[str]:
    """Compatibility wrapper returning workflow staged-input parameter names."""
    return set(workflow_comfyui_staged_upload_specs(workflow_manifest))


def normalize_comfyui_prompt_payload(
    prompt_or_payload: dict[str, Any],
    client_id: str,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(prompt_or_payload.get("prompt"), dict):
        payload = deepcopy(prompt_or_payload)
        payload.setdefault("client_id", client_id)
    else:
        payload = {"client_id": client_id, "prompt": deepcopy(prompt_or_payload)}

    if extra_data:
        current_extra = payload.get("extra_data")
        merged_extra = dict(current_extra) if isinstance(current_extra, dict) else {}
        b1_extra = merged_extra.get("b1")
        merged_extra["b1"] = {**(b1_extra if isinstance(b1_extra, dict) else {}), **extra_data}
        payload["extra_data"] = merged_extra
    return payload


def direct_comfyui_prompt_payload(input_payload: dict[str, Any], client_id: str, extra_data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    parameters = input_payload.get("parameters")
    for key in ("comfyui_payload", "comfyui_prompt", "native_prompt", "workflow_json"):
        candidate = input_payload.get(key)
        if isinstance(candidate, dict):
            if isinstance(parameters, dict) and parameters:
                candidate = render_comfyui_workflow_template(candidate, parameters)
            return normalize_comfyui_prompt_payload(candidate, client_id, extra_data)
    return None


def workflow_comfyui_prompt_payload(
    workflow_manifest: dict[str, Any],
    input_payload: dict[str, Any],
    client_id: str,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow_json = workflow_manifest.get("workflow_json")
    if not isinstance(workflow_json, dict) or not workflow_json:
        workflow_id = workflow_manifest.get("id", "unknown")
        workflow_version = workflow_manifest.get("version", "unknown")
        raise ValueError(f"published workflow {workflow_id}@{workflow_version} has no executable ComfyUI workflow_json")

    parameters = input_payload.get("parameters")
    rendered = deepcopy(workflow_json)
    if isinstance(parameters, dict) and parameters:
        mappings = workflow_manifest.get("comfyui_parameter_mappings")
        if isinstance(mappings, list) and mappings:
            rendered = apply_comfyui_parameter_mappings(rendered, parameters, mappings)
        rendered = render_comfyui_workflow_template(rendered, parameters)
    return normalize_comfyui_prompt_payload(rendered, client_id, extra_data)
