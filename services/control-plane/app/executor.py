from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import uuid
import wave
from contextlib import suppress
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from . import database
from . import comfyui_native
from . import media_artifacts
from . import model_lifecycle
from . import secret_store
from . import voice_profiles as voice_profile_policy
from .runtime_agent_http import runtime_agent_httpx_kwargs
from .scheduler import JobState, ResourcePolicy

import httpx


CPU_RUNTIMES = ["audio-cpu", "panel-cpu"]
GPU_RUNTIMES = ["localai", "lan-localai-worker", "lan-deepseek-worker", "lan-p40-media", "comfyui", "voicebox", "lipsync"]
LAN_MANAGED_GPU_RUNTIMES = {"lan-localai-worker", "lan-deepseek-worker", "lan-p40-media"}
MODEL_DOWNLOAD_RUNTIMES = ["model-download"]
CPU_TTS_OPERATIONS = {"speech", "text-to-speech", "tts"}
CPU_STT_OPERATIONS = {"transcription", "speech-to-text", "stt"}
VOICEBOX_TTS_OPERATIONS = {"speech", "text-to-speech", "tts"}
LOCALAI_IMAGE_GENERATION_OPERATIONS = {"generation", "image-generation", "text-to-image"}
LOCALAI_IMAGE_EDIT_OPERATIONS = {"edit", "image-edit", "image-to-image", "inpainting", "inpainting-outpainting"}
LOCALAI_VIDEO_GENERATION_OPERATIONS = {"generation", "video-generation", "text-to-video"}
LOCALAI_VIDEO_IMAGE_OPERATIONS = {"image-to-video", "video-image", "image-video"}
TALKING_HEAD_LIPSYNC_OPERATIONS = {"audio-driven-talking-head", "audio-to-lip", "lip-sync", "lipsync", "talking-head-lipsync"}
STUDIO_PANEL_SHOT_OPERATIONS = {"studio-panel-shot"}
STUDIO_SEATED_CHARACTER_OPERATIONS = {"studio-seated-character"}
SHA256_HEX_RE = re.compile(r"^[a-fA-F0-9]{64}$")
EMPTY_TIMING_SHA256 = hashlib.sha256(b"{}").hexdigest()
PUBLIC_LIPSYNC_RUNTIME_METADATA_KEYS = {"backend", "musetalk_commit", "batch_size", "use_float16"}
SEATED_GENERAL_MATTE_SHA256 = "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333"
SEATED_GENERAL_MATTE_MODEL = "birefnet-general-lite"

AudioCpuSpeechResult = tuple[bytes, str] | tuple[bytes, str, dict[str, Any]]

GPU_STATE_STEPS = [
    (JobState.UNLOADING, "unloading", 25),
    (JobState.VERIFYING_VRAM, "verifying_vram", 35),
    (JobState.LOADING, "loading", 45),
    (JobState.WARMING, "warming", 55),
    (JobState.RUNNING, "running", 70),
]
HOOK_OK_STATUSES = {"ok", "loaded", "ready", "healthy"}
HOOK_OPTIONAL_UNAVAILABLE_STATUSES = {"skipped", "unsupported", "unconfirmed"}
HOOK_FAILURE_STATUSES = {"failed", "failure", "error", "errored", "invalid", "rejected", "unconfigured", "unhealthy"}

PauseCheck = Callable[[], bool | Awaitable[bool]]


class RuntimePreparationError(RuntimeError):
    pass


class LipsyncInputRejectedError(ValueError):
    pass


class StudioPanelQualityError(ValueError):
    pass


class SeatedCharacterQualityError(ValueError):
    pass


class SeatedReferenceRequiredError(ValueError):
    pass


class CameraCoverageError(ValueError):
    pass


class SeatedPosePipelineUnavailableError(RuntimeError):
    pass


class LipsyncRuntimeFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def elapsed_milliseconds(start: float) -> int:
    return max(0, int((monotonic() - start) * 1000))


async def runner_paused(pause_check: PauseCheck | None) -> bool:
    if pause_check is None:
        return False
    result = pause_check()
    if isawaitable(result):
        result = await result
    return bool(result)


def unsupported_operation_message(job: dict[str, Any]) -> str:
    runtime = str(job.get("runtime") or "unknown")
    modality = str(job.get("modality") or "unknown")
    operation = str(job.get("operation") or "unknown")
    model = str(job.get("model_alias") or job.get("resolved_model_version") or "unknown")
    return f"Runtime {runtime} does not implement {modality}/{operation} jobs for {model}"


def normalized_operation(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def is_talking_head_lipsync_operation(value: Any) -> bool:
    return normalized_operation(value) in TALKING_HEAD_LIPSYNC_OPERATIONS


def gpu_runtime_for_job(job: dict[str, Any]) -> str:
    """Use the dedicated runtime for special media jobs without changing its public policy."""
    if str(job.get("runtime") or "") == "comfyui" and is_talking_head_lipsync_operation(job.get("operation")):
        return "lipsync"
    return str(job.get("runtime") or "")


def canonical_json_sha256(payload: dict[str, Any] | None) -> str:
    if not payload:
        return EMPTY_TIMING_SHA256
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def request_int(payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def wav_duration_ms(content: bytes) -> int:
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            frame_rate = wav.getframerate()
            frame_count = wav.getnframes()
    except wave.Error as exc:
        raise ValueError("audio_artifact_id must reference a readable WAV file") from exc
    if frame_rate <= 0:
        raise ValueError("WAV sample rate is invalid")
    return int(round(frame_count * 1000 / frame_rate))


def pending_startup_reconciliation(runtime_names: list[str]) -> dict[str, Any]:
    return {
        "status": "pending",
        "runtime_names": list(runtime_names),
        "started_at": "",
        "completed_at": "",
        "marked_recovery_required": 0,
        "requeued": 0,
    }


def compact_reconciliation_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value[:50] if isinstance(item, (str, int, float)) and str(item)]


async def record_runner_startup_reconciliation(runner: Any, runtime_names: list[str]) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC)
    runner.startup_reconciliation = {
        **pending_startup_reconciliation(runtime_names),
        "status": "running",
        "started_at": started_at.isoformat(),
    }
    try:
        result = await runner.reconcile_startup()
    except Exception as exc:
        runner.startup_reconciliation = {
            **runner.startup_reconciliation,
            "status": "failed",
            "completed_at": datetime.now(tz=UTC).isoformat(),
            "error": exc.__class__.__name__,
        }
        raise
    runner.startup_reconciliation = {
        "status": "ok",
        "runtime_names": list(runtime_names),
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(tz=UTC).isoformat(),
        "marked_recovery_required": int(result.get("marked_recovery_required") or 0),
        "requeued": int(result.get("requeued") or 0),
    }
    for key in ("paused", "cancelled"):
        if key in result:
            runner.startup_reconciliation[key] = int(result.get(key) or 0)
    for key in (
        "requeued_job_ids",
        "recovery_required_job_ids",
        "requeued_download_ids",
        "paused_download_ids",
        "cancelled_download_ids",
    ):
        if key in result:
            runner.startup_reconciliation[key] = compact_reconciliation_id_list(result.get(key))
    return runner.startup_reconciliation


