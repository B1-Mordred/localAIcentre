from __future__ import annotations

import asyncio
import base64
import binascii
import json
from contextlib import suppress
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from . import database
from . import comfyui_native
from . import media_artifacts
from . import model_lifecycle
from . import secret_store
from .runtime_agent_http import runtime_agent_httpx_kwargs
from .scheduler import JobState

import httpx


CPU_RUNTIMES = ["audio-cpu"]
GPU_RUNTIMES = ["localai", "comfyui", "voicebox"]
CPU_TTS_OPERATIONS = {"speech", "text-to-speech", "tts"}
CPU_STT_OPERATIONS = {"transcription", "speech-to-text", "stt"}
VOICEBOX_TTS_OPERATIONS = {"speech", "text-to-speech", "tts"}
LOCALAI_IMAGE_GENERATION_OPERATIONS = {"generation", "image-generation", "text-to-image"}
LOCALAI_IMAGE_EDIT_OPERATIONS = {"edit", "image-edit", "image-to-image", "inpainting", "inpainting-outpainting"}
LOCALAI_VIDEO_GENERATION_OPERATIONS = {"generation", "video-generation", "text-to-video"}
LOCALAI_VIDEO_IMAGE_OPERATIONS = {"image-to-video", "video-image", "image-video"}

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