class CpuJobRunner:
    def __init__(
        self,
        artifact_root: Path,
        interval_seconds: int = 1,
        audio_cpu_url: str = "",
        pause_check: PauseCheck | None = None,
        runtime_cancel_poll_seconds: float = 1.0,
        resource_policy_provider: Callable[[], ResourcePolicy] | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.interval_seconds = max(1, interval_seconds)
        self.audio_cpu_url = audio_cpu_url.rstrip("/")
        self.pause_check = pause_check
        self.runtime_cancel_poll_seconds = max(0.05, float(runtime_cancel_poll_seconds))
        self.resource_policy_provider = resource_policy_provider or ResourcePolicy
        self._stopped = asyncio.Event()
        self.startup_reconciliation = pending_startup_reconciliation(CPU_RUNTIMES)

    async def reconcile_startup(self) -> dict[str, Any]:
        requeued = await database.requeue_interrupted_waiting_jobs_report(CPU_RUNTIMES)
        marked = await database.mark_interrupted_jobs_recovery_required_report(CPU_RUNTIMES)
        return {**marked, **requeued}

    async def cancel_if_requested(self, job_id: str) -> bool:
        current = await database.get_job(job_id)
        if current and current["state"] in {JobState.CANCELLING.value, JobState.CANCELLED.value}:
            await database.update_job(job_id, state=JobState.CANCELLED.value, stage="cancelled", progress=100)
            return True
        return False

    async def await_cancellable_runtime_call(self, job: dict[str, Any], call: Awaitable[Any]) -> tuple[bool, Any]:
        task = asyncio.create_task(call)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.runtime_cancel_poll_seconds)
                if task in done:
                    return False, await task
                if await self.cancel_if_requested(str(job["id"])):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                    return True, None
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        except Exception:
            if not task.done():
                task.cancel()
            raise

    def request_input(self, job: dict[str, Any]) -> dict[str, Any]:
        request_params = job.get("request_params")
        if not isinstance(request_params, dict):
            return {}
        input_payload = request_params.get("input")
        return input_payload if isinstance(input_payload, dict) else {}

    def resolved_model_id(self, job: dict[str, Any]) -> str:
        resolved = str(job.get("resolved_model_version") or "")
        if "@" in resolved:
            return resolved.split("@", 1)[0]
        return str(job.get("model_alias") or resolved)

    def cpu_residency_allowed(self, job: dict[str, Any]) -> bool:
        alias = str(job.get("model_alias") or "").strip()
        if not alias:
            return False
        policy = self.resource_policy_provider()
        return bool(policy.cpu_residency_enabled and alias in set(policy.cpu_resident_aliases))

    def audio_payload_for_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(self.request_input(job))
        parameters = payload.pop("parameters", None)
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                payload.setdefault(key, value)
        for key in {"workflow_id", "workflow_version", "runtime_policy"}:
            payload.pop(key, None)
        payload["model"] = self.resolved_model_id(job)
        if job.get("resolved_model_version"):
            payload["b1_resolved_model_version"] = str(job["resolved_model_version"])
        if job.get("model_alias"):
            payload["b1_model_alias"] = str(job["model_alias"])
            payload["b1_cpu_residency_allowed"] = self.cpu_residency_allowed(job)
        self.expand_staged_audio_inputs(payload)
        return payload

    def expand_staged_audio_inputs(self, payload: dict[str, Any]) -> None:
        for key in ("audio", "file"):
            value = payload.get(key)
            if not isinstance(value, dict) or value.get("source") != "staged_upload":
                continue
            content, mime_type, filename = media_artifacts.read_staged_input_bytes(self.artifact_root, value)
            if not mime_type.startswith("audio/"):
                raise ValueError("staged transcription input must be audio")
            payload["audio"] = base64.b64encode(content).decode("ascii")
            payload.setdefault("audio_mime_type", mime_type)
            payload.setdefault("filename", filename)
            if key != "audio":
                payload.pop(key, None)
            return

    async def post_audio_cpu_speech(self, payload: dict[str, Any]) -> AudioCpuSpeechResult:
        if not self.audio_cpu_url:
            raise RuntimeError("audio-cpu runtime URL is not configured")
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(f"{self.audio_cpu_url}/v1/audio/speech", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"audio-cpu speech returned HTTP {response.status_code}")
        metadata: dict[str, Any] = {}
        placeholder_header = response.headers.get("x-b1-placeholder")
        if placeholder_header is not None:
            metadata["b1_placeholder"] = placeholder_header.strip().lower() == "true"
        cpu_audio_engine = response.headers.get("x-b1-cpu-audio-engine", "").strip()
        if cpu_audio_engine:
            metadata["b1_cpu_audio_engine"] = cpu_audio_engine
        return response.content, response.headers.get("content-type", "audio/wav").split(";")[0], {
            key: value for key, value in metadata.items() if value != ""
        }

    def unpack_audio_cpu_speech_result(self, result: AudioCpuSpeechResult) -> tuple[bytes, str, dict[str, Any]]:
        content = result[0]
        mime_type = result[1]
        metadata = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}
        return content, mime_type, metadata

    def audio_cpu_transcription_metadata(self, body: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if isinstance(body.get("b1_placeholder"), bool):
            metadata["b1_placeholder"] = body["b1_placeholder"]
        for key in ("b1_engine", "b1_stt_engine"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                metadata[key] = value.strip()
        return metadata

    async def post_audio_cpu_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.audio_cpu_url:
            raise RuntimeError("audio-cpu runtime URL is not configured")
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(f"{self.audio_cpu_url}/v1/audio/transcriptions", json=payload, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"audio-cpu transcription returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("audio-cpu transcription returned non-JSON response") from exc
        if not isinstance(body, dict):
            raise RuntimeError("audio-cpu transcription returned invalid JSON")
        return body

    async def run_audio_cpu_job(self, job: dict[str, Any]) -> bool:
        modality = str(job.get("modality") or "")
        operation = str(job.get("operation") or "")
        if modality == "tts" and operation in CPU_TTS_OPERATIONS:
            await database.update_job(job["id"], state=JobState.RUNNING.value, stage="audio_cpu_speech", progress=60)
            if await self.cancel_if_requested(job["id"]):
                return True
            cancelled, result = await self.await_cancellable_runtime_call(job, self.post_audio_cpu_speech(self.audio_payload_for_job(job)))
            if cancelled:
                return True
            content, mime_type, runtime_metadata = self.unpack_audio_cpu_speech_result(result)
            if not content:
                await database.update_job(
                    job["id"],
                    state=JobState.RECOVERY_REQUIRED.value,
                    stage="audio_cpu_empty_speech",
                    progress=90,
                    failure_category="audio_cpu_empty_speech",
                    failure_message="audio-cpu returned an empty speech response",
                )
                return True
            artifact = media_artifacts.write_artifact_bytes(
                self.artifact_root,
                namespace="audio-cpu",
                job_id=str(job["id"]),
                index=0,
                content=content,
                mime_type=mime_type,
                source="audio_cpu_runtime",
                metadata={
                    "runtime": "audio-cpu",
                    "model": self.resolved_model_id(job),
                    "operation": operation,
                    **runtime_metadata,
                },
            )
            await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
            await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
            return True
        if modality == "stt" and operation in CPU_STT_OPERATIONS:
            await database.update_job(job["id"], state=JobState.RUNNING.value, stage="audio_cpu_transcription", progress=60)
            if await self.cancel_if_requested(job["id"]):
                return True
            cancelled, body = await self.await_cancellable_runtime_call(job, self.post_audio_cpu_transcription(self.audio_payload_for_job(job)))
            if cancelled:
                return True
            if not isinstance(body.get("text"), str):
                await database.update_job(
                    job["id"],
                    state=JobState.RECOVERY_REQUIRED.value,
                    stage="audio_cpu_transcription_missing_text",
                    progress=90,
                    failure_category="audio_cpu_transcription_missing_text",
                    failure_message="audio-cpu transcription response did not include text",
                )
                return True
            content = (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")
            artifact = media_artifacts.write_artifact_bytes(
                self.artifact_root,
                namespace="audio-cpu",
                job_id=str(job["id"]),
                index=0,
                content=content,
                mime_type="application/json",
                source="audio_cpu_runtime",
                metadata={
                    "runtime": "audio-cpu",
                    "model": self.resolved_model_id(job),
                    "operation": operation,
                    "text": body["text"],
                    **self.audio_cpu_transcription_metadata(body),
                },
            )
            await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
            await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
            return True
        return False

    async def run_panel_cpu_job(self, job: dict[str, Any]) -> bool:
        """Run the deterministic panel compositor without a GPU lease.

        The implementation is shared with the managed media runner for now,
        but invoking this single method performs only Pillow/ffmpeg CPU work:
        it does not run the GPU lifecycle, acquire the global lease, or call a
        ComfyUI endpoint.
        """
        if (
            str(job.get("runtime") or "") != "panel-cpu"
            or str(job.get("modality") or "") != "image"
            or normalized_operation(job.get("operation")) not in STUDIO_PANEL_SHOT_OPERATIONS
        ):
            return False
        compositor = GpuJobRunner(self.artifact_root)
        await compositor.run_studio_panel_shot_job(job)
        return True

    async def run_once(self) -> bool:
        if await runner_paused(self.pause_check):
            return False
        job = await database.claim_next_job(CPU_RUNTIMES)
        if job is None:
            return False
        run_started = monotonic()
        await database.update_job(job["id"], started_at=datetime.now(tz=UTC))
        try:
            if await self.run_panel_cpu_job(job):
                await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            if await self.run_audio_cpu_job(job):
                await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            current = await database.get_job(job["id"])
            if current and current["state"] in {"cancelling", "cancelled"}:
                await database.update_job(job["id"], state=JobState.CANCELLED.value, stage="cancelled", progress=100)
                await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            await database.update_job(
                job["id"],
                state=JobState.FAILED.value,
                stage="unsupported_audio_cpu_operation",
                progress=100,
                run_time_ms=elapsed_milliseconds(run_started),
                failure_category="unsupported_audio_cpu_operation",
                failure_message=unsupported_operation_message(job),
                artifacts=[],
            )
        except (StudioPanelQualityError, SeatedReferenceRequiredError) as exc:
            category = "studio_panel_qc_failed" if isinstance(exc, StudioPanelQualityError) else "seated_reference_required"
            await database.update_job(
                job["id"],
                state=JobState.FAILED.value,
                stage=category,
                progress=100,
                run_time_ms=elapsed_milliseconds(run_started),
                failure_category=category,
                failure_message=str(exc)[:500],
            )
        except Exception as exc:
            await self.record_runtime_failure(job, exc)
            await database.update_job(
                job["id"],
                state=JobState.FAILED.value,
                stage="failed",
                progress=100,
                run_time_ms=elapsed_milliseconds(run_started),
                failure_category="cpu_runner_error",
                failure_message=exc.__class__.__name__,
            )
        return True

    async def run_forever(self) -> None:
        await record_runner_startup_reconciliation(self, CPU_RUNTIMES)
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped.set()


class GpuJobRunner:
    def __init__(
        self,
        artifact_root: Path,
        interval_seconds: int = 1,
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 300,
        runtime_agent_url: str = "",
        runtime_agent_token: str = "",
        runtime_agent_tls_ca_file: str = "",
        runtime_agent_tls_client_cert_file: str = "",
        runtime_agent_tls_client_key_file: str = "",
        runtime_agent_tls_verify: bool = True,
        runtime_control_token: str = "",
        reserve_vram_gib: float = 1.5,
        recovery_timeout_seconds: int = 10,
        default_idle_timeout_seconds: int = 300,
        runtime_urls: dict[str, str] | None = None,
        runtime_tls_ca_files: dict[str, str] | None = None,
        comfyui_poll_seconds: int = 2,
        comfyui_completion_timeout_seconds: int = 7200,
        pause_check: PauseCheck | None = None,
        runtime_cancel_poll_seconds: float = 1.0,
        talking_head_lipsync_fallback_renderer: bool = False,
    ) -> None:
        self.artifact_root = artifact_root
        self.interval_seconds = max(1, interval_seconds)
        self.lease_owner = lease_owner or f"control-plane-gpu-runner-{uuid.uuid4().hex}"
        self.lease_ttl_seconds = max(30, lease_ttl_seconds)
        self.runtime_agent_url = runtime_agent_url.rstrip("/")
        self.runtime_agent_token = runtime_agent_token
        self.runtime_agent_tls_ca_file = runtime_agent_tls_ca_file
        self.runtime_agent_tls_client_cert_file = runtime_agent_tls_client_cert_file
        self.runtime_agent_tls_client_key_file = runtime_agent_tls_client_key_file
        self.runtime_agent_tls_verify = runtime_agent_tls_verify
        self.runtime_control_token = runtime_control_token.strip()
        self.reserve_vram_mib = int(max(0.0, reserve_vram_gib) * 1024)
        self.recovery_timeout_seconds = max(1, recovery_timeout_seconds)
        self.default_idle_timeout_seconds = max(0, int(default_idle_timeout_seconds))
        self.runtime_urls = {key: value.rstrip("/") for key, value in (runtime_urls or {}).items() if value}
        self.runtime_tls_ca_files = {key: value for key, value in (runtime_tls_ca_files or {}).items() if value}
        self.comfyui_poll_seconds = max(1, comfyui_poll_seconds)
        self.comfyui_completion_timeout_seconds = max(30, comfyui_completion_timeout_seconds)
        self.pause_check = pause_check
        self.runtime_cancel_poll_seconds = max(0.05, float(runtime_cancel_poll_seconds))
        self.talking_head_lipsync_fallback_renderer = bool(talking_head_lipsync_fallback_renderer)
        self._seated_general_matte_session: Any | None = None
        self._stopped = asyncio.Event()
        self.startup_reconciliation = pending_startup_reconciliation(GPU_RUNTIMES)

    async def reconcile_startup(self) -> dict[str, Any]:
        requeued = await database.requeue_interrupted_waiting_jobs_report(GPU_RUNTIMES)
        marked = await database.mark_interrupted_jobs_recovery_required_report(GPU_RUNTIMES)
        return {**marked, **requeued}

    async def acquire_gpu_lease(self) -> bool:
        lease = await database.acquire_scheduler_owner(self.lease_owner, self.lease_ttl_seconds)
        return bool(lease.get("acquired"))

    async def interactive_waiter_pending(self) -> bool:
        checker = getattr(database, "has_interactive_gpu_waiter", None)
        return bool(await checker()) if checker is not None else False

    async def release_gpu_lease(self) -> None:
        release = getattr(database, "release_scheduler_owner", None)
        if release is not None:
            await release(self.lease_owner)

    async def cancel_if_requested(self, job_id: str) -> bool:
        current = await database.get_job(job_id)
        if current and current["state"] in {JobState.CANCELLING.value, JobState.CANCELLED.value}:
            await database.update_job(job_id, state=JobState.CANCELLED.value, stage="cancelled", progress=100)
            return True
        return False

    async def recover_cancelled_runtime_execution(self, job: dict[str, Any]) -> None:
        runtime = gpu_runtime_for_job(job)
        if runtime not in GPU_RUNTIMES:
            return
        await self.record_runtime_state_for_job(
            runtime,
            "cancel_requested",
            "cancelling",
            job,
            {"reason": "job cancellation requested during runtime execution"},
        )
        if runtime in {"comfyui", "lan-p40-media"}:
            await self.interrupt_comfyui(job)
        recovery_payload = {
            **self.runtime_control_payload(job),
            "reason": f"cancelled job {job['id']}; recover {runtime} before releasing the GPU lease",
            "timeout_seconds": self.recovery_timeout_seconds,
        }
        if runtime in LAN_MANAGED_GPU_RUNTIMES:
            await self.post_runtime_control(runtime, "cancel", recovery_payload)
            result = await self.post_runtime_control(runtime, "recover", recovery_payload)
        elif self.runtime_agent_url:
            result = await self.runtime_agent_post(f"/v1/runtime-actions/{runtime}/recover", recovery_payload)
        else:
            return
        await self.record_runtime_state_for_job(
            runtime,
            self.runtime_hook_state_status("recover", result),
            "cancel_recovery",
            job,
            {"hook": self.compact_hook_result(result)},
        )

    async def await_cancellable_runtime_call(self, job: dict[str, Any], call: Awaitable[Any]) -> tuple[bool, Any]:
        task = asyncio.create_task(call)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.runtime_cancel_poll_seconds)
                if task in done:
                    return False, await task
                if await self.cancel_if_requested(str(job["id"])):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                    await self.recover_cancelled_runtime_execution(job)
                    return True, None
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        except Exception:
            if not task.done():
                task.cancel()
            raise

    def runtime_agent_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.runtime_agent_token}"} if self.runtime_agent_token else {}

    def runtime_agent_client_kwargs(self) -> dict[str, Any]:
        return runtime_agent_httpx_kwargs(
            self.runtime_agent_url,
            ca_file=self.runtime_agent_tls_ca_file,
            client_cert_file=self.runtime_agent_tls_client_cert_file,
            client_key_file=self.runtime_agent_tls_client_key_file,
            verify_tls=self.runtime_agent_tls_verify,
        )

    async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
        if not self.runtime_agent_url:
            return None
        url = f"{self.runtime_agent_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=5.0, **self.runtime_agent_client_kwargs()) as client:
                response = await client.get(url, headers=self.runtime_agent_headers())
            if response.status_code >= 400:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, OSError, ValueError):
            return None

    async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.runtime_agent_url:
            return None
        url = f"{self.runtime_agent_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                timeout=max(30.0, float(self.recovery_timeout_seconds + 5)),
                **self.runtime_agent_client_kwargs(),
            ) as client:
                response = await client.post(url, headers=self.runtime_agent_headers(), json=payload)
            if response.status_code >= 400:
                return None
            body = response.json()
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, OSError, ValueError):
            return None

    def runtime_httpx_kwargs(self, runtime: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"trust_env": False}
        ca_file = self.runtime_tls_ca_files.get(runtime)
        if ca_file:
            kwargs["verify"] = ca_file
        return kwargs

    async def remote_runtime_get(self, runtime: str, path: str) -> dict[str, Any] | None:
        runtime_url = self.runtime_urls.get(runtime)
        if not runtime_url:
            return None
        headers = {"Accept": "application/json"}
        if self.runtime_control_token:
            headers["Authorization"] = f"Bearer {self.runtime_control_token}"
        try:
            async with httpx.AsyncClient(timeout=10.0, **self.runtime_httpx_kwargs(runtime)) as client:
                response = await client.get(f"{runtime_url}/{path.lstrip('/')}", headers=headers)
            if response.status_code >= 400:
                return None
            body = response.json()
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, OSError, ValueError):
            return None

    async def runtime_metrics(self, runtime: str | None = None) -> dict[str, Any] | None:
        if runtime in LAN_MANAGED_GPU_RUNTIMES:
            return await self.remote_runtime_get(runtime, "/b1/runtime/metrics")
        return await self.runtime_agent_get("/v1/metrics")

    async def unload_vram_release_check(self, runtime: str | None = None) -> tuple[bool, dict[str, Any]]:
        if runtime not in LAN_MANAGED_GPU_RUNTIMES and not self.runtime_agent_url:
            return True, {"status": "skipped", "reason": "runtime_agent_missing", "reserve_mib": self.reserve_vram_mib}
        metrics = await self.runtime_metrics(runtime)
        used = self.gpu_memory_used_mib(metrics)
        if used is None:
            return False, {"status": "unconfirmed", "reason": "gpu_metrics_unavailable", "reserve_mib": self.reserve_vram_mib}
        observed = max(used)
        if observed <= self.reserve_vram_mib:
            return True, {"status": "ok", "memory_used_mib": observed, "reserve_mib": self.reserve_vram_mib}
        return False, {"status": "failed", "reason": "vram_above_reserve_after_unload", "memory_used_mib": observed, "reserve_mib": self.reserve_vram_mib}

    async def current_runtime_state_by_name(self) -> dict[str, dict[str, Any]]:
        list_states = getattr(database, "list_runtime_states", None)
        if list_states is None:
            return {}
        try:
            rows = await list_states()
        except Exception:
            return {}
        return {str(row.get("runtime") or ""): dict(row) for row in rows if isinstance(row, dict) and row.get("runtime")}

    def runtime_unload_payload(self, runtime: str, job: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or {}
        return {
            "job_id": str(job["id"]),
            "runtime": runtime,
            "model": str(state.get("active_model") or state.get("resolved_model_version") or ""),
            "model_alias": str(state.get("model_alias") or ""),
            "resolved_model_version": str(state.get("resolved_model_version") or ""),
            "modality": str(state.get("modality") or ""),
            "operation": "unload",
        }

    async def graceful_or_forced_unload_runtime(
        self,
        runtime: str,
        target_runtime: str,
        job: dict[str, Any],
        state: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        graceful: dict[str, Any] | None
        try:
            graceful = await self.post_runtime_control(runtime, "unload", self.runtime_unload_payload(runtime, job, state))
        except Exception as exc:
            graceful = {"status": "failed", "runtime": runtime, "action": "unload", "reason": "runtime_hook_failed", "error": exc.__class__.__name__}
        details: dict[str, Any] = {
            "target_runtime": target_runtime,
            "graceful_hook": self.compact_hook_result(graceful),
        }
        if self.runtime_hook_status(graceful) == "ok":
            released, verification = await self.unload_vram_release_check(runtime)
            details["vram_verification"] = verification
            if released:
                details["hook"] = self.compact_hook_result(graceful)
                return graceful, details
            graceful = {
                "status": "failed",
                "runtime": runtime,
                "action": "unload",
                "reason": str(verification.get("reason") or "vram_unverified_after_unload"),
                "memory_used_mib": verification.get("memory_used_mib"),
                "reserve_mib": verification.get("reserve_mib"),
            }
            details["graceful_vram_failure"] = self.compact_hook_result(graceful)
        if runtime in LAN_MANAGED_GPU_RUNTIMES:
            forced = await self.post_runtime_control(runtime, "recover", self.runtime_unload_payload(runtime, job, state))
            details["recovery_hook"] = self.compact_hook_result(forced)
        elif not self.runtime_agent_url:
            details["hook"] = self.compact_hook_result(graceful)
            return graceful, details
        else:
            fallback_reason = reason or f"prepare {target_runtime} for job {job['id']}: unload other GPU runtime {runtime}"
            forced = await self.runtime_agent_post(
                f"/v1/runtime-actions/{runtime}/unload",
                {
                    "reason": fallback_reason,
                    "timeout_seconds": self.recovery_timeout_seconds,
                },
            )
            details["restart_fallback"] = self.compact_hook_result(forced)
        released, verification = await self.unload_vram_release_check(runtime)
        details["restart_vram_verification"] = verification
        if not released:
            forced = {
                "status": "failed",
                "runtime": runtime,
                "action": "unload",
                "strategy": str((forced or {}).get("strategy") or "restart_service"),
                "reason": str(verification.get("reason") or "vram_unverified_after_unload"),
                "memory_used_mib": verification.get("memory_used_mib"),
                "reserve_mib": verification.get("reserve_mib"),
            }
        details["hook"] = self.compact_hook_result(forced)
        return forced, details

    async def unload_other_gpu_runtimes(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
        target_runtime = gpu_runtime_for_job(job)
        if target_runtime not in GPU_RUNTIMES:
            return []
        results: list[dict[str, Any] | None] = []
        states = await self.current_runtime_state_by_name()
        for runtime in self.configured_gpu_runtimes():
            if runtime == target_runtime:
                continue
            state = states.get(runtime)
            # A persisted state with no active model is an explicit unload
            # record. Reissuing that runtime's unload hook after the target
            # model has become resident can make the inactive runtime mistake
            # the target's VRAM for its own and reject every warm request.
            # Missing state remains fail-closed and is still probed/unloaded.
            if state is not None and not self.runtime_state_has_active_model(state):
                continue
            result, details = await self.graceful_or_forced_unload_runtime(runtime, target_runtime, job, state)
            await self.record_runtime_state_for_job(
                runtime,
                self.runtime_hook_state_status("unload", result),
                "unloading",
                job,
                details,
                record_model=False,
            )
            results.append(result)
        return results

    def runtime_state_has_active_model(self, state: dict[str, Any] | None) -> bool:
        if not isinstance(state, dict):
            return False
        return bool(str(state.get("active_model") or "").strip() or str(state.get("resolved_model_version") or "").strip())

    def runtime_state_matches_job_model(self, state: dict[str, Any] | None, job: dict[str, Any]) -> bool:
        if not self.runtime_state_has_active_model(state):
            return True
        assert state is not None
        target_refs = {
            value
            for value in (
                str(job.get("resolved_model_version") or "").strip(),
                self.resolved_model_id(job).strip(),
            )
            if value
        }
        if not target_refs:
            alias = str(job.get("model_alias") or "").strip()
            return bool(alias and alias == str(state.get("model_alias") or "").strip())
        state_refs = {
            value
            for value in (
                str(state.get("active_model") or "").strip(),
                str(state.get("resolved_model_version") or "").strip(),
            )
            if value
        }
        return bool(target_refs & state_refs)

    async def unload_target_runtime_for_model_switch(self, job: dict[str, Any]) -> dict[str, Any] | None:
        target_runtime = gpu_runtime_for_job(job)
        if target_runtime not in GPU_RUNTIMES:
            return None
        state = (await self.current_runtime_state_by_name()).get(target_runtime) or {}
        if self.runtime_state_matches_job_model(state, job):
            return None
        previous_model = str(state.get("resolved_model_version") or state.get("active_model") or "").strip()
        target_model = str(job.get("resolved_model_version") or self.resolved_model_id(job)).strip()
        result, details = await self.graceful_or_forced_unload_runtime(
            target_runtime,
            target_runtime,
            job,
            state,
            reason=f"prepare {target_runtime} for job {job['id']}: switch from {previous_model} to {target_model}",
        )
        await self.record_runtime_state_for_job(
            target_runtime,
            self.runtime_hook_state_status("unload", result),
            "unloading",
            job,
            {
                **details,
                "same_runtime_model_switch": True,
                "previous_model": previous_model,
                "target_model": target_model,
            },
            record_model=False,
        )
        return result

    async def unload_gpu_runtimes_for_job(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
        results = await self.unload_other_gpu_runtimes(job)
        same_runtime_result = await self.unload_target_runtime_for_model_switch(job)
        if same_runtime_result is not None:
            results.append(same_runtime_result)
        return results

    def runtime_control_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        runtime = gpu_runtime_for_job(job)
        payload = {
            "job_id": str(job["id"]),
            "runtime": runtime,
            "model": self.resolved_model_id(job),
            "model_alias": str(job.get("model_alias") or ""),
            "resolved_model_version": str(job.get("resolved_model_version") or ""),
            "modality": str(job.get("modality") or ""),
            "operation": str(job.get("operation") or ""),
        }
        request_params = job.get("request_params")
        runtime_smoke = request_params.get("runtime_smoke") if isinstance(request_params, dict) else None
        if isinstance(runtime_smoke, dict):
            payload["runtime_smoke"] = runtime_smoke
            runtime_config = runtime_smoke.get(runtime)
            if isinstance(runtime_config, dict):
                payload["runtime_smoke_config"] = runtime_config
        return payload

    def configured_gpu_runtimes(self) -> list[str]:
        # Lipsync is an internal optional runtime; do not issue control calls
        # to it in installations where the service is deliberately absent.
        return [
            runtime
            for runtime in GPU_RUNTIMES
            if runtime not in {"lipsync", *LAN_MANAGED_GPU_RUNTIMES} or runtime in self.runtime_urls
        ]

    def compact_hook_result(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"status": "unconfirmed"}
        allowed_keys = {
            "status",
            "reason",
            "action",
            "strategy",
            "runtime_action",
            "service",
            "runtime",
            "message",
            "error",
            "code",
            "memory_used_mib",
            "reserve_mib",
            "baseline_mib",
            "margin_mib",
            "idle_threshold_mib",
            "stable_sample_count",
            "stable_sample_target",
            "memory_samples_mib",
        }
        return {key: value for key, value in result.items() if key in allowed_keys}

    def runtime_hook_state_status(self, action: str, result: dict[str, Any] | None) -> str:
        status = str((result or {}).get("status") or "unconfirmed").strip().lower()
        if action == "load" and status in {"ok", "loaded"}:
            return "loaded"
        if action == "warm" and status in {"ok", "ready"}:
            return "ready"
        return f"{action}_{status}"

    def runtime_hook_status(self, result: dict[str, Any] | None) -> str:
        if not isinstance(result, dict):
            return "unconfirmed"
        return str(result.get("status") or "unconfirmed").strip().lower()

    def assert_runtime_prepared(self, runtime: str, action: str, result: dict[str, Any] | None) -> None:
        status = self.runtime_hook_status(result)
        if status in HOOK_OK_STATUSES or status in HOOK_OPTIONAL_UNAVAILABLE_STATUSES:
            return
        if status in HOOK_FAILURE_STATUSES or status not in HOOK_OK_STATUSES:
            reason = ""
            if isinstance(result, dict):
                for key in ("reason", "message", "error", "code"):
                    value = result.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        reason = f": {value}"
                        break
            raise RuntimePreparationError(f"{runtime} runtime {action} hook reported {status}{reason}")

    async def upsert_runtime_state(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        upsert = getattr(database, "upsert_runtime_state", None)
        if upsert is None:
            return None
        return await upsert(payload)

    async def record_runtime_state_for_job(
        self,
        runtime: str,
        status: str,
        stage: str,
        job: dict[str, Any],
        details: dict[str, Any] | None = None,
        record_model: bool = True,
    ) -> dict[str, Any] | None:
        return await self.upsert_runtime_state(
            {
                "runtime": runtime,
                "status": status,
                "stage": stage,
                "active_model": self.resolved_model_id(job) if record_model else None,
                "model_alias": str(job.get("model_alias") or "") if record_model else None,
                "resolved_model_version": str(job.get("resolved_model_version") or "") if record_model else None,
                "job_id": str(job["id"]),
                "details": details or {},
            }
        )

    async def record_runtime_idle_for_job(self, job: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any] | None:
        runtime = gpu_runtime_for_job(job)
        ephemeral = runtime == "lipsync"
        return await self.upsert_runtime_state(
            {
                "runtime": runtime,
                "status": "idle",
                "stage": "idle",
                "active_model": None if ephemeral else self.resolved_model_id(job),
                "model_alias": None if ephemeral else str(job.get("model_alias") or ""),
                "resolved_model_version": None if ephemeral else str(job.get("resolved_model_version") or ""),
                "job_id": str(job.get("id") or ""),
                "details": details or {},
            }
        )

    def has_durable_job_record(self, job: dict[str, Any]) -> bool:
        return bool(job.get("durable_job", True))

    def state_updated_at(self, state: dict[str, Any]) -> datetime | None:
        value = state.get("updated_at")
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return None

    async def idle_timeout_seconds_for_alias(self, alias: str) -> int:
        getter = getattr(database, "get_model_alias_policy", None)
        if getter is not None and alias:
            row = await getter(alias)
            if isinstance(row, dict):
                value = row.get("idle_timeout_seconds")
                if isinstance(value, int) and value >= 30:
                    return value
        return self.default_idle_timeout_seconds

    async def expired_idle_runtime_state(self, now: datetime | None = None) -> tuple[dict[str, Any], int, int] | None:
        list_states = getattr(database, "list_runtime_states", None)
        if list_states is None:
            return None
        now = now or datetime.now(tz=UTC)
        for state in await list_states():
            runtime = str(state.get("runtime") or "")
            if runtime not in GPU_RUNTIMES:
                continue
            if not state.get("active_model") and not state.get("resolved_model_version"):
                continue
            updated_at = self.state_updated_at(state)
            if updated_at is None:
                continue
            timeout_seconds = await self.idle_timeout_seconds_for_alias(str(state.get("model_alias") or ""))
            if timeout_seconds <= 0:
                continue
            idle_seconds = int((now - updated_at).total_seconds())
            if idle_seconds >= timeout_seconds:
                return state, idle_seconds, timeout_seconds
        return None

    async def unload_expired_idle_runtime(self) -> bool:
        if not self.runtime_agent_url and not self.runtime_urls:
            return False
        candidate = await self.expired_idle_runtime_state()
        if candidate is None:
            return False
        if not await self.acquire_gpu_lease():
            return False
        result: dict[str, Any] | None = None
        try:
            # The lease may have been held by an interactive request while the
            # candidate above was selected. That request can reload the same
            # runtime and refresh its idle timestamp before this runner gets
            # the lease. Re-read under the lease so a stale candidate cannot
            # unload the freshly loaded model.
            candidate = await self.expired_idle_runtime_state()
            if candidate is None:
                return False
            state, idle_seconds, timeout_seconds = candidate
            runtime = str(state.get("runtime") or "")
            reason = (
                f"idle timeout expired for {runtime} model {state.get('active_model') or state.get('resolved_model_version')} "
                f"after {idle_seconds}s >= {timeout_seconds}s"
            )
            try:
                result, details = await self.graceful_or_forced_unload_runtime(
                    runtime,
                    runtime,
                    {"id": state.get("job_id") or f"idle-{runtime}", "runtime": runtime},
                    state,
                    reason=reason,
                )
            except Exception as exc:
                # A failed idle hook must not terminate the persistent GPU job
                # runner. Preserve the loaded-state claim and refresh its
                # timestamp so the next retry is bounded by the idle timeout.
                with suppress(Exception):
                    await self.upsert_runtime_state(
                        {
                            "runtime": runtime,
                            "status": "idle_unload_failed",
                            "stage": "idle_unload_failed",
                            "active_model": state.get("active_model"),
                            "model_alias": state.get("model_alias"),
                            "resolved_model_version": state.get("resolved_model_version"),
                            "job_id": state.get("job_id"),
                            "details": {
                                "idle_seconds": idle_seconds,
                                "timeout_seconds": timeout_seconds,
                                "error": (str(exc) or exc.__class__.__name__)[:500],
                            },
                        }
                    )
                return False
            confirmed = str((result or {}).get("status") or "") == "ok"
            await self.upsert_runtime_state(
                {
                    "runtime": runtime,
                    "status": self.runtime_hook_state_status("unload", result),
                    "stage": "idle_unloaded" if confirmed else "idle_unload_unconfirmed",
                    "active_model": None if confirmed else state.get("active_model"),
                    "model_alias": None if confirmed else state.get("model_alias"),
                    "resolved_model_version": None if confirmed else state.get("resolved_model_version"),
                    "job_id": state.get("job_id"),
                    "details": {
                        "idle_seconds": idle_seconds,
                        "timeout_seconds": timeout_seconds,
                        **details,
                    },
                }
            )
            return True
        finally:
            await self.release_gpu_lease()

    async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        runtime_url = self.runtime_urls.get(runtime)
        if not runtime_url:
            return {"status": "skipped", "reason": "runtime_url_missing", "runtime": runtime, "action": action}
        url = f"{runtime_url}/b1/runtime/{action}"
        headers = {"Accept": "application/json"}
        if self.runtime_control_token:
            if runtime == "lipsync":
                headers["X-B1-Runtime-Token"] = self.runtime_control_token
            else:
                headers["Authorization"] = f"Bearer {self.runtime_control_token}"
        try:
            async with httpx.AsyncClient(timeout=1800.0, **self.runtime_httpx_kwargs(runtime)) as client:
                response = await client.post(url, json=payload, headers=headers)
        except (httpx.HTTPError, OSError):
            return {"status": "unsupported", "reason": "runtime_control_unreachable", "runtime": runtime, "action": action}
        if response.status_code in {404, 405}:
            return {"status": "unsupported", "reason": f"http_{response.status_code}", "runtime": runtime, "action": action}
        if response.status_code >= 400:
            hook_evidence: dict[str, Any] = {}
            try:
                response_body = response.json()
            except ValueError:
                response_body = None
            if isinstance(response_body, dict):
                hook_evidence = self.compact_hook_result(response_body)
            evidence_suffix = (
                f": {json.dumps(hook_evidence, sort_keys=True, separators=(',', ':'))[:1000]}"
                if hook_evidence
                else ""
            )
            raise RuntimeError(
                f"{runtime} runtime {action} hook returned HTTP "
                f"{response.status_code}{evidence_suffix}"
            )
        if not response.content:
            return {"status": "ok", "runtime": runtime, "action": action}
        try:
            body = response.json()
        except ValueError:
            return {"status": "ok", "runtime": runtime, "action": action}
        return body if isinstance(body, dict) else {"status": "ok", "runtime": runtime, "action": action}

    async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        runtime = gpu_runtime_for_job(job)
        try:
            result = await self.post_runtime_control(runtime, "load", self.runtime_control_payload(job))
        except Exception as exc:
            await self.record_runtime_state_for_job(runtime, "load_failed", "loading", job, {"error": exc.__class__.__name__})
            raise
        await self.record_runtime_state_for_job(
            runtime,
            self.runtime_hook_state_status("load", result),
            "loading",
            job,
            {"hook": self.compact_hook_result(result)},
        )
        self.assert_runtime_prepared(runtime, "load", result)
        return result

    async def warm_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        runtime = gpu_runtime_for_job(job)
        try:
            result = await self.post_runtime_control(runtime, "warm", self.runtime_control_payload(job))
        except Exception as exc:
            await self.record_runtime_state_for_job(runtime, "warm_failed", "warming", job, {"error": exc.__class__.__name__})
            raise
        await self.record_runtime_state_for_job(
            runtime,
            self.runtime_hook_state_status("warm", result),
            "warming",
            job,
            {"hook": self.compact_hook_result(result)},
        )
        self.assert_runtime_prepared(runtime, "warm", result)
        return result

    async def smoke_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        runtime = gpu_runtime_for_job(job)
        try:
            result = await self.post_runtime_control(runtime, "smoke", self.runtime_control_payload(job))
        except Exception as exc:
            await self.record_runtime_state_for_job(runtime, "smoke_failed", "smoke", job, {"error": exc.__class__.__name__})
            raise
        await self.record_runtime_state_for_job(
            runtime,
            self.runtime_hook_state_status("smoke", result),
            "smoke",
            job,
            {"hook": self.compact_hook_result(result)},
        )
        return result

    async def record_runtime_failure(self, job: dict[str, Any], exc: Exception) -> None:
        try:
            await self.record_runtime_state_for_job(
                gpu_runtime_for_job(job),
                "failed",
                "failed",
                job,
                {"error": exc.__class__.__name__},
            )
        except Exception:
            return

    def gpu_memory_used_mib(self, metrics: dict[str, Any] | None) -> list[int] | None:
        gpu = (metrics or {}).get("gpu") or {}
        if not gpu.get("available"):
            return None
        values: list[int] = []
        for device in gpu.get("devices") or []:
            used = device.get("memory_used_mib")
            if isinstance(used, (int, float)):
                values.append(int(used))
        return values if values else None

    def host_ram_used_mib(self, metrics: dict[str, Any] | None) -> int | None:
        memory = (metrics or {}).get("memory") or {}
        if not memory.get("available"):
            return None
        used = memory.get("used_bytes")
        if not isinstance(used, (int, float)):
            return None
        return int(max(0, used) / (1024 * 1024))

    def peak_resource_changes(self, job: dict[str, Any], metrics: dict[str, Any] | None) -> dict[str, int]:
        changes: dict[str, int] = {}
        vram_values = self.gpu_memory_used_mib(metrics)
        if vram_values:
            observed = max(vram_values)
            current = job.get("peak_vram_mib")
            if not isinstance(current, (int, float)) or observed > int(current):
                changes["peak_vram_mib"] = observed
        ram_used = self.host_ram_used_mib(metrics)
        if ram_used is not None:
            current = job.get("peak_ram_mib")
            if not isinstance(current, (int, float)) or ram_used > int(current):
                changes["peak_ram_mib"] = ram_used
        return changes

    async def record_peak_resources_from_metrics(self, job: dict[str, Any], metrics: dict[str, Any] | None) -> None:
        changes = self.peak_resource_changes(job, metrics)
        if not changes:
            return
        job.update(changes)
        if self.has_durable_job_record(job):
            await database.update_job(str(job["id"]), **changes)

    async def record_peak_resources(self, job: dict[str, Any]) -> None:
        runtime = gpu_runtime_for_job(job)
        await self.record_peak_resources_from_metrics(job, await self.runtime_metrics(runtime))

    async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
        runtime = gpu_runtime_for_job(job)
        before_metrics = await self.runtime_metrics(runtime)
        await self.record_peak_resources_from_metrics(job, before_metrics)
        before = self.gpu_memory_used_mib(before_metrics)
        if before is None or max(before) <= self.reserve_vram_mib:
            return
        if self.has_durable_job_record(job):
            await database.update_job(
                job["id"],
                state=JobState.VERIFYING_VRAM.value,
                stage="verifying_vram_recovering",
                progress=38,
                failure_category="vram_above_reserve",
                failure_message=f"VRAM used {max(before)} MiB exceeds reserve {self.reserve_vram_mib} MiB before {gpu_runtime_for_job(job)}",
            )
        else:
            await self.record_runtime_state_for_job(
                gpu_runtime_for_job(job),
                "recovering",
                "verifying_vram",
                job,
                {"memory_used_mib": max(before), "reserve_mib": self.reserve_vram_mib},
            )
        recovery_payload = {
            **self.runtime_control_payload(job),
            "reason": f"VRAM used {max(before)} MiB exceeds reserve {self.reserve_vram_mib} MiB before job {job['id']}",
            "timeout_seconds": self.recovery_timeout_seconds,
        }
        if runtime in LAN_MANAGED_GPU_RUNTIMES:
            await self.post_runtime_control(runtime, "recover", recovery_payload)
        else:
            await self.runtime_agent_post(f"/v1/runtime-actions/{runtime}/recover", recovery_payload)
        after_metrics = await self.runtime_metrics(runtime)
        await self.record_peak_resources_from_metrics(job, after_metrics)
        after = self.gpu_memory_used_mib(after_metrics)
        if after is not None and max(after) > self.reserve_vram_mib:
            raise RuntimeError(f"VRAM remains above reserve after recovery: {max(after)} MiB > {self.reserve_vram_mib} MiB")
        if self.has_durable_job_record(job):
            await database.update_job(
                job["id"],
                state=JobState.VERIFYING_VRAM.value,
                stage="verifying_vram_recovered",
                progress=39,
                failure_category=None,
                failure_message=None,
            )
        else:
            await self.record_runtime_state_for_job(
                gpu_runtime_for_job(job),
                "recovered",
                "verifying_vram",
                job,
                {"memory_used_mib": max(after) if after else None, "reserve_mib": self.reserve_vram_mib},
            )

    def request_input(self, job: dict[str, Any]) -> dict[str, Any]:
        request_params = job.get("request_params")
        if not isinstance(request_params, dict):
            return {}
        input_payload = request_params.get("input")
        return input_payload if isinstance(input_payload, dict) else {}

    def is_talking_head_lipsync_job(self, job: dict[str, Any]) -> bool:
        return str(job.get("modality") or "") == "video" and is_talking_head_lipsync_operation(job.get("operation"))

    def is_studio_panel_shot_job(self, job: dict[str, Any]) -> bool:
        return str(job.get("modality") or "") == "image" and str(job.get("operation") or "") in STUDIO_PANEL_SHOT_OPERATIONS and str(job.get("model_alias") or "") == "studio-panel-shot"

    def is_studio_seated_character_job(self, job: dict[str, Any]) -> bool:
        return (
            str(job.get("modality") or "") == "image"
            and str(job.get("operation") or "") in STUDIO_SEATED_CHARACTER_OPERATIONS
            and str(job.get("model_alias") or "") in {"studio-seated-character", "studio-seated-character-p40"}
        )

    @staticmethod
    def studio_panel_geometry(seat: int) -> tuple[dict[str, float], tuple[float, float]]:
        # Keep every participant behind the actual desk in the supplied master
        # plate. These positions deliberately form one panel rather than six
        # independent portrait columns.
        centers = {1: 0.290, 2: 0.375, 3: 0.460, 4: 0.540, 5: 0.625, 6: 0.710}
        center = centers[seat]
        return ({"x": round(center - 0.036, 4), "y": 0.455, "width": 0.072, "height": 0.145}, (center, 0.43))

    @staticmethod
    def studio_panel_wall_screen_quad() -> list[dict[str, float]]:
        return [
            {"x": 0.225, "y": 0.275},
            {"x": 0.775, "y": 0.275},
            {"x": 0.775, "y": 0.665},
            {"x": 0.225, "y": 0.665},
        ]

    @staticmethod
    def studio_panel_image(content: bytes, field_name: str) -> Image.Image:
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                return image.convert("RGBA")
        except Exception as exc:
            raise ValueError(f"{field_name} is not a readable image") from exc

    @staticmethod
    def seated_pose_control_image(width: int, height: int) -> bytes:
        """Render a conservative, upright OpenPose-compatible seated guide."""
        image = Image.new("RGB", (width, height), "black")
        draw = ImageDraw.Draw(image)
        scale_x = width / 512
        scale_y = height / 720

        def point(x: float, y: float) -> tuple[int, int]:
            return int(round(x * scale_x)), int(round(y * scale_y))

        joints = {
            "nose": point(256, 112), "neck": point(256, 178),
            "left_shoulder": point(194, 205), "left_elbow": point(174, 295), "left_wrist": point(214, 365),
            "right_shoulder": point(318, 205), "right_elbow": point(338, 295), "right_wrist": point(298, 365),
            # This is deliberately not a squat. The knees stay beneath the
            # hips and close to the centre line; lower legs are mostly hidden
            # by the studio desk in the final plate.
            "left_hip": point(218, 392), "left_knee": point(205, 472), "left_ankle": point(216, 592),
            "right_hip": point(294, 392), "right_knee": point(307, 472), "right_ankle": point(296, 592),
        }
        limbs = [
            ("nose", "neck", (255, 0, 0)), ("neck", "left_shoulder", (255, 85, 0)), ("left_shoulder", "left_elbow", (255, 170, 0)),
            ("left_elbow", "left_wrist", (255, 255, 0)), ("neck", "right_shoulder", (170, 255, 0)), ("right_shoulder", "right_elbow", (85, 255, 0)),
            ("right_elbow", "right_wrist", (0, 255, 0)), ("neck", "left_hip", (0, 255, 170)), ("left_hip", "left_knee", (0, 255, 255)),
            ("left_knee", "left_ankle", (0, 170, 255)), ("neck", "right_hip", (0, 85, 255)), ("right_hip", "right_knee", (0, 0, 255)),
            ("right_knee", "right_ankle", (85, 0, 255)), ("left_hip", "right_hip", (170, 0, 255)),
        ]
        stroke = max(4, int(round(min(width, height) / 90)))
        for start, end, color in limbs:
            draw.line((joints[start], joints[end]), fill=color, width=stroke)
        radius = max(5, stroke + 2)
        for joint in joints.values():
            draw.ellipse((joint[0] - radius, joint[1] - radius, joint[0] + radius, joint[1] + radius), fill=(255, 255, 255))
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def seated_identity_source(self, portrait: bytes, full_body: bytes, width: int, height: int) -> bytes:
        """Build a native-size latent from the supplied seated portrait.

        The approved portrait already contains the identity in a natural
        desk-seated pose.  Keep its complete vertical frame instead of using
        ``ImageOps.fit`` (which turned the face into a 16:9 close-up) or asking
        SD 1.5 to reconstruct the character from the standing full-body card.
        A low-denoise, pose-conditioned img2img pass then exercises the native
        1280x720 P40 path while retaining the source identity and anatomy.
        """
        source = self.studio_panel_image(portrait, "portrait_artifact_id").convert("RGB")
        self.studio_panel_image(full_body, "full_body_artifact_id")
        seated = ImageOps.contain(
            source,
            (int(width * 0.72), height),
            method=Image.Resampling.LANCZOS,
        )
        canvas = Image.new("RGB", (width, height), seated.getpixel((0, 0)))
        canvas.paste(seated, ((width - seated.width) // 2, (height - seated.height) // 2))
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def seated_inpaint_mask_image(width: int, height: int) -> bytes:
        """Redraw the full-body reference below the preserved head and neck."""
        image = Image.new("RGB", (width, height), "black")
        draw = ImageDraw.Draw(image)
        draw.polygon(
            [
                (int(width * 0.37), int(height * 0.23)),
                (int(width * 0.63), int(height * 0.23)),
                (int(width * 0.75), int(height * 0.88)),
                (int(width * 0.25), int(height * 0.88)),
            ],
            fill="white",
        )
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def seated_plate_shape_mask(width: int, height: int) -> Image.Image:
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((int(width * 0.30), int(height * 0.05), int(width * 0.70), int(height * 0.35)), fill=255)
        draw.polygon(
            [
                (int(width * 0.30), int(height * 0.26)), (int(width * 0.70), int(height * 0.26)),
                # The lower edge is deliberately narrow and desk-occluded.
                # It leaves room for a torso and forearms without admitting
                # the invented calves/feet that made previous plates unusable.
                (int(width * 0.84), int(height * 0.50)), (int(width * 0.80), int(height * 0.60)),
                (int(width * 0.68), int(height * 0.64)), (int(width * 0.32), int(height * 0.64)),
                (int(width * 0.20), int(height * 0.60)), (int(width * 0.16), int(height * 0.50)),
            ],
            fill=255,
        )
        return mask.filter(ImageFilter.GaussianBlur(radius=max(2, width // 120)))

    @staticmethod
    def refine_seated_human_alpha(image: Image.Image, alpha: Image.Image) -> Image.Image:
        """Use a constrained foreground pass to remove desk/glow false positives.

        U2Net is retained as an independent semantic admission check, but its
        human mask treats the bright aura around stylised characters as a
        second torso.  The P40 workflow deliberately places the complete
        portrait in a fixed-width central card, so GrabCut can use a measured
        inner rectangle whose outside is definite background.  Initialising
        the entire broad seated envelope as probable foreground reproduced
        that aura as an opaque card; the inner rectangle isolates the actual
        head, jacket and forearms on the validated 1280x720 output.

        This is deliberately a CPU post-process and never opens a network
        model or consumes the shared GPU lease.
        """
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise SeatedPosePipelineUnavailableError(
                "seated_pose_pipeline_unavailable: the local GrabCut matting runtime is not installed"
            ) from exc
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        height, width = rgb.shape[:2]
        coarse = np.asarray(alpha, dtype=np.uint8)
        semantic_core = coarse[
            int(height * 0.10) : int(height * 0.58),
            int(width * 0.38) : int(width * 0.66),
        ]
        if semantic_core.size == 0 or float(semantic_core.mean()) < 18.0:
            raise SeatedCharacterQualityError(
                "studio_seated_character_qc_failed: semantic matte found no centred seated subject"
            )
        mask = np.zeros((height, width), dtype=np.uint8)
        rect = (
            round(width * 0.3711),
            round(height * 0.0972),
            max(2, int(width * 0.301)),
            max(2, int(height * 0.632)),
        )
        background_model = np.zeros((1, 65), np.float64)
        foreground_model = np.zeros((1, 65), np.float64)
        try:
            cv2.grabCut(rgb, mask, rect, background_model, foreground_model, 7, cv2.GC_INIT_WITH_RECT)
        except cv2.error as exc:
            raise SeatedCharacterQualityError("studio_seated_character_qc_failed: foreground matting could not isolate the seated reference") from exc
        refined = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        refined = cv2.morphologyEx(refined, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8))
        refined = cv2.GaussianBlur(refined, (0, 0), sigmaX=1.2)
        return Image.fromarray(refined, mode="L")

    @staticmethod
    def seated_plate_anatomy_metrics(alpha: Image.Image) -> dict[str, float]:
        """Measure silhouette width/gaps in the region that used to become a squat."""
        width, height = alpha.size
        threshold = 160

        def band_metrics(start: float, end: float) -> tuple[float, float, float]:
            widths: list[int] = []
            gaps: list[int] = []
            for y in range(int(height * start), int(height * end)):
                occupied = [x for x in range(width) if alpha.getpixel((x, y)) >= threshold]
                if not occupied:
                    continue
                widths.append(occupied[-1] - occupied[0] + 1)
                spans = 1
                largest_gap = 0
                previous = occupied[0]
                for x in occupied[1:]:
                    if x > previous + 1:
                        spans += 1
                        largest_gap = max(largest_gap, x - previous - 1)
                    previous = x
                if spans > 1:
                    gaps.append(largest_gap)
            if not widths:
                return 0.0, 0.0, 0.0
            return sum(widths) / len(widths) / width, max(widths) / width, max(gaps, default=0) / width

        # Stop before the shoulders begin; their natural width is not evidence
        # of a background halo around the head.
        head_width, head_max_width, head_gap = band_metrics(0.05, 0.28)
        torso_width, _, torso_gap = band_metrics(0.36, 0.54)
        lower_width, _, lower_gap = band_metrics(0.54, 0.76)
        return {
            "head_width_ratio": round(head_width, 4),
            "head_max_width_ratio": round(head_max_width, 4),
            "head_gap_ratio": round(head_gap, 4),
            "torso_width_ratio": round(torso_width, 4),
            "torso_gap_ratio": round(torso_gap, 4),
            "lower_body_width_ratio": round(lower_width, 4),
            "lower_body_gap_ratio": round(lower_gap, 4),
        }

    def seated_general_matte_model_path(self) -> Path:
        # The model is an immutable Model Hub blob, never an unverified cache
        # fetched by an inference request. artifact_root is /srv/b1-ai-hub/
        # artifacts in production and keeps this location configurable in tests.
        return self.artifact_root.parent / "models" / "blobs" / SEATED_GENERAL_MATTE_SHA256

    def seated_general_segmentation_alpha(self, image: Image.Image) -> tuple[Image.Image, float]:
        """Return a general-object matte without consuming the managed GPU lease."""
        model_path = self.seated_general_matte_model_path()
        if not model_path.is_file():
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: install the pinned BiRefNet general-object matting dependency before generating seated character plates")
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: the local ONNX matting runtime is not installed") from exc
        if self._seated_general_matte_session is None:
            try:
                options = ort.SessionOptions()
                options.intra_op_num_threads = 18
                options.inter_op_num_threads = 1
                options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                self._seated_general_matte_session = ort.InferenceSession(
                    str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
                )
            except Exception as exc:
                raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: the pinned BiRefNet general-object matting model could not be loaded") from exc
        # BiRefNet's published wrapper resizes the full frame. Cropping a 16:9
        # plate to a square changes the subject/background context and caused
        # the very near-body halo this stage is intended to reject.
        resized = image.convert("RGB").resize((1024, 1024), Image.Resampling.LANCZOS)
        pixels = np.asarray(resized, dtype=np.float32) / 255.0
        normalized = (pixels - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
        tensor = normalized.transpose(2, 0, 1)[None, ...]
        started = monotonic()
        try:
            input_name = self._seated_general_matte_session.get_inputs()[0].name
            logits = self._seated_general_matte_session.run(None, {input_name: tensor})[0][0][0]
            output = 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))
        except Exception as exc:
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: BiRefNet general-object matting inference failed") from exc
        inference_seconds = monotonic() - started
        minimum, maximum = float(output.min()), float(output.max())
        if maximum - minimum < 1e-6:
            raise SeatedCharacterQualityError("seated_plate_matte_failed: BiRefNet produced an empty general-object segmentation mask")
        alpha = ((output - minimum) / (maximum - minimum) * 255.0).clip(0, 255).astype("uint8")
        return Image.fromarray(alpha, mode="L").resize(image.size, Image.Resampling.LANCZOS), inference_seconds

    @staticmethod
    def seated_plate_halo_metrics(alpha: Image.Image) -> dict[str, float]:
        """Measure the empty wedges where the former opaque source card leaked."""
        width, height = alpha.size
        boxes = {
            "left": (int(width * 0.39), int(height * 0.09), int(width * 0.445), int(height * 0.29)),
            "right": (int(width * 0.555), int(height * 0.09), int(width * 0.61), int(height * 0.29)),
        }
        means = {side: ImageStat.Stat(alpha.crop(box)).mean[0] for side, box in boxes.items()}
        return {
            "matte_halo_left_alpha": round(means["left"], 3),
            "matte_halo_right_alpha": round(means["right"], 3),
            "matte_halo_max_alpha": round(max(means.values()), 3),
        }

    def matte_seated_character_plate(self, content: bytes, width: int, height: int) -> tuple[bytes, dict[str, Any]]:
        image = ImageOps.fit(self.studio_panel_image(content, "seated_pose_output").convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
        alpha, matte_inference_seconds = self.seated_general_segmentation_alpha(image)
        alpha = ImageChops.multiply(alpha, self.seated_plate_shape_mask(width, height))
        bbox = alpha.getbbox()
        if bbox is None or bbox[2] - bbox[0] < width * 0.28 or bbox[3] - bbox[1] < height * 0.45:
            raise SeatedCharacterQualityError("seated_plate_matte_failed: generated output could not be matted into a usable seated figure")
        border = Image.new("L", (width, height), 0)
        border_draw = ImageDraw.Draw(border)
        border_draw.rectangle((0, 0, width - 1, height - 1), outline=255, width=max(2, width // 64))
        border_alpha = ImageStat.Stat(ImageChops.multiply(alpha, border)).mean[0]
        if border_alpha > 18:
            raise SeatedCharacterQualityError("seated_plate_matte_failed: generated output retains an opaque source-card border")
        plate = image.convert("RGBA")
        plate.putalpha(alpha)
        # The contained portrait is deliberately centred at a known scale.
        # Measure the actual head region rather than averaging alpha across a
        # box twice as wide as the face, which made a clean matte miss the
        # visibility threshold by including mostly transparent background.
        face_box = (int(width * 0.43), int(height * 0.09), int(width * 0.58), int(height * 0.32))
        face_alpha = ImageStat.Stat(alpha.crop(face_box)).mean[0]
        face_variance = sum(ImageStat.Stat(image.crop(face_box)).var) / 3
        if face_alpha < 55 or face_variance < 35:
            raise SeatedCharacterQualityError("seated_plate_qc_failed: generated seated plate has no usable visible face")
        lower_leg_alpha = ImageStat.Stat(alpha.crop((0, int(height * 0.86), width, height))).mean[0]
        if lower_leg_alpha > 8:
            raise SeatedCharacterQualityError("seated_plate_qc_failed: generated plate retains vertical lower-leg geometry inconsistent with desk-occluded seated compositing")
        anatomy = self.seated_plate_anatomy_metrics(alpha)
        halo = self.seated_plate_halo_metrics(alpha)
        if halo["matte_halo_max_alpha"] > 8:
            raise SeatedCharacterQualityError(
                "studio_seated_character_qc_failed: foreground matting retains an opaque source-background card"
            )
        if anatomy["head_width_ratio"] > 0.35 or anatomy["head_max_width_ratio"] > 0.35:
            raise SeatedCharacterQualityError(
                "studio_seated_character_qc_failed: foreground matting retains source background around the head"
            )
        if anatomy["lower_body_width_ratio"] > 0.66 or anatomy["lower_body_gap_ratio"] > 0.18:
            raise SeatedCharacterQualityError(
                "studio_seated_character_qc_failed: generated plate has an implausibly wide or disconnected lower-body silhouette"
            )
        output = io.BytesIO()
        plate.save(output, format="PNG", optimize=True)
        return output.getvalue(), {
            "transparent_background": True,
            "face_detected": True,
            "body_region": {"x": 0.37, "y": 0.09, "width": 0.31, "height": 0.59},
            "face_region": {"x": 0.43, "y": 0.09, "width": 0.15, "height": 0.23},
            "quality_control": {
                # Pose/identity fidelity is a visual acceptance decision. The
                # automated stage proves a real pose-conditioned generation,
                # semantic matte, transparent output, and usable face, but a
                # reviewer must still reject visual artefacts before the plate
                # is allowed to occupy a production studio seat.
                "status": "review_required",
                "identity_reference_used": True,
                "pose_conditioning_used": True,
                # Automatic pose detection is deliberately conservative. A
                # passing silhouette is still review_required, but malformed
                # plates fail before they can be offered for approval.
                "seated_pose_detected": True,
                "source_card_compositing": False,
                "matte_border_alpha": round(border_alpha, 3),
                "matte_model": SEATED_GENERAL_MATTE_MODEL,
                "matte_model_sha256": SEATED_GENERAL_MATTE_SHA256,
                "matte_inference_seconds": round(matte_inference_seconds, 3),
                "face_variance": round(face_variance, 3),
                "lower_leg_alpha": round(lower_leg_alpha, 3),
                **halo,
                **anatomy,
            },
        }

    def media_runtime_is_remote(self, job: dict[str, Any]) -> bool:
        return str(job.get("runtime") or "") == "lan-p40-media"

    def comfyui_url_for_job(self, job: dict[str, Any]) -> str:
        if self.media_runtime_is_remote(job):
            base = self.runtime_urls.get("lan-p40-media", "")
            return f"{base}/comfyui" if base else ""
        return self.runtime_urls.get("comfyui", "")

    def lipsync_url_for_job(self, job: dict[str, Any]) -> str:
        if self.media_runtime_is_remote(job):
            base = self.runtime_urls.get("lan-p40-media", "")
            return f"{base}/lipsync" if base else ""
        return self.runtime_urls.get("lipsync", "")

    def media_runtime_headers(self, job: dict[str, Any]) -> dict[str, str]:
        if not self.media_runtime_is_remote(job) or not self.runtime_control_token:
            return {}
        return {"Authorization": f"Bearer {self.runtime_control_token}"}

    def lipsync_runtime_headers(self, job: dict[str, Any]) -> dict[str, str]:
        headers = self.media_runtime_headers(job)
        if self.runtime_control_token:
            # The remote Caddy route authenticates Authorization, while the
            # internal MuseTalk service independently authenticates this
            # runtime-only header. Both must be present on the managed path.
            headers["X-B1-Runtime-Token"] = self.runtime_control_token
        return headers

    def media_runtime_client_kwargs(self, job: dict[str, Any]) -> dict[str, Any]:
        if not self.media_runtime_is_remote(job):
            return {}
        return self.runtime_httpx_kwargs("lan-p40-media")

    async def comfyui_model_names(self, job: dict[str, Any], folder: str) -> list[str]:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: ComfyUI runtime is not configured")
        async with httpx.AsyncClient(timeout=15.0, **self.media_runtime_client_kwargs(job)) as client:
            response = await client.get(f"{comfyui_url}/models/{folder}", headers=self.media_runtime_headers(job))
        if response.status_code >= 400:
            raise SeatedPosePipelineUnavailableError(f"seated_pose_pipeline_unavailable: ComfyUI model registry {folder} returned HTTP {response.status_code}")
        body = response.json()
        return [item for item in body if isinstance(item, str)] if isinstance(body, list) else []

    async def upload_comfyui_image_bytes(self, *, job: dict[str, Any], field_name: str, content: bytes, filename: str) -> str:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: ComfyUI runtime is not configured")
        safe_name = f"b1-seated-{media_artifacts.safe_artifact_segment(str(job['id']), 'job')}-{field_name}-{media_artifacts.safe_artifact_segment(filename, 'input.png')}"
        async with httpx.AsyncClient(timeout=60.0, **self.media_runtime_client_kwargs(job)) as client:
            response = await client.post(
                f"{comfyui_url}/upload/image",
                files={"image": (safe_name, content, "image/png")},
                data={"overwrite": "false"},
                headers=self.media_runtime_headers(job),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"ComfyUI seated-character upload returned HTTP {response.status_code}")
        body = response.json()
        name = body.get("name") if isinstance(body, dict) else None
        subfolder = body.get("subfolder") if isinstance(body, dict) else ""
        if not isinstance(name, str) or not name or not isinstance(subfolder, str):
            raise RuntimeError("ComfyUI seated-character upload returned an invalid filename")
        return f"{subfolder}/{name}".lstrip("/") if subfolder else name

    async def run_studio_seated_character_job(self, job: dict[str, Any]) -> None:
        payload = dict(self.request_input(job))
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="seated_character_validating_inputs", progress=70)
        refs = {field: self.staged_input_reference(job, payload.get(field), field) for field in ("portrait_artifact_id", "full_body_artifact_id", "studio_reference_artifact_id")}
        inputs: dict[str, bytes] = {}
        for field, reference in refs.items():
            content, mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, reference)
            if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError(f"{field} must reference a PNG, JPEG, or WebP upload")
            inputs[field] = content
        width = request_int(payload, "width", 512, 384, 1280)
        height = request_int(payload, "height", 720, 512, 720)
        controlnet_name = "b1-control-v11p-sd15-openpose-fp16.safetensors"
        checkpoint_name = "sd-v1-5-pruned-emaonly-b1.safetensors"
        if controlnet_name not in await self.comfyui_model_names(job, "controlnet"):
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: install the pinned OpenPose ControlNet dependency before generating seated character plates")
        if checkpoint_name not in await self.comfyui_model_names(job, "checkpoints"):
            raise SeatedPosePipelineUnavailableError("seated_pose_pipeline_unavailable: the pinned SD 1.5 checkpoint is unavailable")
        source = self.seated_identity_source(inputs["portrait_artifact_id"], inputs["full_body_artifact_id"], width, height)
        pose = self.seated_pose_control_image(width, height)
        source_name = await self.upload_comfyui_image_bytes(job=job, field_name="identity", content=source, filename="identity.png")
        pose_name = await self.upload_comfyui_image_bytes(job=job, field_name="openpose", content=pose, filename="openpose.png")
        prompt = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint_name}},
            "2": {"class_type": "LoadImage", "inputs": {"image": source_name}},
            "3": {"class_type": "LoadImage", "inputs": {"image": pose_name}},
            "4": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": controlnet_name}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "preserve the supplied character identity, face, futuristic jacket, torso, arms, and hands exactly; one upright character seated naturally at a panel desk, shoulders level, hands resting on lap or desk, knees together beneath the desk, front three-quarter camera, isolated transparent-ready background", "clip": ["1", 1]}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "wide-legged squat, spread legs, crouching, disconnected limbs, extra limbs, malformed hands, malformed feet, standing, bare feet, generic shirt, different clothing, duplicate head, empty chair, desk, studio, background, text, logo, watermark", "clip": ["1", 1]}},
            "7": {"class_type": "ControlNetApply", "inputs": {"conditioning": ["5", 0], "control_net": ["4", 0], "image": ["3", 0], "strength": 0.70}},
            "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
            "11": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": int(payload.get("seed", 20260802)), "steps": 24, "cfg": 4.0, "sampler_name": "dpmpp_2m", "scheduler": "karras", "positive": ["7", 0], "negative": ["6", 0], "latent_image": ["10", 0], "denoise": 0.12}},
            "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["1", 2]}},
            "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": f"b1-seated-{job['id']}"}},
        }
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="seated_character_pose_generating", progress=78)
        prompt_id = await self.submit_comfyui_prompt(job, {"prompt": prompt, "client_id": str(job["id"]), "extra_data": {"b1": {"job_id": str(job["id"]), "operation": "studio-seated-character"}}})
        await database.update_job(job["id"], native_prompt_id=prompt_id)
        history = await self.wait_for_comfyui_history(job, prompt_id)
        if history is None:
            current = await database.get_job(job["id"])
            if current and current["state"] == JobState.CANCELLED.value:
                return
            raise RuntimeError("seated_pose_pipeline_failed: ComfyUI did not complete the seated-character workflow")
        native_artifacts = comfyui_native.comfyui_artifacts_from_history(prompt_id, history)
        ingested = await self.ingest_comfyui_artifacts(job, native_artifacts)
        generated = next((artifact for artifact in ingested if artifact.get("mime_type") == "image/png" and artifact.get("ingest_status") == "stored"), None)
        if not isinstance(generated, dict) or not isinstance(generated.get("path"), str):
            raise RuntimeError("seated_pose_pipeline_failed: ComfyUI produced no readable image artifact")
        generated_bytes = media_artifacts.read_regular_file_bytes(media_artifacts.artifact_store_path(self.artifact_root, generated["path"]))
        plate, evidence = self.matte_seated_character_plate(generated_bytes, width, height)
        reference = media_artifacts.write_staged_input_bytes(self.artifact_root, owner_id=str(job.get("owner_id") or ""), field_name="seated_character", content=plate, declared_mime_type="image/png", filename=f"seated-{payload['participant_id']}.png")
        seated_character = {
            "participant_id": payload["participant_id"], "seat": payload["seat"], "pose": payload["pose"],
            "seated_reference_artifact_id": reference["id"], **evidence,
        }
        artifact = media_artifacts.write_artifact_bytes(self.artifact_root, namespace="studio-seated-character", job_id=str(job["id"]), index=0, content=plate, mime_type="image/png", source="b1_sd15_openpose_seated_plate", metadata={"type": "image", "runtime": str(job.get("runtime") or "comfyui"), "model": self.resolved_model_id(job), "operation": "studio-seated-character", "seated_character": seated_character})
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])

    @staticmethod
    def studio_panel_subject_mask(width: int, height: int, *, head_only: bool) -> Image.Image:
        """Return a conservative foreground mask for centred character art.

        Reference uploads frequently include a dark generated backdrop. The
        subject geometry intentionally excludes that backdrop before the
        luminance key below is applied, preventing a six-card strip from being
        mistaken for a shared studio panel.
        """
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        if head_only:
            draw.ellipse((int(width * 0.16), 0, int(width * 0.84), int(height * 0.86)), fill=255)
        else:
            draw.ellipse((int(width * 0.28), 0, int(width * 0.72), int(height * 0.40)), fill=255)
            draw.polygon(
                [
                    (int(width * 0.30), int(height * 0.25)),
                    (int(width * 0.70), int(height * 0.25)),
                    (int(width * 0.90), int(height * 0.58)),
                    (int(width * 0.76), height),
                    (int(width * 0.24), height),
                    (int(width * 0.10), int(height * 0.58)),
                ],
                fill=255,
            )
        return mask.filter(ImageFilter.GaussianBlur(radius=max(2, width // 28)))

    def studio_panel_subject_layer(
        self,
        source: Image.Image,
        size: tuple[int, int],
        *,
        head_only: bool,
    ) -> Image.Image:
        """Fit a centred character into a transparent, non-card layer."""
        width, height = size
        if head_only:
            # Identity portraits are usually vertical chest-up renders. Use a
            # deliberately tight centre crop so the declared face region in
            # the panel is a real face-sized source for a later native camera,
            # rather than an entire portrait card containing a tiny face.
            crop_left = int(source.width * 0.27)
            crop_right = max(crop_left + 1, int(source.width * 0.73))
            crop_bottom = max(1, int(source.height * 0.48))
        else:
            crop_left = int(source.width * 0.12)
            crop_right = max(crop_left + 1, int(source.width * 0.88))
            crop_bottom = max(1, int(source.height * 0.72))
        layer = ImageOps.fit(
            source.crop((crop_left, 0, crop_right, crop_bottom)),
            size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.22),
        )
        # A dark background is common in supplied identity art. Keep the
        # visible face/costume detail while keying near-black pixels out so it
        # cannot form an opaque band over the supplied studio screen.
        luminance = ImageOps.grayscale(layer.convert("RGB"))
        luminance_mask = luminance.point(lambda value: 0 if value < 14 else min(255, (value - 14) * 5))
        alpha = ImageChops.multiply(self.studio_panel_subject_mask(width, height, head_only=head_only), luminance_mask)
        layer.putalpha(alpha)
        return layer

    def compose_studio_panel_image(
        self,
        *,
        studio: bytes,
        participants: list[dict[str, Any]],
        width: int,
        height: int,
        stature_reference_participant_id: str | None = None,
    ) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Composite approved RGBA seated plates into one fixed studio scene.

        This intentionally does no prompt-time identity regeneration.  The
        approved plate is the sole character visual source, and the original
        studio is reintroduced as chair/desk foreground layers so a character
        cannot simply appear standing behind an empty chair.
        """
        base = ImageOps.fit(
            self.studio_panel_image(studio, "studio_reference_artifact_id"),
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        canvas = base.copy()
        table_y = int(round(height * 0.61))
        # Six fixed seats have roughly 8.5% frame spacing. Keep each approved
        # plate inside its physical seat envelope so QC can reject accidental
        # overlap without rejecting a normal six-person panel.
        person_width = max(54, int(round(width * 0.075)))
        person_height = max(170, int(round(height * 0.55)))
        seat_map: list[dict[str, Any]] = []
        occupancy: list[dict[str, Any]] = []
        body_boxes: list[tuple[str, tuple[int, int, int, int]]] = []
        prepared_participants: list[dict[str, Any]] = []

        # Preserve the physical chair back where each plate meets the desk.
        # It is a masked crop of the caller's actual studio, not a generated
        # card or a replacement studio background.
        chair_back_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for participant in sorted(participants, key=lambda item: int(item["seat"])):
            _, (center_x, _) = self.studio_panel_geometry(int(participant["seat"]))
            chair_width = max(44, int(width * 0.105))
            chair_height = max(42, int(height * 0.14))
            left = int(center_x * width - chair_width / 2)
            top = int(height * 0.47)
            chair = base.crop((max(0, left), top, min(width, left + chair_width), min(height, top + chair_height)))
            if chair.width != chair_width or chair.height != chair_height:
                padded = Image.new("RGBA", (chair_width, chair_height), (0, 0, 0, 0))
                padded.alpha_composite(chair, (max(0, -left), 0))
                chair = padded
            chair_mask = Image.new("L", (chair_width, chair_height), 0)
            ImageDraw.Draw(chair_mask).rounded_rectangle((0, 0, chair_width - 1, chair_height - 1), radius=max(8, chair_width // 6), fill=180)
            chair.putalpha(chair_mask)
            chair_back_layer.alpha_composite(chair, (left, top))
        canvas.alpha_composite(chair_back_layer)

        for participant in sorted(participants, key=lambda item: int(item["seat"])):
            plate = self.studio_panel_image(participant["seated_plate"], "seated_reference_artifact_id")
            alpha = plate.getchannel("A")
            alpha_bbox = alpha.getbbox()
            if alpha_bbox is None or ImageStat.Stat(alpha).mean[0] > 245:
                raise StudioPanelQualityError(f"studio_panel_qc_failed: participant {participant['participant_id']} seated plate is not a usable transparent figure")
            original_width, original_height = plate.size
            evidence = participant["seated_character"]
            source_face = evidence["face_region"]
            face_pixels = (
                max(alpha_bbox[0], int(round(float(source_face["x"]) * original_width))),
                max(alpha_bbox[1], int(round(float(source_face["y"]) * original_height))),
                min(alpha_bbox[2], int(round((float(source_face["x"]) + float(source_face["width"])) * original_width))),
                min(alpha_bbox[3], int(round((float(source_face["y"]) + float(source_face["height"])) * original_height))),
            )
            if face_pixels[2] <= face_pixels[0] or face_pixels[3] <= face_pixels[1]:
                raise StudioPanelQualityError(f"studio_panel_qc_failed: participant {participant['participant_id']} face geometry does not intersect the transparent figure")
            # Production plates retain the requested 16:9 canvas for artifact
            # provenance. Crop to the non-transparent figure before fitting a
            # physical seat; otherwise ImageOps.contain scales the whole
            # 1280x720 canvas to a 96px slot and makes the character only a few
            # pixels tall.
            plate = plate.crop(alpha_bbox)
            source_face_in_plate = {
                "x": (face_pixels[0] - alpha_bbox[0]) / plate.width,
                "y": (face_pixels[1] - alpha_bbox[1]) / plate.height,
                "width": (face_pixels[2] - face_pixels[0]) / plate.width,
                "height": (face_pixels[3] - face_pixels[1]) / plate.height,
            }
            legacy_layer = ImageOps.contain(
                plate,
                (person_width, person_height),
                method=Image.Resampling.LANCZOS,
            )
            prepared_participants.append(
                {
                    "participant": participant,
                    "plate": plate,
                    "source_face_in_plate": source_face_in_plate,
                    "legacy_height": legacy_layer.height,
                }
            )

        reference_entry = next(
            (
                entry
                for entry in prepared_participants
                if entry["participant"]["participant_id"]
                == stature_reference_participant_id
            ),
            None,
        )
        if stature_reference_participant_id and reference_entry is None:
            raise StudioPanelQualityError(
                "studio_panel_qc_failed: stature reference is not a panel participant"
            )
        if reference_entry is None:
            ordered_heights = sorted(
                int(entry["legacy_height"]) for entry in prepared_participants
            )
            target_body_height = ordered_heights[len(ordered_heights) // 2]
            effective_stature_reference = "median_legacy_fit"
        else:
            target_body_height = int(reference_entry["legacy_height"])
            effective_stature_reference = str(stature_reference_participant_id)

        for entry in prepared_participants:
            participant = entry["participant"]
            plate = entry["plate"]
            source_face_in_plate = entry["source_face_in_plate"]
            face_region, (center_x, _) = self.studio_panel_geometry(int(participant["seat"]))
            target_width = max(
                1, int(round(plate.width * target_body_height / plate.height))
            )
            layer = plate.resize(
                (target_width, target_body_height), resample=Image.Resampling.LANCZOS
            )
            body_left = int(round(width * center_x - layer.width / 2))
            # Anchor the cropped figure at the physical desk line.  The old
            # expression anchored it to the bottom of the much taller maximum
            # seat envelope; a width-limited upper-body plate consequently
            # landed entirely below ``table_y`` and was erased when the real
            # desk foreground was restored.
            body_top = table_y - int(round(layer.height * 0.92))
            canvas.alpha_composite(layer, (body_left, body_top))
            rendered_face = {
                "x": round((body_left + source_face_in_plate["x"] * layer.width) / width, 4),
                "y": round((body_top + source_face_in_plate["y"] * layer.height) / height, 4),
                "width": round(source_face_in_plate["width"] * layer.width / width, 4),
                "height": round(source_face_in_plate["height"] * layer.height / height, 4),
            }
            body_region = {
                "x": round(body_left / width, 4),
                "y": round(body_top / height, 4),
                "width": round(layer.width / width, 4),
                "height": round(layer.height / height, 4),
            }
            seat_map.append({"participant_id": participant["participant_id"], "seat": participant["seat"], "face_region": rendered_face})
            occupancy.append({"participant_id": participant["participant_id"], "seat": participant["seat"], "occupied": True, "seated_pose_detected": True, "body_region": body_region, "face_region": rendered_face, "stature_scale": round(target_body_height / int(entry["legacy_height"]), 4)})
            body_boxes.append((str(participant["participant_id"]), (body_left, body_top, body_left + layer.width, body_top + layer.height)))

        # The original desk and its foreground chair geometry occlude each
        # plate in exactly the same scene, making the seated depth relationship
        # deterministic and avoiding the old empty-chair standing composite.
        canvas.alpha_composite(base.crop((0, table_y, width, height)), (0, table_y))
        difference = ImageChops.difference(base.convert("RGB"), canvas.convert("RGB"))
        mean_delta = sum(ImageStat.Stat(difference).mean) / 3
        if len(participants) > 1 and mean_delta < 1.5:
            raise StudioPanelQualityError("studio_panel_qc_failed: participant composite is not visibly present in the shared studio")
        rear_screen_delta = difference.crop((int(width * 0.20), int(height * 0.36), int(width * 0.80), int(height * 0.61)))
        rear_screen_mean = sum(ImageStat.Stat(rear_screen_delta).mean) / 3
        if rear_screen_mean > 36.0:
            raise StudioPanelQualityError("studio_panel_qc_failed: participant composition obscures the shared rear studio as a source-card band")
        for participant, box in body_boxes:
            crop = difference.crop(box)
            if sum(ImageStat.Stat(crop).mean) / 3 < 2.5:
                raise StudioPanelQualityError(f"studio_panel_qc_failed: declared seat for {participant} is visually unoccupied")
        for index, (_, first) in enumerate(body_boxes):
            for _, second in body_boxes[index + 1:]:
                overlap_width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
                overlap_height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
                overlap = overlap_width * overlap_height
                if overlap > 0.18 * min((first[2] - first[0]) * (first[3] - first[1]), (second[2] - second[0]) * (second[3] - second[1])):
                    raise StudioPanelQualityError("studio_panel_qc_failed: seated character plates materially overlap")
        rendered_body_heights = [box[3] - box[1] for _, box in body_boxes]
        body_height_spread = max(rendered_body_heights) - min(rendered_body_heights)
        body_height_spread_ratio = body_height_spread / target_body_height
        if body_height_spread_ratio > 0.04:
            raise StudioPanelQualityError(
                "studio_panel_qc_failed: seated character stature is not normalized"
            )
        qc = {
            "status": "passed",
            "composition": "shared_studio_seated_panel",
            "participant_count_requested": len(participants),
            "participant_count_visible": len(occupancy),
            "occupied_seat_count": len(occupancy),
            "physical_table_preserved": True,
            "rear_screen_preserved": True,
            "source_card_compositing": False,
            "mean_pixel_delta": round(mean_delta, 3),
            "rear_screen_mean_pixel_delta": round(rear_screen_mean, 3),
            "stature_normalization": "alpha_bounds_reference_height_v1",
            "stature_reference_participant_id": effective_stature_reference,
            "target_body_height_px": target_body_height,
            "body_height_spread_px": body_height_spread,
            "body_height_spread_ratio": round(body_height_spread_ratio, 4),
        }
        output = io.BytesIO()
        canvas.convert("RGB").save(output, format="PNG", optimize=True)
        return output.getvalue(), seat_map, occupancy, qc

    async def run_studio_panel_ffmpeg(self, command: list[str], error_message: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError as exc:
            process.kill()
            with suppress(Exception):
                await process.communicate()
            raise RuntimeError(f"{error_message}: timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500] or "ffmpeg failed"
            raise RuntimeError(f"{error_message}: {detail}")

    async def run_studio_panel_shot_job(self, job: dict[str, Any]) -> None:
        payload = dict(self.request_input(job))
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="studio_panel_validating_inputs", progress=72)
        studio_ref = self.staged_input_reference(job, payload.get("studio_reference_artifact_id"), "studio_reference_artifact_id")
        studio, studio_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, studio_ref)
        if studio_mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("studio_reference_artifact_id must reference a PNG, JPEG, or WebP upload")
        participants = payload.get("participants")
        if not isinstance(participants, list) or not participants:
            raise ValueError("studio-panel-shot requires participants")
        width = request_int(payload, "width", 512, 256, 1280)
        height = request_int(payload, "height", 288, 144, 720)
        if abs(width / height - (16 / 9)) > 0.03:
            raise ValueError("studio-panel-shot requires a 16:9 output")
        composite_inputs: list[dict[str, Any]] = []
        for index, participant in enumerate(sorted(participants, key=lambda item: int(item["seat"]))):
            seated_ref = self.staged_input_reference(job, participant.get("seated_reference_artifact_id"), f"participants[{index}].seated_reference_artifact_id")
            seated_plate, seated_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, seated_ref)
            if seated_mime_type != "image/png":
                raise SeatedReferenceRequiredError(f"participant {participant.get('participant_id') or index} seated reference must be a transparent PNG generated by studio-seated-character")
            reference_id = seated_ref.get("id")
            provenance = await database.get_completed_seated_character_reference(str(job.get("owner_id") or ""), str(reference_id or ""))
            metadata = provenance.get("metadata") if isinstance(provenance, dict) else None
            quality = metadata.get("quality_control") if isinstance(metadata, dict) else None
            if (
                not isinstance(metadata, dict)
                or metadata.get("participant_id") != participant.get("participant_id")
                or metadata.get("seat") != participant.get("seat")
                or metadata.get("transparent_background") is not True
                or not isinstance(quality, dict)
                or quality.get("status") != "passed"
                or quality.get("seated_pose_detected") is not True
            ):
                raise SeatedReferenceRequiredError(f"participant {participant.get('participant_id') or index} has no valid completed seated character plate for the declared seat")
            composite_inputs.append({**participant, "seated_plate": seated_plate, "seated_character": metadata})
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="studio_panel_compositing", progress=82)
        content, seat_map, seat_occupancy, qc = await asyncio.to_thread(
            self.compose_studio_panel_image,
            studio=studio,
            participants=composite_inputs,
            width=width,
            height=height,
            stature_reference_participant_id=(
                str(payload.get("stature_reference_participant_id") or "").strip()
                or None
            ),
        )
        if not content:
            raise StudioPanelQualityError("studio_panel_qc_failed: panel compositor produced an empty image")
        scene_reference = media_artifacts.write_staged_input_bytes(
            self.artifact_root,
            owner_id=str(job.get("owner_id") or ""),
            field_name="scene",
            content=content,
            declared_mime_type="image/png",
            filename="studio-panel-keyframe.png",
        )
        studio_panel = {
            "camera_view": "establishing_wide",
            "seat_map": seat_map,
            "seat_occupancy": seat_occupancy,
            "physical_table_preserved": True,
            "rear_screen_preserved": True,
            "source_card_compositing": False,
            "wall_screen_quad": self.studio_panel_wall_screen_quad(),
            "quality_control": qc,
            # This private upload reference is the intended input for a later
            # scene-conditioned lipsync turn by the same authenticated owner.
            "scene_artifact_id": scene_reference["id"],
        }
        artifact = media_artifacts.write_artifact_bytes(
            self.artifact_root,
            namespace="studio-panel-shot",
            job_id=str(job["id"]),
            index=0,
            content=content,
            mime_type="image/png",
            source="b1_studio_panel_compositor",
            metadata={"type": "image", "runtime": "panel-cpu", "model": self.resolved_model_id(job), "operation": "studio-panel-shot", "studio_panel": studio_panel},
        )
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])

    def seated_scene_crop(self, width: int, height: int, face_region: dict[str, Any]) -> tuple[int, int, int, int]:
        x = float(face_region["x"])
        y = float(face_region["y"])
        face_width = float(face_region["width"])
        face_height = float(face_region["height"])
        # The B1-declared face rectangle is also the compositing boundary. A
        # wider crop can overwrite a neighbouring seated participant on a
        # six-person 512px panel. The crop is upscaled before MuseTalk, so it
        # does not need extra source pixels merely to satisfy detector size.
        target_width = max(32, int(round(face_width * width)))
        target_height = max(32, int(round(face_height * height * 1.20)))
        target_width = min(width, target_width + target_width % 2)
        target_height = min(height, target_height + target_height % 2)
        center_x = int(round((x + face_width / 2) * width))
        center_y = int(round((y + face_height / 2) * height))
        crop_x = max(0, min(width - target_width, center_x - target_width // 2))
        crop_y = max(0, min(height - target_height, center_y - target_height // 2))
        return crop_x, crop_y, target_width, target_height

    @staticmethod
    def normalized_face_bbox_in_crop(
        width: int,
        height: int,
        face_region: dict[str, Any],
        crop: tuple[int, int, int, int],
    ) -> dict[str, float]:
        """Transform a trusted scene face rectangle into crop coordinates."""
        crop_x, crop_y, crop_width, crop_height = crop
        face_x = float(face_region["x"]) * width
        face_y = float(face_region["y"]) * height
        face_right = (float(face_region["x"]) + float(face_region["width"])) * width
        face_bottom = (float(face_region["y"]) + float(face_region["height"])) * height
        x = max(0.0, min(1.0, (face_x - crop_x) / crop_width))
        y = max(0.0, min(1.0, (face_y - crop_y) / crop_height))
        right = max(x, min(1.0, (face_right - crop_x) / crop_width))
        bottom = max(y, min(1.0, (face_bottom - crop_y) / crop_height))
        if right <= x or bottom <= y:
            raise LipsyncInputRejectedError("scene_face_invalid_crop: declared face rectangle does not intersect the speaker crop")
        return {"x": x, "y": y, "width": right - x, "height": bottom - y}

    def native_scene_camera_plan(
        self,
        *,
        scene_width: int,
        scene_height: int,
        output_width: int,
        output_height: int,
        payload: dict[str, Any],
        speaker_region: dict[str, Any],
    ) -> dict[str, Any]:
        view = str(payload.get("camera_view") or "establishing_wide")
        thresholds = {"speaker_medium": 140, "speaker_close": 220, "panel_two_shot": 110, "reaction": 110}
        if view == "establishing_wide":
            return {
                "view": view,
                "x": 0,
                "y": 0,
                "width": scene_width,
                "height": scene_height,
                "face_region": dict(speaker_region["face_region"]),
                "framed_participant_ids": payload.get("framed_participant_ids") or [payload.get("speaker_participant_id")],
                "wall_screen": {"x": 0.225, "y": 0.275, "width": 0.55, "height": 0.39},
            }
        if view not in thresholds:
            raise CameraCoverageError(f"unsupported_camera_coverage: unsupported camera view {view}")
        if output_width < 1024 or output_height < 576 or scene_width < 1024 or scene_height < 576:
            raise CameraCoverageError(f"unsupported_camera_coverage: {view} requires a 1024x576 or higher scene master and output")
        regions = [item for item in payload.get("face_regions", []) if isinstance(item, dict) and isinstance(item.get("face_region"), dict)]
        framed = payload.get("framed_participant_ids") or [payload.get("speaker_participant_id")]
        selected = [item for item in regions if item.get("participant_id") in framed]
        if view == "panel_two_shot" and (not isinstance(framed, list) or len(framed) != 2 or len(selected) != 2):
            raise CameraCoverageError("unsupported_camera_coverage: panel_two_shot requires exactly two valid framed_participant_ids")
        if not selected:
            selected = [speaker_region]
        x0 = min(float(item["face_region"]["x"]) for item in selected) * scene_width
        x1 = max(float(item["face_region"]["x"]) + float(item["face_region"]["width"]) for item in selected) * scene_width
        y0 = min(float(item["face_region"]["y"]) for item in selected) * scene_height
        y1 = max(float(item["face_region"]["y"]) + float(item["face_region"]["height"]) for item in selected) * scene_height
        source_face_height = float(speaker_region["face_region"]["height"]) * scene_height
        # The panel's public face rectangle bounds hair and shoulders as well
        # as the mouth. Compose tighter than the nominal rectangle so visual
        # face detail, not merely the rectangle arithmetic, satisfies the
        # requested editorial coverage.
        target_height = {"speaker_medium": 0.30, "speaker_close": 0.20, "panel_two_shot": 0.40, "reaction": 0.36}[view] * scene_height
        target_height = min(target_height, source_face_height * output_height / thresholds[view])
        target_height = max(64.0, target_height)
        target_width = target_height * output_width / output_height
        required_width = (x1 - x0) + scene_width * 0.08
        if target_width < required_width:
            target_width = required_width
            target_height = target_width * output_height / output_width
        if target_width > scene_width or target_height > scene_height:
            raise CameraCoverageError(f"unsupported_camera_coverage: {view} cannot frame the selected seats at the requested aspect ratio")
        center_x = (x0 + x1) / 2
        center_y = (y0 + y1) / 2
        crop_x = max(0, min(scene_width - int(round(target_width)), int(round(center_x - target_width / 2))))
        crop_y = max(0, min(scene_height - int(round(target_height)), int(round(center_y - target_height / 2))))
        crop_width = max(2, int(round(target_width)) // 2 * 2)
        # Never round the detail-limited crop upward: doing so can turn an
        # exact threshold plan into 219px for a requested 220px close-up.
        crop_height = max(2, int(target_height) // 2 * 2)
        face = speaker_region["face_region"]
        transformed = {
            "x": (float(face["x"]) * scene_width - crop_x) / crop_width,
            "y": (float(face["y"]) * scene_height - crop_y) / crop_height,
            "width": float(face["width"]) * scene_width / crop_width,
            "height": float(face["height"]) * scene_height / crop_height,
        }
        face_pixels = int(round(transformed["height"] * output_height))
        if face_pixels < thresholds[view]:
            raise CameraCoverageError(f"unsupported_camera_coverage: {view} cannot meet the {thresholds[view]}px face-detail threshold")
        screen_x = (scene_width * 0.225 - crop_x) / crop_width
        screen_y = (scene_height * 0.275 - crop_y) / crop_height
        screen_width = scene_width * 0.55 / crop_width
        screen_height = scene_height * 0.39 / crop_height
        return {
            "view": view,
            "x": crop_x,
            "y": crop_y,
            "width": crop_width,
            "height": crop_height,
            "face_region": transformed,
            "framed_participant_ids": list(framed),
            "speaker_face_height_px": face_pixels,
            "wall_screen": {"x": screen_x, "y": screen_y, "width": screen_width, "height": screen_height},
        }

    async def render_seated_scene_lipsync(
        self,
        job: dict[str, Any],
        *,
        scene: bytes,
        scene_mime_type: str,
        audio: bytes,
        audio_sha256: str,
        timing_sha256: str,
        width: int,
        height: int,
        fps: int,
        duration_ms: int,
        payload: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        regions = payload.get("face_regions")
        speaker = payload.get("speaker_participant_id")
        speaker_region = next((item for item in regions if isinstance(item, dict) and item.get("participant_id") == speaker), None) if isinstance(regions, list) else None
        if not isinstance(speaker_region, dict) or not isinstance(speaker_region.get("face_region"), dict):
            raise LipsyncInputRejectedError("scene-conditioned lipsync requires the selected speaker face_region")
        face_region = speaker_region["face_region"]
        if float(face_region["width"]) * width < 24 or float(face_region["height"]) * height < 24:
            raise LipsyncInputRejectedError("scene_face_too_small: declared scene face_region is below 24 pixels at the requested output size")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed in the control-plane container")
        job_segment = media_artifacts.safe_artifact_segment(str(job["id"]), "job")
        temp_dir = media_artifacts.artifact_store_path(self.artifact_root, f"temporary/{job_segment}/seated-panel")
        temp_dir.mkdir(parents=True, exist_ok=True)
        scene_path = temp_dir / f"scene{media_artifacts.extension_for_mime_type(scene_mime_type)}"
        camera_path = temp_dir / "native-scene-camera.png"
        crop_path = temp_dir / "speaker-crop.png"
        patch_path = temp_dir / "speaker-lipsync.mp4"
        output_path = temp_dir / "seated-panel-lipsync.mp4"
        media_artifacts.write_regular_file_bytes(scene_path, scene)
        scene_image = self.studio_panel_image(scene, "scene_artifact_id")
        camera = self.native_scene_camera_plan(
            scene_width=scene_image.width,
            scene_height=scene_image.height,
            output_width=width,
            output_height=height,
            payload=payload,
            speaker_region=speaker_region,
        )
        await self.run_studio_panel_ffmpeg(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(scene_path), "-vf", f"crop={camera['width']}:{camera['height']}:{camera['x']}:{camera['y']},scale={width}:{height}:flags=lanczos", "-frames:v", "1", str(camera_path)],
            "native scene camera render failed",
        )
        face_region = camera["face_region"]
        crop_x, crop_y, crop_width, crop_height = self.seated_scene_crop(width, height, face_region)
        runtime_face_bbox = self.normalized_face_bbox_in_crop(
            width,
            height,
            face_region,
            (crop_x, crop_y, crop_width, crop_height),
        )
        runtime_scale = min(8, max(4, int((384 + min(crop_width, crop_height) - 1) / min(crop_width, crop_height))))
        runtime_width = min(768, max(256, crop_width * runtime_scale))
        runtime_height = min(768, max(256, crop_height * runtime_scale))
        runtime_width += runtime_width % 2
        runtime_height += runtime_height % 2
        try:
            await self.run_studio_panel_ffmpeg(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(camera_path), "-vf", f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},scale={runtime_width}:{runtime_height}:flags=lanczos", "-frames:v", "1", str(crop_path)],
                "scene speaker crop failed",
            )
            crop = media_artifacts.read_regular_file_bytes(crop_path)
            try:
                patch, metadata = await self.render_talking_head_lipsync_with_metrics(
                    job,
                    portrait=crop,
                    portrait_mime_type="image/png",
                    audio=audio,
                    audio_sha256=audio_sha256,
                    timing_sha256=timing_sha256,
                    width=runtime_width,
                    height=runtime_height,
                    fps=fps,
                    duration_ms=duration_ms,
                    payload={
                        **payload,
                        "_b1_lipsync_source_context": "scene_face_region",
                        "_b1_lipsync_face_bbox": runtime_face_bbox,
                    },
                )
            except LipsyncInputRejectedError as exc:
                message = str(exc)
                if "lipsync_face_not_detected" in message:
                    raise LipsyncInputRejectedError("scene_face_not_detected: MuseTalk could not track the declared face crop from the scene plate") from exc
                raise
            media_artifacts.write_regular_file_bytes(patch_path, patch)
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(camera_path)]
            filter_parts = ["[0:v]format=rgba[base]"]
            patch_input_index = 1
            wall_ref = payload.get("wall_screen_artifact_id")
            if wall_ref is not None:
                wall, wall_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, self.staged_input_reference(job, wall_ref, "wall_screen_artifact_id"))
                if wall_mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise LipsyncInputRejectedError("wall_screen_artifact_id must reference a PNG, JPEG, or WebP upload")
                wall_path = temp_dir / f"wall{media_artifacts.extension_for_mime_type(wall_mime_type)}"
                media_artifacts.write_regular_file_bytes(wall_path, wall)
                command.extend(["-loop", "1", "-i", str(wall_path)])
                wall_screen = camera["wall_screen"]
                screen_x = int(round(float(wall_screen["x"]) * width))
                screen_y = int(round(float(wall_screen["y"]) * height))
                screen_width = int(round(float(wall_screen["width"]) * width))
                screen_height = int(round(float(wall_screen["height"]) * height))
                if screen_width < 8 or screen_height < 8 or screen_x >= width or screen_y >= height or screen_x + screen_width <= 0 or screen_y + screen_height <= 0:
                    raise CameraCoverageError("unsupported_camera_coverage: the requested native scene camera does not include the physical rear wall screen")
                # Crop the physical screen to the camera frame. It remains
                # embedded in the shared scene rather than becoming a PIP.
                visible_x = max(0, screen_x)
                visible_y = max(0, screen_y)
                visible_right = min(width, screen_x + screen_width)
                visible_bottom = min(height, screen_y + screen_height)
                screen_width = visible_right - visible_x
                screen_height = visible_bottom - visible_y
                screen_x, screen_y = visible_x, visible_y
                filter_parts.extend([f"[1:v]scale={screen_width}:{screen_height}:force_original_aspect_ratio=decrease,pad={screen_width}:{screen_height}:(ow-iw)/2:(oh-ih)/2:color=black[wall]", f"[base][wall]overlay={screen_x}:{screen_y}[withwall]"])
                filter_parts[0] = filter_parts[0].replace("[base]", "[base]")
                base_label = "withwall"
                patch_input_index = 2
            else:
                base_label = "base"
            command.extend(["-i", str(patch_path)])
            filter_parts.append(f"[{patch_input_index}:v]scale={crop_width}:{crop_height}[speaker]")
            filter_parts.append(f"[{base_label}][speaker]overlay={crop_x}:{crop_y}:eof_action=pass[out]")
            command.extend(["-filter_complex", ";".join(filter_parts), "-map", "[out]", "-map", f"{patch_input_index}:a?", "-t", f"{duration_ms / 1000:.3f}", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(output_path)])
            await self.run_studio_panel_ffmpeg(command, "scene-conditioned lipsync compositor failed")
            content = media_artifacts.read_regular_file_bytes(output_path)
            if not content:
                raise RuntimeError("scene-conditioned lipsync compositor produced an empty MP4")
            metadata["studio_panel"] = {
                "actual_camera_view": camera["view"],
                "camera_composition": "native_scene_camera",
                "output_width": width,
                "output_height": height,
                "speaker_face_region_px": {
                    "x": int(round(float(face_region["x"]) * width)),
                    "y": int(round(float(face_region["y"]) * height)),
                    "width": int(round(float(face_region["width"]) * width)),
                    "height": int(round(float(face_region["height"]) * height)),
                },
                "framed_participant_ids": camera["framed_participant_ids"],
                "wall_screen_preserved": True,
                "source_card_compositing": False,
            }
            return content, metadata
        finally:
            for path in temp_dir.glob("*"):
                with suppress(FileNotFoundError):
                    path.unlink()
            with suppress(OSError):
                temp_dir.rmdir()

    def staged_input_reference(self, job: dict[str, Any], value: Any, field_name: str) -> dict[str, Any]:
        if isinstance(value, dict) and value.get("source") == "staged_upload":
            return value
        if isinstance(value, str) and value.startswith("upload_"):
            return media_artifacts.staged_input_reference_for_upload_id(
                self.artifact_root,
                owner_id=str(job.get("owner_id") or ""),
                upload_id=value,
            )
        raise ValueError(f"{field_name} must be a staged upload reference or upload_ id")

    async def voicebox_timing_by_generation_id(self, generation_id: str) -> dict[str, Any] | None:
        voicebox_url = self.runtime_urls.get("voicebox")
        if not voicebox_url or not generation_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                response = await client.get(f"{voicebox_url}/generate/timing/{generation_id}")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    async def resolved_talking_head_timing(self, timing: Any) -> dict[str, Any] | None:
        if timing is None:
            return None
        if not isinstance(timing, dict):
            raise ValueError("timing must be an object")
        generation_id = timing.get("generation_id")
        if isinstance(generation_id, str) and generation_id.strip():
            resolved = await self.voicebox_timing_by_generation_id(generation_id.strip())
            if resolved is not None:
                return resolved
        return dict(timing)

    def validate_talking_head_timing_binding(self, timing: dict[str, Any] | None, audio_sha256: str) -> None:
        if timing is None:
            return
        timing_audio_sha256 = timing.get("audio_sha256")
        if not isinstance(timing_audio_sha256, str) or not SHA256_HEX_RE.fullmatch(timing_audio_sha256):
            raise ValueError("timing.audio_sha256 must be supplied and must be a SHA-256 hex digest")
        if not hmac.compare_digest(timing_audio_sha256.lower(), audio_sha256.lower()):
            raise ValueError("timing.audio_sha256 does not match uploaded WAV SHA-256")

    async def render_talking_head_lipsync_smoke(
        self,
        *,
        job_id: str,
        portrait: bytes,
        portrait_mime_type: str,
        audio: bytes,
        width: int,
        height: int,
        fps: int,
        duration_ms: int,
    ) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed in the control-plane container")
        job_segment = media_artifacts.safe_artifact_segment(job_id, "job")
        temporary_dir = media_artifacts.artifact_store_path(self.artifact_root, f"temporary/{job_segment}")
        temporary_dir.mkdir(parents=True, exist_ok=True)
        portrait_path = media_artifacts.artifact_store_path(
            self.artifact_root,
            f"temporary/{job_segment}/portrait{media_artifacts.extension_for_mime_type(portrait_mime_type)}",
        )
        audio_path = media_artifacts.artifact_store_path(self.artifact_root, f"temporary/{job_segment}/dialogue.wav")
        output_path = media_artifacts.artifact_store_path(self.artifact_root, f"temporary/{job_segment}/talking-head-lipsync.mp4")
        media_artifacts.write_regular_file_bytes(portrait_path, portrait)
        media_artifacts.write_regular_file_bytes(audio_path, audio)
        frame_count = max(1, int(round(duration_ms * fps / 1000)))
        timeout_seconds = max(15.0, duration_ms / 1000 + 10.0)
        video_filter = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(portrait_path),
                "-i",
                str(audio_path),
                "-t",
                f"{duration_ms / 1000:.3f}",
                "-vf",
                video_filter,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "stillimage",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-shortest",
                "-frames:v",
                str(frame_count),
                "-movflags",
                "+faststart",
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                process.kill()
                with suppress(Exception):
                    await process.communicate()
                raise RuntimeError("talking-head lipsync render timed out") from exc
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip()[:500] or "ffmpeg failed"
                raise RuntimeError(f"talking-head lipsync render failed: {message}")
            return media_artifacts.read_regular_file_bytes(output_path)
        finally:
            for path in (portrait_path, audio_path, output_path):
                with suppress(FileNotFoundError):
                    path.unlink()
            with suppress(OSError):
                temporary_dir.rmdir()

    async def render_talking_head_lipsync_runtime(
        self,
        *,
        job: dict[str, Any],
        portrait: bytes,
        portrait_mime_type: str,
        audio: bytes,
        audio_sha256: str,
        timing_sha256: str,
        width: int,
        height: int,
        fps: int,
        duration_ms: int,
        payload: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        lipsync_url = self.lipsync_url_for_job(job)
        if not lipsync_url:
            raise RuntimeError("lipsync runtime URL is not configured")
        request_payload: dict[str, Any] = {
            "portrait_b64": base64.b64encode(portrait).decode("ascii"),
            "portrait_mime_type": portrait_mime_type,
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "audio_sha256": audio_sha256,
            "timing_sha256": timing_sha256,
            "width": width,
            "height": height,
            "fps": fps,
            "duration_ms": duration_ms,
        }
        source_context = payload.get("_b1_lipsync_source_context")
        if source_context == "scene_face_region":
            request_payload["source_context"] = source_context
            face_bbox = payload.get("_b1_lipsync_face_bbox")
            if isinstance(face_bbox, dict):
                request_payload["face_bbox"] = face_bbox
        options = payload.get("lipsync_options")
        if isinstance(options, dict):
            for key in (
                "batch_size",
                "extra_margin",
                "audio_padding_length_left",
                "audio_padding_length_right",
                "parsing_mode",
                "left_cheek_width",
                "right_cheek_width",
                "use_float16",
            ):
                if key in options:
                    request_payload[key] = options[key]
        performance_plan = payload.get("performance_plan")
        if performance_plan is not None:
            if not isinstance(performance_plan, dict):
                raise LipsyncInputRejectedError("lipsync input rejected: performance_plan must be an object")
            request_payload["performance_plan"] = performance_plan
        headers = {"Accept": "application/json", **self.lipsync_runtime_headers(job)}
        client_kwargs = self.media_runtime_client_kwargs(job) or {"trust_env": False}
        async with httpx.AsyncClient(timeout=1800.0, **client_kwargs) as client:
            response = await client.post(f"{lipsync_url}/v1/talking-head/lipsync", json=request_payload, headers=headers)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"message": response.text[:500]}
            message = body.get("detail") if isinstance(body, dict) else body
            if response.status_code < 500:
                if isinstance(message, dict):
                    code = message.get("code")
                    detail = message.get("message")
                    if isinstance(code, str) and isinstance(detail, str):
                        raise LipsyncInputRejectedError(f"{code}: {detail}")
                raise LipsyncInputRejectedError(f"lipsync input rejected: {message}")
            if isinstance(message, dict):
                code = message.get("code")
                detail = message.get("message")
                if isinstance(code, str) and code.startswith("lipsync_") and isinstance(detail, str):
                    raise LipsyncRuntimeFailure(code, detail)
            raise RuntimeError(f"lipsync runtime returned HTTP {response.status_code}: {message}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("lipsync runtime returned non-JSON response") from exc
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise RuntimeError("lipsync runtime returned an invalid response")
        try:
            content = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
        except binascii.Error as exc:
            raise RuntimeError("lipsync runtime returned invalid base64 video") from exc
        if not content:
            raise RuntimeError("lipsync runtime returned an empty video")
        metadata = body.get("metadata")
        return content, metadata if isinstance(metadata, dict) else {}

    async def render_talking_head_lipsync_with_metrics(self, job: dict[str, Any], **kwargs: Any) -> tuple[bytes, dict[str, Any]]:
        """Sample the ephemeral MuseTalk child while its CUDA allocations exist."""
        finished = asyncio.Event()

        async def sample_resources() -> None:
            while not finished.is_set():
                with suppress(Exception):
                    await self.record_peak_resources(job)
                try:
                    await asyncio.wait_for(finished.wait(), timeout=1)
                except TimeoutError:
                    continue

        sampler = asyncio.create_task(sample_resources())
        try:
            return await self.render_talking_head_lipsync_runtime(job=job, **kwargs)
        finally:
            finished.set()
            with suppress(Exception):
                await sampler

    async def record_lipsync_completion_audit(self, job: dict[str, Any]) -> None:
        insert_audit_event = getattr(database, "insert_audit_event", None)
        if insert_audit_event is None or not self.has_durable_job_record(job):
            return
        try:
            post_cleanup_metrics = await self.runtime_agent_get("/v1/metrics")
            await insert_audit_event(
                {
                    "event_type": "job.lipsync_completed",
                    "actor_id": "system:scheduler",
                    "actor_role": "service",
                    "target_type": "job",
                    "target_id": str(job["id"]),
                    "summary": "MuseTalk job completed with GPU cleanup measurement",
                    "metadata": {
                        "peak_vram_mib": job.get("peak_vram_mib"),
                        "post_cleanup_gpu_used_mib": self.gpu_memory_used_mib(post_cleanup_metrics),
                    },
                    "correlation_id": str(job.get("correlation_id") or job["id"]),
                }
            )
        except Exception:
            return

    async def record_lipsync_cuda_retry(
        self,
        job: dict[str, Any],
        *,
        before_metrics: dict[str, Any] | None,
        after_metrics: dict[str, Any] | None,
        cleanup: dict[str, Any],
    ) -> None:
        """Persist only resource figures needed to diagnose a bounded retry."""
        insert_audit_event = getattr(database, "insert_audit_event", None)
        if insert_audit_event is None or not self.has_durable_job_record(job):
            return
        try:
            await insert_audit_event(
                {
                    "event_type": "job.lipsync_cuda_retry",
                    "actor_id": "system:scheduler",
                    "actor_role": "service",
                    "target_type": "job",
                    "target_id": str(job["id"]),
                    "summary": "Retrying MuseTalk allocation after CUDA memory cleanup",
                    "metadata": {
                        "attempt": 2,
                        "gpu_used_mib_before": self.gpu_memory_used_mib(before_metrics),
                        "gpu_used_mib_after": self.gpu_memory_used_mib(after_metrics),
                        "cleanup": cleanup,
                    },
                    "correlation_id": str(job.get("correlation_id") or job["id"]),
                }
            )
        except Exception:
            # Auditing must not prevent recovery of a user-owned media job.
            return

    async def recover_lipsync_after_cuda_oom(self, job: dict[str, Any]) -> None:
        """Clear B1-managed GPU work once before retrying an ephemeral MuseTalk worker.

        The GPU lease is already owned by ``run_once``.  MuseTalk itself is a
        short-lived subprocess, but its failed CUDA context can take a moment
        to disappear.  This is deliberately a single retry: a persistent OOM
        remains an actionable capacity failure instead of an unbounded loop.
        """
        before_metrics = await self.runtime_agent_get("/v1/metrics")
        await self.record_peak_resources_from_metrics(job, before_metrics)
        await database.update_job(
            job["id"],
            state=JobState.RUNNING.value,
            stage="lipsync_recovering_gpu",
            progress=83,
            failure_category=None,
            failure_message=None,
        )
        cleanup: dict[str, Any] = {}
        target_state = (await self.current_runtime_state_by_name()).get("lipsync")
        try:
            result, details = await self.graceful_or_forced_unload_runtime(
                "lipsync",
                "lipsync",
                job,
                target_state,
                reason="MuseTalk CUDA allocation retry",
            )
            cleanup["lipsync"] = {"result": self.compact_hook_result(result), **details}
        except Exception as exc:
            cleanup["lipsync"] = {"error": exc.__class__.__name__}
        for result in await self.unload_other_gpu_runtimes(job):
            runtime = str((result or {}).get("runtime") or "unknown")
            cleanup[runtime] = self.compact_hook_result(result)
        await asyncio.sleep(1)
        after_metrics = await self.runtime_agent_get("/v1/metrics")
        await self.record_peak_resources_from_metrics(job, after_metrics)
        await self.record_lipsync_cuda_retry(
            job,
            before_metrics=before_metrics,
            after_metrics=after_metrics,
            cleanup=cleanup,
        )
        await database.update_job(
            job["id"],
            state=JobState.RUNNING.value,
            stage="lipsync_retrying_allocation",
            progress=84,
        )

    async def run_talking_head_lipsync_job(self, job: dict[str, Any]) -> None:
        payload = dict(self.request_input(job))
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="lipsync_validating_inputs", progress=72)
        audio_ref = self.staged_input_reference(job, payload.get("audio_artifact_id"), "audio_artifact_id")
        audio, audio_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, audio_ref)
        if audio_mime_type != "audio/wav":
            raise ValueError("audio_artifact_id must reference a WAV upload")
        scene_mode = payload.get("scene_artifact_id") is not None
        portrait: bytes | None = None
        portrait_mime_type: str | None = None
        if not scene_mode:
            portrait_ref = self.staged_input_reference(job, payload.get("portrait_artifact_id"), "portrait_artifact_id")
            portrait, portrait_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, portrait_ref)
            if not portrait_mime_type.startswith("image/"):
                raise ValueError("portrait_artifact_id must reference an image upload")
        scene: bytes | None = None
        scene_mime_type: str | None = None
        if scene_mode:
            scene_ref = self.staged_input_reference(job, payload.get("scene_artifact_id"), "scene_artifact_id")
            scene, scene_mime_type, _ = media_artifacts.read_staged_input_bytes(self.artifact_root, scene_ref)
            if scene_mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError("scene_artifact_id must reference a PNG, JPEG, or WebP upload")
            provenance = await database.get_completed_studio_panel_reference(str(job.get("owner_id") or ""), str(scene_ref.get("id") or ""))
            metadata = provenance.get("metadata") if isinstance(provenance, dict) else None
            quality = metadata.get("quality_control") if isinstance(metadata, dict) else None
            occupancy = metadata.get("seat_occupancy") if isinstance(metadata, dict) else None
            if (
                not isinstance(metadata, dict)
                or not isinstance(quality, dict)
                or quality.get("status") != "passed"
                or metadata.get("source_card_compositing") is not False
                or not isinstance(occupancy, list)
                or not occupancy
                or any(not isinstance(item, dict) or item.get("occupied") is not True or item.get("seated_pose_detected") is not True for item in occupancy)
            ):
                raise LipsyncInputRejectedError("scene_panel_qc_required: scene_artifact_id is not a completed semantically QC-passed seated studio panel")
        actual_audio_sha256 = hashlib.sha256(audio).hexdigest()
        requested_audio_sha256 = payload.get("audio_sha256")
        if not isinstance(requested_audio_sha256, str) or not SHA256_HEX_RE.fullmatch(requested_audio_sha256):
            raise ValueError("audio_sha256 must be a SHA-256 hex digest")
        if not hmac.compare_digest(requested_audio_sha256.lower(), actual_audio_sha256):
            raise ValueError("audio_sha256 does not match uploaded WAV")
        timing = await self.resolved_talking_head_timing(payload.get("timing"))
        self.validate_talking_head_timing_binding(timing, actual_audio_sha256)
        width = request_int(payload, "width", 1280, 64, 1920)
        height = request_int(payload, "height", 720, 64, 1080)
        fps = request_int(payload, "fps", 24, 1, 60)
        requested_duration_ms = request_int(payload, "duration_ms", 1000, 250, 60000)
        audio_duration_ms = wav_duration_ms(audio)
        if abs(audio_duration_ms - requested_duration_ms) > 250:
            raise ValueError("duration_ms differs from uploaded WAV duration by more than 250 ms")
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="lipsync_rendering", progress=82)
        runtime_metadata: dict[str, Any] = {}
        source = "b1_lipsync_runtime"

        async def render() -> tuple[bytes, dict[str, Any]]:
            if scene_mode:
                assert scene is not None and scene_mime_type is not None
                return await self.render_seated_scene_lipsync(
                    job,
                    scene=scene,
                    scene_mime_type=scene_mime_type,
                    audio=audio,
                    audio_sha256=actual_audio_sha256,
                    timing_sha256=canonical_json_sha256(timing),
                    width=width,
                    height=height,
                    fps=fps,
                    duration_ms=audio_duration_ms,
                    payload=payload,
                )
            return await self.render_talking_head_lipsync_with_metrics(
                job,
                portrait=portrait or b"",
                portrait_mime_type=portrait_mime_type or "image/png",
                audio=audio,
                audio_sha256=actual_audio_sha256,
                timing_sha256=canonical_json_sha256(timing),
                width=width,
                height=height,
                fps=fps,
                duration_ms=audio_duration_ms,
                payload=payload,
            )

        try:
            try:
                content, runtime_metadata = await render()
            except LipsyncRuntimeFailure as exc:
                if exc.code != "lipsync_cuda_out_of_memory":
                    raise
                await self.recover_lipsync_after_cuda_oom(job)
                content, runtime_metadata = await render()
        except Exception:
            # A static fallback would silently discard requested character
            # performance and is therefore never valid for this operation.
            if scene_mode or payload.get("performance_plan") is not None or not self.talking_head_lipsync_fallback_renderer:
                raise
            source = "b1_talking_head_lipsync_smoke_renderer"
            runtime_metadata = {"backend": "b1-smoke-ffmpeg-static-audio-bound", "fallback_renderer": True}
            content = await self.render_talking_head_lipsync_smoke(
                job_id=str(job["id"]),
                portrait=portrait,
                portrait_mime_type=portrait_mime_type,
                audio=audio,
                width=width,
                height=height,
                fps=fps,
                duration_ms=audio_duration_ms,
            )
        lip_sync = {
            "mode": "audio_driven_seated_panel" if scene_mode else "audio_driven",
            "backend": str(runtime_metadata.get("backend") or "b1-musetalk-v1.5"),
            "audio_sha256": actual_audio_sha256,
            "timing_sha256": canonical_json_sha256(timing),
            "measured_offset_ms": 0,
            "duration_ms": audio_duration_ms,
            "fps": fps,
            **{key: value for key, value in runtime_metadata.items() if key in PUBLIC_LIPSYNC_RUNTIME_METADATA_KEYS},
        }
        performance = runtime_metadata.get("performance")
        if payload.get("performance_plan") is not None and not isinstance(performance, dict):
            raise RuntimeError("lipsync runtime did not return performance evidence for the requested performance_plan")
        artifact_metadata: dict[str, Any] = {
            "type": "video",
            "runtime": str(job.get("runtime") or "comfyui"),
            "model": self.resolved_model_id(job),
            "operation": "talking-head-lipsync",
            "lip_sync": lip_sync,
        }
        if isinstance(performance, dict):
            artifact_metadata["performance"] = performance
        studio_panel = runtime_metadata.get("studio_panel")
        if isinstance(studio_panel, dict):
            artifact_metadata["studio_panel"] = studio_panel
        artifact = media_artifacts.write_artifact_bytes(
            self.artifact_root,
            namespace="talking-head-lipsync",
            job_id=str(job["id"]),
            index=0,
            content=content,
            mime_type="video/mp4",
            source=source,
            metadata=artifact_metadata,
        )
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
        await self.record_lipsync_completion_audit(job)

    async def workflow_manifest_for_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        input_payload = self.request_input(job)
        workflow_id = input_payload.get("workflow_id")
        workflow_version = input_payload.get("workflow_version")
        if not isinstance(workflow_id, str) and not isinstance(workflow_version, str):
            return None
        if not isinstance(workflow_id, str) or not workflow_id or not isinstance(workflow_version, str) or not workflow_version:
            raise ValueError("workflow-backed jobs require workflow_id and workflow_version")
        row = await database.get_workflow(workflow_id, workflow_version)
        if row is None:
            raise ValueError(f"published workflow {workflow_id}@{workflow_version} is no longer available")
        manifest = row.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError(f"published workflow {workflow_id}@{workflow_version} has an invalid manifest")
        return manifest

    async def upload_staged_comfyui_inputs(self, job: dict[str, Any], manifest: dict[str, Any], input_payload: dict[str, Any]) -> dict[str, Any]:
        """Upload workflow-declared staged media and return render parameters.

        ComfyUI deliberately accepts only names in its own input directory. The
        B1 staged reference is therefore read through the artifact policy and
        sent to native ``/upload/image``; only the filename returned by ComfyUI
        is made available to the workflow template.
        """
        payload = dict(input_payload)
        supplied_parameters = input_payload.get("parameters")
        if not isinstance(supplied_parameters, dict):
            raise ValueError("workflow-backed ComfyUI jobs require input.parameters")
        parameters = dict(supplied_parameters)
        required_uploads = comfyui_native.workflow_comfyui_staged_upload_specs(manifest)
        if not required_uploads:
            return payload
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        for parameter_name, expected_media_type in required_uploads.items():
            reference = parameters.get(parameter_name)
            if not isinstance(reference, dict) or reference.get("source") != "staged_upload":
                raise ValueError(f"workflow input {parameter_name} must be a staged {expected_media_type} upload")
            try:
                content, detected_mime_type, filename = media_artifacts.read_staged_input_bytes(self.artifact_root, reference)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ValueError(f"workflow input {parameter_name} is unavailable") from exc
            if not detected_mime_type.startswith(f"{expected_media_type}/"):
                raise ValueError(f"workflow input {parameter_name} is not a supported {expected_media_type}")
            async with httpx.AsyncClient(timeout=60.0, **self.media_runtime_client_kwargs(job)) as client:
                response = await client.post(
                    f"{comfyui_url}/upload/image",
                    files={"image": (filename, content, detected_mime_type)},
                    data={"overwrite": "false"},
                    headers=self.media_runtime_headers(job),
                )
            if response.status_code >= 400:
                raise RuntimeError(f"ComfyUI {expected_media_type} upload returned HTTP {response.status_code}")
            try:
                uploaded = response.json()
            except ValueError as exc:
                raise RuntimeError(f"ComfyUI {expected_media_type} upload returned non-JSON response") from exc
            uploaded_name = uploaded.get("name") if isinstance(uploaded, dict) else None
            subfolder = uploaded.get("subfolder") if isinstance(uploaded, dict) else ""
            if not isinstance(uploaded_name, str) or not uploaded_name or not isinstance(subfolder, str):
                raise RuntimeError(f"ComfyUI {expected_media_type} upload returned an invalid filename")
            parameters[f"{parameter_name}_filename"] = f"{subfolder}/{uploaded_name}".lstrip("/") if subfolder else uploaded_name
        payload["parameters"] = parameters
        return payload

    async def comfyui_prompt_payload_for_job(self, job: dict[str, Any]) -> dict[str, Any]:
        input_payload = self.request_input(job)
        extra_data = {
            "job_id": str(job["id"]),
            "model_alias": str(job.get("model_alias") or ""),
            "resolved_model_version": str(job.get("resolved_model_version") or ""),
        }
        direct = comfyui_native.direct_comfyui_prompt_payload(input_payload, client_id=str(job["id"]), extra_data=extra_data)
        if direct is not None:
            return direct
        manifest = await self.workflow_manifest_for_job(job)
        if manifest is not None:
            parameters = await self.upload_staged_comfyui_inputs(job, manifest, input_payload)
            return comfyui_native.workflow_comfyui_prompt_payload(manifest, parameters, client_id=str(job["id"]), extra_data=extra_data)
        raise ValueError("ComfyUI media jobs require input.comfyui_prompt, input.workflow_json, or a published workflow with workflow_json")

    async def submit_comfyui_prompt(self, job: dict[str, Any], payload: dict[str, Any]) -> str:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        async with httpx.AsyncClient(timeout=30.0, **self.media_runtime_client_kwargs(job)) as client:
            response = await client.post(f"{comfyui_url}/prompt", json=payload, headers=self.media_runtime_headers(job))
        if response.status_code >= 400:
            raise RuntimeError(f"ComfyUI /prompt returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("ComfyUI /prompt returned non-JSON response") from exc
        prompt_id = body.get("prompt_id") if isinstance(body, dict) else None
        if not isinstance(prompt_id, str) or not prompt_id:
            raise RuntimeError("ComfyUI /prompt response did not include prompt_id")
        return prompt_id

    async def fetch_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        return await comfyui_native.fetch_comfyui_history(
            comfyui_url,
            prompt_id,
            headers=self.media_runtime_headers(job),
            client_kwargs=self.media_runtime_client_kwargs(job),
        )

    async def interrupt_comfyui(self, job: dict[str, Any]) -> None:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0, **self.media_runtime_client_kwargs(job)) as client:
                await client.post(f"{comfyui_url}/interrupt", headers=self.media_runtime_headers(job))
        except httpx.HTTPError:
            return

    async def wait_for_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        deadline = monotonic() + self.comfyui_completion_timeout_seconds
        while monotonic() < deadline:
            if await self.cancel_if_requested(job["id"]):
                await self.recover_cancelled_runtime_execution(job)
                return None
            history = await self.fetch_comfyui_history(job, prompt_id)
            if history is not None:
                return history
            await database.update_job(job["id"], state=JobState.RUNNING.value, stage="comfyui_waiting_history", progress=80)
            await asyncio.sleep(self.comfyui_poll_seconds)
        return None

    async def ingest_comfyui_artifacts(self, job: dict[str, Any], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        comfyui_url = self.comfyui_url_for_job(job)
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        return await comfyui_native.ingest_comfyui_artifacts(
            artifacts,
            self.artifact_root,
            comfyui_url,
            headers=self.media_runtime_headers(job),
            client_kwargs=self.media_runtime_client_kwargs(job),
        )

    async def run_comfyui_job(self, job: dict[str, Any]) -> None:
        payload = await self.comfyui_prompt_payload_for_job(job)
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="comfyui_submitting", progress=72)
        cancelled, prompt_id = await self.await_cancellable_runtime_call(job, self.submit_comfyui_prompt(job, payload))
        if cancelled:
            return
        await database.update_job(
            job["id"],
            state=JobState.RUNNING.value,
            stage="comfyui_native_running",
            progress=75,
            native_prompt_id=prompt_id,
        )
        history = await self.wait_for_comfyui_history(job, prompt_id)
        if history is None:
            current = await database.get_job(job["id"])
            if current and current["state"] == JobState.CANCELLED.value:
                return
            await database.update_job(
                job["id"],
                state=JobState.RECOVERY_REQUIRED.value,
                stage="comfyui_history_timeout",
                progress=85,
                failure_category="comfyui_history_timeout",
                failure_message=f"ComfyUI prompt {prompt_id} did not reach history before timeout",
            )
            return
        if await self.cancel_if_requested(job["id"]):
            return
        current = await database.get_job(job["id"]) or job
        artifacts = comfyui_native.merge_job_artifacts(
            current.get("artifacts") or [],
            comfyui_native.comfyui_artifacts_from_history(prompt_id, history),
        )
        if not artifacts:
            await database.update_job(
                job["id"],
                state=JobState.RECOVERY_REQUIRED.value,
                stage="comfyui_no_media_artifacts",
                progress=90,
                artifacts=[],
                failure_category="comfyui_no_media_artifacts",
                failure_message=f"ComfyUI prompt {prompt_id} completed without image, video, GIF, or audio outputs",
            )
            return
        artifacts = await self.ingest_comfyui_artifacts(job, artifacts)
        failed_ingests = [artifact for artifact in artifacts if artifact.get("ingest_status") == "failed"]
        if failed_ingests:
            await database.update_job(
                job["id"],
                state=JobState.RECOVERY_REQUIRED.value,
                stage="artifact_ingest_failed",
                progress=90,
                artifacts=artifacts,
                failure_category="artifact_ingest_failed",
                failure_message=f"{len(failed_ingests)} ComfyUI artifact(s) could not be stored",
            )
            return
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="comfyui_history_recorded", progress=90, artifacts=artifacts)
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=artifacts)

    def resolved_model_id(self, job: dict[str, Any]) -> str:
        resolved = str(job.get("resolved_model_version") or "")
        if "@" in resolved:
            return resolved.split("@", 1)[0]
        return str(job.get("model_alias") or resolved)

    def localai_media_endpoint_for_job(self, job: dict[str, Any]) -> str | None:
        modality = str(job.get("modality") or "")
        operation = str(job.get("operation") or "")
        if modality == "image" and operation in LOCALAI_IMAGE_GENERATION_OPERATIONS:
            return "/v1/images/generations"
        if modality == "image" and operation in LOCALAI_IMAGE_EDIT_OPERATIONS:
            return "/v1/images/edits"
        if modality == "video" and operation in LOCALAI_VIDEO_GENERATION_OPERATIONS:
            return "/v1/videos/generations"
        if modality == "video" and operation in LOCALAI_VIDEO_IMAGE_OPERATIONS:
            return "/v1/videos/image-to-video"
        return None

    def apply_runtime_parameter_mappings(self, payload: dict[str, Any], workflow_manifest: dict[str, Any] | None, runtime: str) -> dict[str, Any]:
        if not workflow_manifest:
            return payload
        mappings = workflow_manifest.get("runtime_parameter_mappings")
        if not isinstance(mappings, list):
            return payload
        mapped = dict(payload)
        for item in mappings:
            if not isinstance(item, dict) or item.get("runtime") != runtime:
                continue
            parameter = item.get("parameter")
            target = item.get("target")
            if not isinstance(parameter, str) or not isinstance(target, str) or parameter not in mapped:
                continue
            mapped.setdefault(target, mapped[parameter])
            if not item.get("keep_source", False) and parameter != target:
                mapped.pop(parameter, None)
        return mapped

    async def localai_media_payload_for_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(self.request_input(job))
        parameters = payload.pop("parameters", None)
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                payload.setdefault(key, value)
        payload = self.apply_runtime_parameter_mappings(payload, await self.workflow_manifest_for_job(job), "localai")
        for key in {
            "runtime_policy",
            "workflow_id",
            "workflow_version",
            "comfyui_payload",
            "comfyui_prompt",
            "native_prompt",
            "workflow_json",
        }:
            payload.pop(key, None)
        payload["model"] = self.resolved_model_id(job)
        return payload

    async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        localai_url = self.runtime_urls.get("localai")
        if not localai_url:
            raise RuntimeError("LocalAI runtime URL is not configured")
        async with httpx.AsyncClient(timeout=1800.0) as client:
            response = await client.post(f"{localai_url}/{endpoint.lstrip('/')}", json=payload, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"LocalAI {endpoint} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"LocalAI {endpoint} returned non-JSON response") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"LocalAI {endpoint} returned an invalid JSON shape")
        return body

    def localai_file_input_from_value(self, field_name: str, value: Any) -> tuple[str, bytes, str] | None:
        if isinstance(value, dict) and value.get("source") == "staged_upload":
            content, mime_type, filename = media_artifacts.read_staged_input_bytes(self.artifact_root, value)
            if not mime_type.startswith("image/"):
                raise ValueError(f"LocalAI image edit field {field_name} must be an image upload")
            return filename, content, mime_type
        if isinstance(value, str):
            decoded = self.decode_data_url(value)
            if decoded is not None:
                content, mime_type = decoded
                if not mime_type.startswith("image/"):
                    raise ValueError(f"LocalAI image edit field {field_name} must use an image data URL")
                return f"{field_name}{media_artifacts.extension_for_mime_type(mime_type)}", content, mime_type
        return None

    def localai_multipart_payload_for_job(self, payload: dict[str, Any]) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]:
        data: dict[str, str] = {}
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for key, value in payload.items():
            if key in {"image", "mask"}:
                values = value if isinstance(value, list) else [value]
                for item in values:
                    file_input = self.localai_file_input_from_value(key, item)
                    if file_input is None:
                        continue
                    filename, content, mime_type = file_input
                    files.append((key, (filename, content, mime_type)))
                continue
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                data[key] = json.dumps(value, sort_keys=True)
            else:
                data[key] = str(value)
        if not any(name == "image" for name, _ in files):
            raise ValueError("LocalAI multipart media requests require image as a staged upload or image data URL")
        return data, files

    async def post_localai_media_multipart(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        localai_url = self.runtime_urls.get("localai")
        if not localai_url:
            raise RuntimeError("LocalAI runtime URL is not configured")
        data, files = self.localai_multipart_payload_for_job(payload)
        async with httpx.AsyncClient(timeout=1800.0) as client:
            response = await client.post(f"{localai_url}/{endpoint.lstrip('/')}", data=data, files=files, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"LocalAI {endpoint} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"LocalAI {endpoint} returned non-JSON response") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"LocalAI {endpoint} returned an invalid JSON shape")
        return body

    def decode_data_url(self, value: str) -> tuple[bytes, str] | None:
        if not value.startswith("data:") or "," not in value:
            return None
        header, encoded = value[5:].split(",", 1)
        parts = header.split(";")
        mime_type = parts[0] if parts and "/" in parts[0] else "application/octet-stream"
        if "base64" in {part.lower() for part in parts[1:]}:
            try:
                return base64.b64decode(encoded.encode("ascii"), validate=True), mime_type
            except (UnicodeEncodeError, binascii.Error):
                return None
        return encoded.encode("utf-8"), mime_type

    async def fetch_localai_artifact_url(self, url: str, default_mime_type: str = "application/octet-stream") -> tuple[bytes, str]:
        localai_url = self.runtime_urls.get("localai")
        if not localai_url:
            raise RuntimeError("LocalAI runtime URL is not configured")
        base = urlsplit(localai_url)
        target_url = urljoin(f"{localai_url.rstrip('/')}/", url)
        target = urlsplit(target_url)
        if target.scheme not in {"http", "https"} or (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise RuntimeError("LocalAI media URL is not same-origin with the configured runtime")
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(target_url)
        if response.status_code >= 400:
            raise RuntimeError(f"LocalAI artifact URL returned HTTP {response.status_code}")
        content_type = response.headers.get("content-type", default_mime_type).split(";")[0]
        if content_type == "application/octet-stream":
            content_type = default_mime_type
        return response.content, content_type

    async def localai_media_bytes_from_item(self, item: dict[str, Any], default_mime_type: str) -> tuple[bytes, str] | None:
        mime_type = item.get("mime_type") if isinstance(item.get("mime_type"), str) else default_mime_type
        b64_json = next((item.get(key) for key in ("b64_json", "b64", "base64") if isinstance(item.get(key), str) and item.get(key)), None)
        if isinstance(b64_json, str) and b64_json:
            decoded = self.decode_data_url(b64_json)
            if decoded is not None:
                return decoded
            try:
                return base64.b64decode(b64_json.encode("ascii"), validate=True), mime_type
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise RuntimeError("LocalAI returned invalid base64 media") from exc
        url = item.get("url")
        if isinstance(url, str) and url:
            decoded = self.decode_data_url(url)
            if decoded is not None:
                return decoded
            return await self.fetch_localai_artifact_url(url, default_mime_type)
        return None

    def localai_default_mime_type_for_job(self, job: dict[str, Any]) -> str:
        modality = str(job.get("modality") or "")
        if modality == "video":
            return "video/mp4"
        if modality == "audio":
            return "audio/wav"
        return "image/png"

    async def localai_artifacts_from_response(self, job: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
        data = body.get("data")
        if not isinstance(data, list):
            return []
        artifacts: list[dict[str, Any]] = []
        default_mime_type = self.localai_default_mime_type_for_job(job)
        for item in data:
            if not isinstance(item, dict):
                continue
            media = await self.localai_media_bytes_from_item(item, default_mime_type)
            if media is None:
                continue
            content, mime_type = media
            artifacts.append(
                media_artifacts.write_artifact_bytes(
                    self.artifact_root,
                    namespace="localai",
                    job_id=str(job["id"]),
                    index=len(artifacts),
                    content=content,
                    mime_type=mime_type,
                    source="localai_openai",
                    metadata={
                        "runtime": "localai",
                        "model": self.resolved_model_id(job),
                        "operation": job.get("operation"),
                    },
                )
            )
        return artifacts

    async def run_localai_media_job(self, job: dict[str, Any]) -> bool:
        endpoint = self.localai_media_endpoint_for_job(job)
        if endpoint is None:
            return False
        payload = await self.localai_media_payload_for_job(job)
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="localai_submitting", progress=72)
        if endpoint in {"/v1/images/edits", "/v1/videos/image-to-video"}:
            cancelled, body = await self.await_cancellable_runtime_call(job, self.post_localai_media_multipart(endpoint, payload))
        else:
            cancelled, body = await self.await_cancellable_runtime_call(job, self.post_localai_media_json(endpoint, payload))
        if cancelled:
            return True
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="localai_response_received", progress=85)
        if await self.cancel_if_requested(job["id"]):
            return True
        artifacts = await self.localai_artifacts_from_response(job, body)
        if not artifacts:
            await database.update_job(
                job["id"],
                state=JobState.RECOVERY_REQUIRED.value,
                stage="localai_no_media_artifacts",
                progress=90,
                failure_category="localai_no_media_artifacts",
                failure_message="LocalAI returned no b64_json, data URL, or same-origin media URL artifacts",
            )
            return True
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=artifacts)
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=artifacts)
        return True

    async def voicebox_payload_for_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(self.request_input(job))
        parameters = payload.pop("parameters", None)
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                payload.setdefault(key, value)
        payload = self.apply_runtime_parameter_mappings(payload, await self.workflow_manifest_for_job(job), "voicebox")
        for key in {"workflow_id", "workflow_version", "runtime_policy"}:
            payload.pop(key, None)
        payload["model"] = self.resolved_model_id(job)
        profile = await self.voice_profile_for_job(job, payload)
        if profile is not None:
            payload = voice_profile_policy.apply_voice_profile_to_payload(payload, profile)
        else:
            payload = {key: value for key, value in payload.items() if key not in voice_profile_policy.VOICE_PROFILE_REQUEST_FIELDS}
        return payload

    async def voice_profile_for_job(self, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            profile_id = voice_profile_policy.requested_voice_profile_id(payload)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not profile_id:
            return None
        get_profile = getattr(database, "get_voice_profile", None)
        if get_profile is None:
            raise ValueError("voice profile lookup is unavailable")
        profile = await get_profile(profile_id)
        if profile is None:
            raise ValueError("voice profile not found")
        if profile.get("status") != "active":
            raise ValueError("voice profile is not active")
        runtime = str(job.get("runtime") or "")
        if profile.get("runtime") != runtime:
            raise ValueError(f"voice profile runtime {profile.get('runtime')} does not match job runtime {runtime}")
        model_alias = str(job.get("model_alias") or "")
        if profile.get("model_alias") != model_alias:
            raise ValueError(f"voice profile model alias {profile.get('model_alias')} does not match job model {model_alias}")
        return profile

    async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        voicebox_url = self.runtime_urls.get("voicebox")
        if not voicebox_url:
            raise RuntimeError("Voicebox runtime URL is not configured")
        async with httpx.AsyncClient(timeout=1800.0) as client:
            response = await client.post(f"{voicebox_url}/v1/audio/speech", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"Voicebox /v1/audio/speech returned HTTP {response.status_code}")
        return response.content, response.headers.get("content-type", "audio/wav").split(";")[0]

    async def run_voicebox_job(self, job: dict[str, Any]) -> bool:
        modality = str(job.get("modality") or "")
        operation = str(job.get("operation") or "")
        if modality != "tts" or operation not in VOICEBOX_TTS_OPERATIONS:
            return False
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="voicebox_speech", progress=72)
        cancelled, result = await self.await_cancellable_runtime_call(job, self.post_voicebox_speech(await self.voicebox_payload_for_job(job)))
        if cancelled:
            return True
        content, mime_type = result
        if await self.cancel_if_requested(job["id"]):
            return True
        if not content:
            await database.update_job(
                job["id"],
                state=JobState.RECOVERY_REQUIRED.value,
                stage="voicebox_empty_speech",
                progress=90,
                failure_category="voicebox_empty_speech",
                failure_message="Voicebox returned an empty speech response",
            )
            return True
        artifact = media_artifacts.write_artifact_bytes(
            self.artifact_root,
            namespace="voicebox",
            job_id=str(job["id"]),
            index=0,
            content=content,
            mime_type=mime_type,
            source="voicebox_runtime",
            metadata={
                "runtime": "voicebox",
                "model": self.resolved_model_id(job),
                "operation": operation,
                **self.voicebox_artifact_profile_metadata(job),
            },
        )
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
        return True

    def voicebox_artifact_profile_metadata(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            profile_id = voice_profile_policy.requested_voice_profile_id(self.request_input(job))
        except ValueError:
            return {}
        return {"voice_profile_id": profile_id} if profile_id else {}

    async def run_once(self) -> bool:
        if await runner_paused(self.pause_check):
            return False
        if await self.interactive_waiter_pending():
            return False
        job = await database.claim_next_job(
            GPU_RUNTIMES,
            claimed_state=JobState.WAITING_FOR_GPU.value,
            claimed_stage="waiting_for_gpu",
            claimed_progress=20,
            claimable_states=[JobState.QUEUED.value, JobState.WAITING_FOR_GPU.value],
            respect_runtime_reservations=True,
        )
        if job is None:
            return await self.unload_expired_idle_runtime()
        if await self.interactive_waiter_pending():
            return False
        if not await self.acquire_gpu_lease():
            return False
        load_started: float | None = None
        run_started: float | None = None
        runtime_prepared = False
        try:
            for state, stage, progress in GPU_STATE_STEPS:
                if await self.cancel_if_requested(job["id"]):
                    return True
                update_payload: dict[str, Any] = {"state": state.value, "stage": stage, "progress": progress}
                if state == JobState.RUNNING:
                    update_payload["started_at"] = datetime.now(tz=UTC)
                    run_started = monotonic()
                await database.update_job(job["id"], **update_payload)
                if state == JobState.UNLOADING:
                    await self.unload_gpu_runtimes_for_job(job)
                if state == JobState.VERIFYING_VRAM:
                    await self.verify_vram_or_recover(job)
                if state == JobState.LOADING:
                    load_started = monotonic()
                    await self.load_runtime_model(job)
                if state == JobState.WARMING:
                    await self.warm_runtime_model(job)
                    runtime_prepared = True
                    if load_started is not None:
                        load_time_ms = elapsed_milliseconds(load_started)
                        job["load_time_ms"] = load_time_ms
                        await database.update_job(job["id"], load_time_ms=load_time_ms)
            if await self.cancel_if_requested(job["id"]):
                if run_started is not None:
                    await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            if job["runtime"] in {"comfyui", "lan-p40-media"}:
                if self.is_talking_head_lipsync_job(job):
                    await self.run_talking_head_lipsync_job(job)
                elif self.is_studio_seated_character_job(job):
                    await self.run_studio_seated_character_job(job)
                else:
                    await self.run_comfyui_job(job)
                await self.record_peak_resources(job)
                if run_started is not None:
                    await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            if job["runtime"] == "localai" and await self.run_localai_media_job(job):
                await self.record_peak_resources(job)
                if run_started is not None:
                    await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            if job["runtime"] == "voicebox" and await self.run_voicebox_job(job):
                await self.record_peak_resources(job)
                if run_started is not None:
                    await database.update_job(job["id"], run_time_ms=elapsed_milliseconds(run_started))
                return True
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "unsupported_gpu_operation",
                "progress": 100,
                "failure_category": "unsupported_gpu_operation",
                "failure_message": unsupported_operation_message(job),
                "artifacts": [],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except RuntimePreparationError as exc:
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "runtime_prepare_failed",
                "progress": 100,
                "failure_category": "runtime_prepare_failed",
                "failure_message": str(exc)[:500],
            }
            if load_started is not None:
                failure_update["load_time_ms"] = elapsed_milliseconds(load_started)
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except LipsyncInputRejectedError as exc:
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "invalid_lipsync_input",
                "progress": 100,
                "failure_category": "invalid_lipsync_input",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except StudioPanelQualityError as exc:
            failure_update = {
                "state": JobState.FAILED.value,
                "stage": "studio_panel_qc_failed",
                "progress": 100,
                "failure_category": "studio_panel_qc_failed",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except SeatedCharacterQualityError as exc:
            failure_update = {
                "state": JobState.FAILED.value,
                "stage": "studio_seated_character_qc_failed",
                "progress": 100,
                "failure_category": "studio_seated_character_qc_failed",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except SeatedReferenceRequiredError as exc:
            failure_update = {
                "state": JobState.FAILED.value,
                "stage": "seated_reference_required",
                "progress": 100,
                "failure_category": "seated_reference_required",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except SeatedPosePipelineUnavailableError as exc:
            failure_update = {
                "state": JobState.FAILED.value,
                "stage": "seated_pose_pipeline_unavailable",
                "progress": 100,
                "failure_category": "seated_pose_pipeline_unavailable",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except CameraCoverageError as exc:
            failure_update = {
                "state": JobState.FAILED.value,
                "stage": "unsupported_camera_coverage",
                "progress": 100,
                "failure_category": "unsupported_camera_coverage",
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except LipsyncRuntimeFailure as exc:
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "lipsync_runtime_failed",
                "progress": 100,
                "failure_category": exc.code,
                "failure_message": str(exc)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(job["id"], **failure_update)
            await self.record_peak_resources(job)
        except Exception as exc:
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "failed",
                "progress": 100,
                "failure_category": "gpu_runner_error",
                "failure_message": (str(exc) or exc.__class__.__name__)[:500],
            }
            if run_started is not None:
                failure_update["run_time_ms"] = elapsed_milliseconds(run_started)
            await database.update_job(
                job["id"],
                **failure_update,
            )
        finally:
            if runtime_prepared:
                with suppress(Exception):
                    await self.record_runtime_idle_for_job(
                        job,
                        {
                            "last_state": str((await database.get_job(job["id"]) or job).get("state") or ""),
                        },
                    )
            await self.release_gpu_lease()
        return True

    async def run_forever(self) -> None:
        await record_runner_startup_reconciliation(self, GPU_RUNTIMES)
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped.set()


class ModelDownloadRunner:
    def __init__(
        self,
        data_root: Path,
        interval_seconds: int = 2,
        chunk_size: int = 1024 * 1024,
        request_timeout_seconds: int = 120,
        master_key: str = "",
        pause_check: PauseCheck | None = None,
    ) -> None:
        self.data_root = data_root
        self.interval_seconds = max(1, interval_seconds)
        self.chunk_size = max(64 * 1024, chunk_size)
        self.request_timeout_seconds = max(10, request_timeout_seconds)
        self.master_key = master_key.strip()
        self.pause_check = pause_check
        self._stopped = asyncio.Event()
        self.startup_reconciliation = pending_startup_reconciliation(MODEL_DOWNLOAD_RUNTIMES)

    def _download_path_parent_parts(self, path: Path, description: str, *, partial: bool) -> tuple[Path, tuple[str, ...]]:
        candidate = path.absolute()
        blob_root = (self.data_root / "models" / "blobs").absolute()
        expected_parent = blob_root / ".partial" if partial else blob_root
        if candidate.parent != expected_parent:
            raise model_lifecycle.ModelLifecycleError(f"{description} is outside the model blob library")
        try:
            parent_parts = candidate.parent.relative_to(self.data_root.absolute()).parts
        except ValueError as exc:
            raise model_lifecycle.ModelLifecycleError(f"{description} is outside the configured data root") from exc
        return candidate, parent_parts

    def ensure_download_parent_tree(self, path: Path, description: str, *, partial: bool, allow_missing: bool) -> None:
        _, parent_parts = self._download_path_parent_parts(path, description, partial=partial)
        current = self.data_root.absolute()
        for segment in parent_parts:
            current = current / segment
            try:
                file_stat = current.lstat()
            except FileNotFoundError as exc:
                if allow_missing:
                    return
                raise model_lifecycle.ModelLifecycleError(f"{description} parent directory is missing") from exc
            if stat.S_ISLNK(file_stat.st_mode):
                raise model_lifecycle.ModelLifecycleError(f"{description} contains a symlink")
            if not stat.S_ISDIR(file_stat.st_mode):
                raise model_lifecycle.ModelLifecycleError(f"{description} parent is not a directory")

    @staticmethod
    def regular_file_stat(path: Path, description: str, *, missing_ok: bool = False) -> os.stat_result | None:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise model_lifecycle.ModelLifecycleError(f"{description} is missing")
        if stat.S_ISLNK(file_stat.st_mode):
            raise model_lifecycle.ModelLifecycleError(f"{description} is a symlink")
        if not stat.S_ISREG(file_stat.st_mode):
            raise model_lifecycle.ModelLifecycleError(f"{description} is not a regular file")
        return file_stat

    def existing_regular_file_size(self, path: Path, description: str) -> int:
        file_stat = self.regular_file_stat(path, description, missing_ok=True)
        return int(file_stat.st_size) if file_stat is not None else 0

    def validate_content_addressed_download_paths(self, target: Path, partial: Path, expected_sha256: str) -> str:
        digest = expected_sha256.strip().lower()
        if not model_lifecycle.SHA256_RE.fullmatch(digest):
            raise model_lifecycle.ModelLifecycleError("target blob SHA-256 is invalid")
        expected_target = model_lifecycle.blob_path_for(self.data_root, digest).absolute()
        expected_partial = model_lifecycle.blob_partial_path_for(self.data_root, digest).absolute()
        if target.absolute() != expected_target:
            raise model_lifecycle.ModelLifecycleError("target blob path does not match target SHA-256")
        if partial.absolute() != expected_partial:
            raise model_lifecycle.ModelLifecycleError("partial blob path does not match target SHA-256")
        return digest

    @staticmethod
    def open_partial_file(path: Path, mode: str) -> Any:
        if mode not in {"ab", "wb"}:
            raise model_lifecycle.ModelLifecycleError("partial blob file mode is invalid")
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_APPEND if mode == "ab" else os.O_TRUNC
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        try:
            fd = os.open(path, flags, 0o600)
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise model_lifecycle.ModelLifecycleError("partial blob path is not a regular file")
            handle = os.fdopen(fd, mode)
            fd = -1
            return handle
        except IsADirectoryError as exc:
            raise model_lifecycle.ModelLifecycleError("partial blob path is not a regular file") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise model_lifecycle.ModelLifecycleError("partial blob path is a symlink") from exc
            raise
        finally:
            if fd >= 0:
                os.close(fd)

    def publish_verified_partial(self, partial: Path, target: Path, *, expected_size: int, expected_sha256: str) -> None:
        self.regular_file_stat(partial, "partial blob path")
        target_stat = self.regular_file_stat(target, "target blob path", missing_ok=True)
        if target_stat is not None:
            if target_stat.st_size == expected_size and model_lifecycle.sha256_file(target) == expected_sha256:
                partial.unlink(missing_ok=True)
                target.chmod(0o644)
                return
            raise model_lifecycle.ModelLifecycleError("target blob exists but does not verify")
        partial.replace(target)
        target.chmod(0o644)

    async def reconcile_startup(self) -> dict[str, Any]:
        return await database.reconcile_interrupted_model_downloads()

    async def stop_if_requested(self, download_id: str) -> bool:
        current = await database.get_model_download(download_id)
        if current and current["status"] == "cancelling":
            await database.update_model_download(download_id, status="cancelled", stage="cancelled")
            return True
        if current and current["status"] == "pausing":
            await database.update_model_download(download_id, status="paused", stage="paused")
            return True
        return False

    async def run_once(self) -> bool:
        if await runner_paused(self.pause_check):
            return False
        download = await database.claim_next_model_download()
        if download is None:
            return False
        download_id = download["id"]
        try:
            if await self.stop_if_requested(download_id):
                return True
            manifest = model_lifecycle.parse_uploaded_manifest(download["manifest"])
            plan = model_lifecycle.build_download_plan(manifest, self.data_root, accept_license=bool(download.get("license_accepted")))
            if not plan["can_download"]:
                raise model_lifecycle.ModelLifecycleError("; ".join(plan["blockers"]) or "download is blocked")
            if plan["already_available"]:
                await database.update_model_download(
                    download_id,
                    status="completed",
                    stage="already_available",
                    bytes_downloaded=plan["target_size_bytes"],
                    error_category=None,
                    error_message=None,
                )
                return True
            auth_headers = await self.download_auth_headers(download)
            await self.download_plan(download_id, plan, auth_headers=auth_headers)
        except Exception as exc:
            await database.update_model_download(
                download_id,
                status="failed",
                stage="failed",
                error_category=exc.__class__.__name__,
                error_message=str(exc)[:500],
            )
        return True

    async def download_auth_headers(self, download: dict[str, Any]) -> dict[str, str]:
        secret_name = str(download.get("credential_secret_name") or "").strip()
        if not secret_name:
            return {}
        try:
            normalized_name = secret_store.validate_secret_name(secret_name)
        except secret_store.SecretStoreError as exc:
            raise model_lifecycle.ModelLifecycleError(str(exc)) from exc
        if not self.master_key:
            raise model_lifecycle.ModelLifecycleError("model download credential is configured but the master encryption key is unavailable")
        secret_row = await database.get_encrypted_secret(normalized_name)
        if secret_row is None:
            raise model_lifecycle.ModelLifecycleError("model download credential secret is not active")
        if secret_row.get("category") != "model-download":
            raise model_lifecycle.ModelLifecycleError("model download credential secret must be in category model-download")
        try:
            token = secret_store.decrypt_value(self.master_key, normalized_name, secret_row.get("secret_envelope") or {}).strip()
        except secret_store.SecretStoreError as exc:
            raise model_lifecycle.ModelLifecycleError(str(exc)) from exc
        if not token:
            raise model_lifecycle.ModelLifecycleError("model download credential is empty")
        if "\r" in token or "\n" in token:
            raise model_lifecycle.ModelLifecycleError("model download credential contains invalid header characters")
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def response_header(headers: Any, name: str) -> str:
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is None and hasattr(headers, "items"):
            lowered = name.lower()
            for key, candidate in headers.items():
                if str(key).lower() == lowered:
                    value = candidate
                    break
        return str(value).strip() if value is not None else ""

    def validate_download_response_headers(self, file_plan: dict[str, Any], status_code: int, headers: Any, resume_from: int) -> None:
        expected_size = int(file_plan["target_size_bytes"])
        content_length = self.response_header(headers, "Content-Length")
        if content_length:
            try:
                actual_length = int(content_length)
            except ValueError as exc:
                raise model_lifecycle.ModelLifecycleError("download returned invalid Content-Length") from exc
            expected_length = expected_size - resume_from if status_code == 206 and resume_from else expected_size
            if actual_length != expected_length:
                raise model_lifecycle.ModelLifecycleError(f"download returned unexpected Content-Length {actual_length} != {expected_length}")
        if status_code != 206:
            return
        if resume_from <= 0:
            raise model_lifecycle.ModelLifecycleError("download returned partial content without a resume request")
        content_range = self.response_header(headers, "Content-Range")
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range)
        if not match:
            raise model_lifecycle.ModelLifecycleError(f"download returned invalid Content-Range {content_range or '<missing>'}")
        start, end, total = (int(part) for part in match.groups())
        if start != resume_from or total != expected_size or end != expected_size - 1:
            raise model_lifecycle.ModelLifecycleError(
                f"download returned unexpected Content-Range {content_range}; expected bytes {resume_from}-{expected_size - 1}/{expected_size}"
            )

    async def download_plan(self, download_id: str, plan: dict[str, Any], *, auth_headers: dict[str, str] | None = None) -> None:
        file_plans = plan.get("files") or [plan]
        completed_bytes = 0
        total_files = len(file_plans)
        for index, file_plan in enumerate(file_plans, start=1):
            stage_prefix = f"file_{index}_of_{total_files}"
            if await self.download_file(download_id, file_plan, completed_bytes, stage_prefix, auth_headers=auth_headers):
                completed_bytes += int(file_plan["target_size_bytes"])
            else:
                return
        if await self.stop_if_requested(download_id):
            return
        await database.update_model_download(
            download_id,
            status="completed",
            stage="verified",
            bytes_downloaded=plan["target_size_bytes"],
            error_category=None,
            error_message=None,
        )

    async def download_file(
        self,
        download_id: str,
        file_plan: dict[str, Any],
        completed_before: int,
        stage_prefix: str,
        *,
        auth_headers: dict[str, str] | None = None,
    ) -> bool:
        if await self.stop_if_requested(download_id):
            return False
        target = Path(file_plan["target_path"])
        partial = Path(file_plan["partial_path"])
        expected_size = int(file_plan["target_size_bytes"])
        expected_sha256 = self.validate_content_addressed_download_paths(target, partial, str(file_plan["target_sha256"]))
        self.ensure_download_parent_tree(target, "target blob path", partial=False, allow_missing=True)
        self.ensure_download_parent_tree(partial, "partial blob path", partial=True, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_download_parent_tree(target, "target blob path", partial=False, allow_missing=False)
        self.ensure_download_parent_tree(partial, "partial blob path", partial=True, allow_missing=False)
        target_stat = self.regular_file_stat(target, "target blob path", missing_ok=True)
        if target_stat is not None:
            if target_stat.st_size == expected_size and model_lifecycle.sha256_file(target) == expected_sha256:
                await database.update_model_download(
                    download_id,
                    stage=f"{stage_prefix}_already_available",
                    bytes_downloaded=completed_before + expected_size,
                    error_category=None,
                    error_message=None,
                )
                return True
            raise model_lifecycle.ModelLifecycleError("target blob exists but does not verify")
        existing = self.existing_regular_file_size(partial, "partial blob path")
        if existing > expected_size:
            raise model_lifecycle.ModelLifecycleError("partial download is larger than expected")
        if existing == expected_size:
            if model_lifecycle.sha256_file(partial) == expected_sha256:
                self.publish_verified_partial(
                    partial,
                    target,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
                await database.update_model_download(
                    download_id,
                    stage=f"{stage_prefix}_verified",
                    bytes_downloaded=completed_before + expected_size,
                    error_category=None,
                    error_message=None,
                )
                return True
            partial.unlink()
            existing = 0
        mode = "ab" if existing else "wb"
        timeout = httpx.Timeout(self.request_timeout_seconds)
        source_url = str(file_plan["source_url"])
        request_url = source_url
        redirect_count = 0
        source_type = str(file_plan.get("source_type") or "direct-url")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            while True:
                request_url = model_lifecycle.download_request_url_allowed(source_url, request_url, source_type=source_type)
                headers: dict[str, str] = {}
                if model_lifecycle.send_download_authorization(source_url, request_url):
                    headers.update(auth_headers or {})
                if existing:
                    headers["Range"] = f"bytes={existing}-"
                async with client.stream("GET", request_url, headers=headers) as response:
                    if response.status_code in model_lifecycle.DOWNLOAD_REDIRECT_STATUS_CODES:
                        redirect_count += 1
                        if redirect_count > 5:
                            raise model_lifecycle.ModelLifecycleError("download exceeded maximum redirect count")
                        request_url = model_lifecycle.redirect_url_allowed(
                            source_url,
                            request_url,
                            response.headers.get("location", ""),
                            source_type=source_type,
                        )
                        continue
                    if existing and response.status_code == 200:
                        existing = 0
                        mode = "wb"
                    elif existing and response.status_code == 416:
                        partial.unlink(missing_ok=True)
                        existing = 0
                        mode = "wb"
                        request_url = source_url
                        continue
                    elif response.status_code not in {200, 206}:
                        raise model_lifecycle.ModelLifecycleError(f"download returned HTTP {response.status_code}")
                    self.validate_download_response_headers(file_plan, response.status_code, getattr(response, "headers", {}), existing)
                    downloaded = existing
                    await database.update_model_download(download_id, stage=f"{stage_prefix}_downloading", bytes_downloaded=completed_before + downloaded)
                    with self.open_partial_file(partial, mode) as handle:
                        async for chunk in response.aiter_bytes(self.chunk_size):
                            if not chunk:
                                continue
                            if await self.stop_if_requested(download_id):
                                return False
                            downloaded += len(chunk)
                            if downloaded > expected_size:
                                raise model_lifecycle.ModelLifecycleError("download exceeded expected size")
                            handle.write(chunk)
                            await database.update_model_download(download_id, stage=f"{stage_prefix}_downloading", bytes_downloaded=completed_before + downloaded)
                    break
        actual_size = self.existing_regular_file_size(partial, "partial blob path")
        if actual_size != expected_size:
            raise model_lifecycle.ModelLifecycleError(f"downloaded size {actual_size} does not match expected {expected_size}")
        actual_sha = model_lifecycle.sha256_file(partial)
        if actual_sha != expected_sha256:
            raise model_lifecycle.ModelLifecycleError("downloaded SHA-256 does not match manifest")
        self.publish_verified_partial(
            partial,
            target,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        await database.update_model_download(
            download_id,
            stage=f"{stage_prefix}_verified",
            bytes_downloaded=completed_before + expected_size,
            error_category=None,
            error_message=None,
        )
        return True

    async def run_forever(self) -> None:
        await record_runner_startup_reconciliation(self, MODEL_DOWNLOAD_RUNTIMES)
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped.set()