class CpuJobRunner:
    def __init__(
        self,
        artifact_root: Path,
        interval_seconds: int = 1,
        audio_cpu_url: str = "",
        pause_check: PauseCheck | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.interval_seconds = max(1, interval_seconds)
        self.audio_cpu_url = audio_cpu_url.rstrip("/")
        self.pause_check = pause_check
        self._stopped = asyncio.Event()

    async def reconcile_startup(self) -> dict[str, int]:
        marked = await database.mark_interrupted_jobs_recovery_required(CPU_RUNTIMES)
        requeued = await database.requeue_recovery_jobs(CPU_RUNTIMES)
        return {"marked_recovery_required": marked, "requeued": requeued}

    async def cancel_if_requested(self, job_id: str) -> bool:
        current = await database.get_job(job_id)
        if current and current["state"] in {JobState.CANCELLING.value, JobState.CANCELLED.value}:
            await database.update_job(job_id, state=JobState.CANCELLED.value, stage="cancelled", progress=100)
            return True
        return False

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

    async def post_audio_cpu_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        if not self.audio_cpu_url:
            raise RuntimeError("audio-cpu runtime URL is not configured")
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(f"{self.audio_cpu_url}/v1/audio/speech", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"audio-cpu speech returned HTTP {response.status_code}")
        return response.content, response.headers.get("content-type", "audio/wav").split(";")[0]

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
            content, mime_type = await self.post_audio_cpu_speech(self.audio_payload_for_job(job))
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
                metadata={"runtime": "audio-cpu", "model": self.resolved_model_id(job), "operation": operation},
            )
            await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
            await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
            return True
        if modality == "stt" and operation in CPU_STT_OPERATIONS:
            await database.update_job(job["id"], state=JobState.RUNNING.value, stage="audio_cpu_transcription", progress=60)
            if await self.cancel_if_requested(job["id"]):
                return True
            body = await self.post_audio_cpu_transcription(self.audio_payload_for_job(job))
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
                },
            )
            await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
            await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
            return True
        return False

    async def run_once(self) -> bool:
        if await runner_paused(self.pause_check):
            return False
        job = await database.claim_next_job(CPU_RUNTIMES)
        if job is None:
            return False
        run_started = monotonic()
        await database.update_job(job["id"], started_at=datetime.now(tz=UTC))
        try:
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
        await self.reconcile_startup()
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
        lease_owner: str = "control-plane-gpu-runner",
        lease_ttl_seconds: int = 300,
        runtime_agent_url: str = "",
        runtime_agent_token: str = "",
        runtime_agent_tls_ca_file: str = "",
        runtime_agent_tls_client_cert_file: str = "",
        runtime_agent_tls_client_key_file: str = "",
        runtime_agent_tls_verify: bool = True,
        reserve_vram_gib: float = 1.5,
        recovery_timeout_seconds: int = 10,
        default_idle_timeout_seconds: int = 300,
        runtime_urls: dict[str, str] | None = None,
        comfyui_poll_seconds: int = 2,
        comfyui_completion_timeout_seconds: int = 7200,
        pause_check: PauseCheck | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.interval_seconds = max(1, interval_seconds)
        self.lease_owner = lease_owner
        self.lease_ttl_seconds = max(30, lease_ttl_seconds)
        self.runtime_agent_url = runtime_agent_url.rstrip("/")
        self.runtime_agent_token = runtime_agent_token
        self.runtime_agent_tls_ca_file = runtime_agent_tls_ca_file
        self.runtime_agent_tls_client_cert_file = runtime_agent_tls_client_cert_file
        self.runtime_agent_tls_client_key_file = runtime_agent_tls_client_key_file
        self.runtime_agent_tls_verify = runtime_agent_tls_verify
        self.reserve_vram_mib = int(max(0.0, reserve_vram_gib) * 1024)
        self.recovery_timeout_seconds = max(1, recovery_timeout_seconds)
        self.default_idle_timeout_seconds = max(0, int(default_idle_timeout_seconds))
        self.runtime_urls = {key: value.rstrip("/") for key, value in (runtime_urls or {}).items() if value}
        self.comfyui_poll_seconds = max(1, comfyui_poll_seconds)
        self.comfyui_completion_timeout_seconds = max(30, comfyui_completion_timeout_seconds)
        self.pause_check = pause_check
        self._stopped = asyncio.Event()

    async def reconcile_startup(self) -> dict[str, int]:
        marked = await database.mark_interrupted_jobs_recovery_required(GPU_RUNTIMES)
        requeued = await database.requeue_recovery_jobs(GPU_RUNTIMES)
        return {"marked_recovery_required": marked, "requeued": requeued}

    async def acquire_gpu_lease(self) -> bool:
        lease = await database.acquire_scheduler_owner(self.lease_owner, self.lease_ttl_seconds)
        return bool(lease.get("acquired"))

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
        except (httpx.HTTPError, ValueError):
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
        except (httpx.HTTPError, ValueError):
            return None

    async def unload_other_gpu_runtimes(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
        target_runtime = str(job.get("runtime") or "")
        if not self.runtime_agent_url or target_runtime not in GPU_RUNTIMES:
            return []
        results: list[dict[str, Any] | None] = []
        for runtime in GPU_RUNTIMES:
            if runtime == target_runtime:
                continue
            result = await self.runtime_agent_post(
                f"/v1/runtime-actions/{runtime}/unload",
                {
                    "reason": f"prepare {target_runtime} for job {job['id']}: unload other GPU runtime {runtime}",
                    "timeout_seconds": self.recovery_timeout_seconds,
                },
            )
            await self.record_runtime_state_for_job(
                runtime,
                self.runtime_hook_state_status("unload", result),
                "unloading",
                job,
                {
                    "target_runtime": target_runtime,
                    "hook": self.compact_hook_result(result),
                },
                record_model=False,
            )
            results.append(result)
        return results

    def runtime_control_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(job["id"]),
            "runtime": str(job.get("runtime") or ""),
            "model": self.resolved_model_id(job),
            "model_alias": str(job.get("model_alias") or ""),
            "resolved_model_version": str(job.get("resolved_model_version") or ""),
            "modality": str(job.get("modality") or ""),
            "operation": str(job.get("operation") or ""),
        }

    def compact_hook_result(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"status": "unconfirmed"}
        allowed_keys = {"status", "reason", "action", "strategy", "runtime_action", "service", "runtime", "message", "error", "code"}
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
        return await self.upsert_runtime_state(
            {
                "runtime": str(job.get("runtime") or ""),
                "status": "idle",
                "stage": "idle",
                "active_model": self.resolved_model_id(job),
                "model_alias": str(job.get("model_alias") or ""),
                "resolved_model_version": str(job.get("resolved_model_version") or ""),
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
        if not self.runtime_agent_url:
            return False
        candidate = await self.expired_idle_runtime_state()
        if candidate is None:
            return False
        state, idle_seconds, timeout_seconds = candidate
        runtime = str(state.get("runtime") or "")
        if not await self.acquire_gpu_lease():
            return False
        result: dict[str, Any] | None = None
        try:
            result = await self.runtime_agent_post(
                f"/v1/runtime-actions/{runtime}/unload",
                {
                    "reason": (
                        f"idle timeout expired for {runtime} model {state.get('active_model') or state.get('resolved_model_version')} "
                        f"after {idle_seconds}s >= {timeout_seconds}s"
                    ),
                    "timeout_seconds": self.recovery_timeout_seconds,
                },
            )
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
                        "hook": self.compact_hook_result(result),
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
        try:
            async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as client:
                response = await client.post(url, json=payload, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            return {"status": "unsupported", "reason": "runtime_control_unreachable", "runtime": runtime, "action": action}
        if response.status_code in {404, 405}:
            return {"status": "unsupported", "reason": f"http_{response.status_code}", "runtime": runtime, "action": action}
        if response.status_code >= 400:
            raise RuntimeError(f"{runtime} runtime {action} hook returned HTTP {response.status_code}")
        if not response.content:
            return {"status": "ok", "runtime": runtime, "action": action}
        try:
            body = response.json()
        except ValueError:
            return {"status": "ok", "runtime": runtime, "action": action}
        return body if isinstance(body, dict) else {"status": "ok", "runtime": runtime, "action": action}

    async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        runtime = str(job.get("runtime") or "")
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
        runtime = str(job.get("runtime") or "")
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
        runtime = str(job.get("runtime") or "")
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
                str(job.get("runtime") or ""),
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
        await self.record_peak_resources_from_metrics(job, await self.runtime_agent_get("/v1/metrics"))

    async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
        before_metrics = await self.runtime_agent_get("/v1/metrics")
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
                failure_message=f"VRAM used {max(before)} MiB exceeds reserve {self.reserve_vram_mib} MiB before {job['runtime']}",
            )
        else:
            await self.record_runtime_state_for_job(
                str(job.get("runtime") or ""),
                "recovering",
                "verifying_vram",
                job,
                {"memory_used_mib": max(before), "reserve_mib": self.reserve_vram_mib},
            )
        await self.runtime_agent_post(
            f"/v1/runtime-actions/{job['runtime']}/recover",
            {
                "reason": f"VRAM used {max(before)} MiB exceeds reserve {self.reserve_vram_mib} MiB before job {job['id']}",
                "timeout_seconds": self.recovery_timeout_seconds,
            },
        )
        after_metrics = await self.runtime_agent_get("/v1/metrics")
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
                str(job.get("runtime") or ""),
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
            return comfyui_native.workflow_comfyui_prompt_payload(manifest, input_payload, client_id=str(job["id"]), extra_data=extra_data)
        raise ValueError("ComfyUI media jobs require input.comfyui_prompt, input.workflow_json, or a published workflow with workflow_json")

    async def submit_comfyui_prompt(self, payload: dict[str, Any]) -> str:
        comfyui_url = self.runtime_urls.get("comfyui")
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{comfyui_url}/prompt", json=payload)
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

    async def fetch_comfyui_history(self, prompt_id: str) -> dict[str, Any] | None:
        comfyui_url = self.runtime_urls.get("comfyui")
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        return await comfyui_native.fetch_comfyui_history(comfyui_url, prompt_id)

    async def interrupt_comfyui(self) -> None:
        comfyui_url = self.runtime_urls.get("comfyui")
        if not comfyui_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{comfyui_url}/interrupt")
        except httpx.HTTPError:
            return

    async def wait_for_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        deadline = monotonic() + self.comfyui_completion_timeout_seconds
        while monotonic() < deadline:
            if await self.cancel_if_requested(job["id"]):
                await self.interrupt_comfyui()
                return None
            history = await self.fetch_comfyui_history(prompt_id)
            if history is not None:
                return history
            await database.update_job(job["id"], state=JobState.RUNNING.value, stage="comfyui_waiting_history", progress=80)
            await asyncio.sleep(self.comfyui_poll_seconds)
        return None

    async def ingest_comfyui_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        comfyui_url = self.runtime_urls.get("comfyui")
        if not comfyui_url:
            raise RuntimeError("ComfyUI runtime URL is not configured")
        return await comfyui_native.ingest_comfyui_artifacts(artifacts, self.artifact_root, comfyui_url)

    async def run_comfyui_job(self, job: dict[str, Any]) -> None:
        payload = await self.comfyui_prompt_payload_for_job(job)
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="comfyui_submitting", progress=72)
        prompt_id = await self.submit_comfyui_prompt(payload)
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
        current = await database.get_job(job["id"]) or job
        artifacts = await self.ingest_comfyui_artifacts(
            comfyui_native.merge_job_artifacts(
                current.get("artifacts") or [],
                comfyui_native.comfyui_artifacts_from_history(prompt_id, history),
            )
        )
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
            body = await self.post_localai_media_multipart(endpoint, payload)
        else:
            body = await self.post_localai_media_json(endpoint, payload)
        await database.update_job(job["id"], state=JobState.RUNNING.value, stage="localai_response_received", progress=85)
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
        return payload

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
        content, mime_type = await self.post_voicebox_speech(await self.voicebox_payload_for_job(job))
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
            metadata={"runtime": "voicebox", "model": self.resolved_model_id(job), "operation": operation},
        )
        await database.update_job(job["id"], state=JobState.SAVING.value, stage="saving", progress=90, artifacts=[artifact])
        await database.update_job(job["id"], state=JobState.COMPLETED.value, stage="completed", progress=100, artifacts=[artifact])
        return True

    async def run_once(self) -> bool:
        if await runner_paused(self.pause_check):
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
                    await self.unload_other_gpu_runtimes(job)
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
            if job["runtime"] == "comfyui":
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
        except Exception as exc:
            failure_update: dict[str, Any] = {
                "state": JobState.FAILED.value,
                "stage": "failed",
                "progress": 100,
                "failure_category": "gpu_runner_error",
                "failure_message": exc.__class__.__name__,
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
        await self.reconcile_startup()
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
            plan = model_lifecycle.build_download_plan(manifest, self.data_root)
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
            if not plan["can_download"]:
                raise model_lifecycle.ModelLifecycleError("; ".join(plan["blockers"]) or "download is blocked")
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
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or partial.is_symlink():
            raise model_lifecycle.ModelLifecycleError("target or partial path is a symlink")
        if target.exists():
            if target.is_file() and target.stat().st_size == file_plan["target_size_bytes"] and model_lifecycle.sha256_file(target) == file_plan["target_sha256"]:
                await database.update_model_download(
                    download_id,
                    stage=f"{stage_prefix}_already_available",
                    bytes_downloaded=completed_before + int(file_plan["target_size_bytes"]),
                    error_category=None,
                    error_message=None,
                )
                return True
            raise model_lifecycle.ModelLifecycleError("target blob exists but does not verify")
        existing = partial.stat().st_size if partial.exists() else 0
        if existing > file_plan["target_size_bytes"]:
            raise model_lifecycle.ModelLifecycleError("partial download is larger than expected")
        mode = "ab" if existing else "wb"
        timeout = httpx.Timeout(self.request_timeout_seconds)
        source_url = str(file_plan["source_url"])
        request_url = source_url
        redirect_count = 0
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            while True:
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
                            source_type=str(file_plan.get("source_type") or "direct-url"),
                        )
                        continue
                    if existing and response.status_code == 200:
                        existing = 0
                        mode = "wb"
                    elif response.status_code not in {200, 206}:
                        raise model_lifecycle.ModelLifecycleError(f"download returned HTTP {response.status_code}")
                    downloaded = existing
                    await database.update_model_download(download_id, stage=f"{stage_prefix}_downloading", bytes_downloaded=completed_before + downloaded)
                    with partial.open(mode) as handle:
                        async for chunk in response.aiter_bytes(self.chunk_size):
                            if not chunk:
                                continue
                            if await self.stop_if_requested(download_id):
                                return False
                            downloaded += len(chunk)
                            if downloaded > file_plan["target_size_bytes"]:
                                raise model_lifecycle.ModelLifecycleError("download exceeded expected size")
                            handle.write(chunk)
                            await database.update_model_download(download_id, stage=f"{stage_prefix}_downloading", bytes_downloaded=completed_before + downloaded)
                    break
        actual_size = partial.stat().st_size
        if actual_size != file_plan["target_size_bytes"]:
            raise model_lifecycle.ModelLifecycleError(f"downloaded size {actual_size} does not match expected {file_plan['target_size_bytes']}")
        actual_sha = model_lifecycle.sha256_file(partial)
        if actual_sha != file_plan["target_sha256"]:
            raise model_lifecycle.ModelLifecycleError("downloaded SHA-256 does not match manifest")
        partial.replace(target)
        target.chmod(0o644)
        await database.update_model_download(
            download_id,
            stage=f"{stage_prefix}_verified",
            bytes_downloaded=completed_before + int(file_plan["target_size_bytes"]),
            error_category=None,
            error_message=None,
        )
        return True

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped.set()
