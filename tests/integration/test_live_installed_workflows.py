from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import time
import unittest
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from test_live_stack import (  # noqa: E402
    LiveApiClient,
    TERMINAL_STATES,
    assert_media_job_links,
    measured_model_alias,
    media_job_link,
)


INSTALLED_WORKFLOWS_EVIDENCE_FORMAT = "b1-ai-hub-installed-workflows-acceptance/v1"
INSTALLED_WORKFLOWS_REQUIRED_CHECKS = (
    "chat_completed",
    "tts_completed",
    "stt_completed",
    "cpu_audio_does_not_take_gpu_lease",
    "image_generation_completed",
    "image_edit_completed",
    "short_video_completed",
    "media_artifacts_verified",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_json_from_env(value_name: str, file_name: str) -> dict[str, Any] | None:
    raw = os.getenv(value_name, "").strip()
    if raw:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise AssertionError(f"{value_name} must decode to a JSON object")
        return payload
    path = os.getenv(file_name, "").strip()
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{file_name} must point to a JSON object")
    return payload


def silence_wav_base64() -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def placeholder_proof(headers: dict[str, str], payload: Any, *, runtime: str | None = None) -> dict[str, Any]:
    header_map = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    header_placeholder = header_map.get("x-b1-placeholder")
    body_placeholder = payload.get("b1_placeholder") if isinstance(payload, dict) else None
    marker: bool | None = None
    if isinstance(header_placeholder, str) and header_placeholder.lower() in {"true", "false"}:
        marker = header_placeholder.lower() == "true"
    elif isinstance(body_placeholder, bool):
        marker = body_placeholder

    cpu_audio_engine = header_map.get("x-b1-cpu-audio-engine", "")
    if not cpu_audio_engine and isinstance(payload, dict):
        for key in ("b1_stt_engine", "b1_engine"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                cpu_audio_engine = value.strip()
                break

    runtime_name = str(runtime or "").strip().lower()
    reasons: list[str] = []
    if marker is True:
        reasons.append("explicit_placeholder_marker")
    if runtime_name == "audio-cpu" and marker is not False:
        reasons.append("audio_cpu_non_placeholder_marker_missing")
    if cpu_audio_engine.strip().lower() == "scaffold":
        reasons.append("scaffold_cpu_audio_engine")
    return {
        "placeholder": marker,
        "runtime": runtime_name or None,
        "cpu_audio_engine": cpu_audio_engine or None,
        "placeholder_failure": bool(reasons),
        "reasons": reasons,
    }


@unittest.skipUnless(os.getenv("B1_WORKFLOWS_LIVE_TEST") == "1", "set B1_WORKFLOWS_LIVE_TEST=1 to run installed workflow acceptance")
class LiveInstalledWorkflowAcceptanceTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    model_measurements: dict[str, dict[str, Any]] = {}
    required_model_aliases: set[str] = set()
    media_artifact_proofs: dict[str, dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.model_measurements = {}
        cls.required_model_aliases = set()
        cls.media_artifact_proofs = {}
        api_key = os.getenv("B1_WORKFLOWS_API_KEY") or os.getenv("B1_SMOKE_ADMIN_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or ""
        if not api_key:
            raise unittest.SkipTest("set B1_WORKFLOWS_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
        tls_verify = os.getenv("B1_WORKFLOWS_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        cls.client = LiveApiClient(
            os.getenv("B1_WORKFLOWS_API_BASE") or os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_WORKFLOWS_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_WORKFLOWS_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "30"))),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_WORKFLOWS_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.job_timeout_seconds = float(os.getenv("B1_WORKFLOWS_JOB_TIMEOUT_SECONDS", "900"))
        cls.allow_placeholder = env_flag("B1_WORKFLOWS_ALLOW_PLACEHOLDER", False)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_WORKFLOWS_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in INSTALLED_WORKFLOWS_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": INSTALLED_WORKFLOWS_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.client.base_url,
                    "status": status,
                    "required_checks": list(INSTALLED_WORKFLOWS_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
                    "required_model_aliases": sorted(cls.required_model_aliases),
                    "model_measurements": cls.model_measurements,
                    "media_artifacts": cls.media_artifact_proofs,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def response_header(self, headers: dict[str, str], name: str) -> str:
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
        return ""

    def require_measured_model(self, alias: str, *, expected_runtime: str | None = None) -> dict[str, Any]:
        self.__class__.required_model_aliases.add(alias)
        existing = self.__class__.model_measurements.get(alias)
        if existing is not None:
            if expected_runtime and existing.get("runtime") != expected_runtime:
                raise AssertionError(f"cached measurement for {alias!r} does not match expected runtime {expected_runtime!r}: {existing}")
            return existing
        measurement = measured_model_alias(self.client, alias, expected_runtime=expected_runtime)
        self.__class__.model_measurements[alias] = measurement
        return measurement

    def placeholder_check_status(self, proof: dict[str, Any]) -> str:
        return "incomplete" if proof.get("placeholder_failure") else "ok"

    def raise_if_disallowed_placeholder(self, proof: dict[str, Any], label: str) -> None:
        if proof.get("placeholder_failure") and not self.allow_placeholder:
            reason = ", ".join(str(item) for item in proof.get("reasons") or []) or "placeholder output"
            raise AssertionError(
                f"{label} returned placeholder or unproven output ({reason}); install a real model/runtime before acceptance"
            )

    def test_chat_audio_image_edit_and_video_installed_workflows(self) -> None:
        self.verify_chat()
        self.verify_tts()
        self.verify_stt()
        self.verify_cpu_audio_does_not_take_gpu_lease()
        self.verify_media_job(
            "image_generation_completed",
            "image-generation",
            self.media_job_body(
                "B1_WORKFLOWS_IMAGE_JOB_JSON",
                "B1_WORKFLOWS_IMAGE_JOB_FILE",
                modality="image",
                operation=os.getenv("B1_WORKFLOWS_IMAGE_OPERATION", "generation"),
                model=os.getenv("B1_WORKFLOWS_IMAGE_MODEL", "image-default"),
                priority="single_image",
                input_payload=load_json_from_env("B1_WORKFLOWS_IMAGE_INPUT_JSON", "B1_WORKFLOWS_IMAGE_INPUT_FILE")
                or {"prompt": os.getenv("B1_WORKFLOWS_IMAGE_PROMPT", "B1 AI Hub acceptance image.")},
            ),
        )
        self.verify_media_job(
            "image_edit_completed",
            "image-edit",
            self.media_job_body(
                "B1_WORKFLOWS_IMAGE_EDIT_JOB_JSON",
                "B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE",
                modality="image",
                operation=os.getenv("B1_WORKFLOWS_IMAGE_EDIT_OPERATION", "edit"),
                model=os.getenv("B1_WORKFLOWS_IMAGE_EDIT_MODEL", "image-edit"),
                priority="single_image",
                input_payload=load_json_from_env("B1_WORKFLOWS_IMAGE_EDIT_INPUT_JSON", "B1_WORKFLOWS_IMAGE_EDIT_INPUT_FILE")
                or {"prompt": os.getenv("B1_WORKFLOWS_IMAGE_EDIT_PROMPT", "B1 AI Hub acceptance image edit.")},
            ),
        )
        self.verify_media_job(
            "short_video_completed",
            "short-video",
            self.media_job_body(
                "B1_WORKFLOWS_VIDEO_JOB_JSON",
                "B1_WORKFLOWS_VIDEO_JOB_FILE",
                modality="video",
                operation=os.getenv("B1_WORKFLOWS_VIDEO_OPERATION", "generation"),
                model=os.getenv("B1_WORKFLOWS_VIDEO_MODEL", "video-text"),
                priority="video",
                input_payload=load_json_from_env("B1_WORKFLOWS_VIDEO_INPUT_JSON", "B1_WORKFLOWS_VIDEO_INPUT_FILE")
                or {
                    "prompt": os.getenv("B1_WORKFLOWS_VIDEO_PROMPT", "B1 AI Hub short acceptance clip."),
                    "frames": int(os.getenv("B1_WORKFLOWS_VIDEO_FRAMES", "8")),
                    "duration_seconds": float(os.getenv("B1_WORKFLOWS_VIDEO_DURATION_SECONDS", "1")),
                },
            ),
        )
        self.verify_media_artifacts_verified()

    def verify_chat(self) -> None:
        model = os.getenv("B1_WORKFLOWS_CHAT_MODEL", "chat-default")
        measurement = self.require_measured_model(model)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": os.getenv("B1_WORKFLOWS_CHAT_PROMPT", "Reply with exactly: ready")}],
            "max_tokens": int(os.getenv("B1_WORKFLOWS_CHAT_MAX_TOKENS", "16")),
            "temperature": 0,
            "runtime_policy": os.getenv("B1_WORKFLOWS_CHAT_RUNTIME_POLICY", "non_comfy_only"),
        }
        status, headers, payload = self.client.json_request("POST", "/v1/chat/completions", body=body, require_auth=True)
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        choices = payload.get("choices")
        self.assertIsInstance(choices, list)
        self.assertGreater(len(choices), 0, payload)
        runtime = payload.get("b1_runtime") or measurement.get("runtime")
        proof = placeholder_proof(headers, payload, runtime=str(runtime or ""))
        resolved_model = payload.get("b1_resolved_model") if isinstance(payload.get("b1_resolved_model"), str) else measurement.get("resolved_model_version")
        self.assertEqual(resolved_model, measurement.get("resolved_model_version"), payload)
        self.record_check(
            "chat_completed",
            self.placeholder_check_status(proof),
            model=model,
            resolved_model_version=measurement.get("resolved_model_version"),
            runtime=runtime,
            model_measurement=measurement,
            placeholder_proof=proof,
            choice_count=len(choices),
        )
        self.samples.append(
            {
                "label": "chat",
                "model": model,
                "resolved_model_version": measurement.get("resolved_model_version"),
                "runtime": runtime,
                "choice_count": len(choices),
            }
        )
        self.raise_if_disallowed_placeholder(proof, "chat")

    def verify_tts(self) -> None:
        model = os.getenv("B1_WORKFLOWS_TTS_MODEL", "tts-fast")
        measurement = self.require_measured_model(model)
        body = {
            "model": model,
            "input": os.getenv("B1_WORKFLOWS_TTS_TEXT", "B1 AI Hub installed workflow acceptance speech."),
            "voice": os.getenv("B1_WORKFLOWS_TTS_VOICE", "default"),
            "response_format": os.getenv("B1_WORKFLOWS_TTS_FORMAT", "wav"),
            "runtime_policy": os.getenv("B1_WORKFLOWS_TTS_RUNTIME_POLICY", "any"),
        }
        status, headers, content = self.client.request("POST", "/v1/audio/speech", body=body, headers={"Accept": "audio/*"}, require_auth=True)
        self.assertEqual(status, 200, content[:200])
        self.assertGreater(len(content), 0, "TTS returned an empty response")
        proof = placeholder_proof(headers, None, runtime=str(measurement.get("runtime") or ""))
        digest = hashlib.sha256(content).hexdigest()
        self.record_check(
            "tts_completed",
            self.placeholder_check_status(proof),
            model=model,
            resolved_model_version=measurement.get("resolved_model_version"),
            runtime=measurement.get("runtime"),
            model_measurement=measurement,
            placeholder_proof=proof,
            byte_count=len(content),
            sha256=digest,
        )
        self.samples.append(
            {
                "label": "tts",
                "model": model,
                "resolved_model_version": measurement.get("resolved_model_version"),
                "runtime": measurement.get("runtime"),
                "byte_count": len(content),
                "sha256": digest,
            }
        )
        self.raise_if_disallowed_placeholder(proof, "tts")

    def verify_stt(self) -> None:
        model = os.getenv("B1_WORKFLOWS_STT_MODEL", "stt-default")
        measurement = self.require_measured_model(model)
        body = {
            "model": model,
            "audio": os.getenv("B1_WORKFLOWS_STT_AUDIO_BASE64", "") or silence_wav_base64(),
            "audio_mime_type": os.getenv("B1_WORKFLOWS_STT_AUDIO_MIME_TYPE", "audio/wav"),
            "runtime_policy": os.getenv("B1_WORKFLOWS_STT_RUNTIME_POLICY", "any"),
        }
        status, headers, payload = self.client.json_request("POST", "/v1/audio/transcriptions", body=body, require_auth=True)
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        self.assertIn("text", payload)
        proof = placeholder_proof(headers, payload, runtime=str(measurement.get("runtime") or ""))
        text_length = len(str(payload.get("text") or ""))
        self.record_check(
            "stt_completed",
            self.placeholder_check_status(proof),
            model=model,
            resolved_model_version=measurement.get("resolved_model_version"),
            runtime=measurement.get("runtime"),
            model_measurement=measurement,
            placeholder_proof=proof,
            text_length=text_length,
        )
        self.samples.append(
            {
                "label": "stt",
                "model": model,
                "resolved_model_version": measurement.get("resolved_model_version"),
                "runtime": measurement.get("runtime"),
                "text_length": text_length,
            }
        )
        self.raise_if_disallowed_placeholder(proof, "stt")

    def scheduler_lease_snapshot(self) -> dict[str, Any]:
        status, _, payload = self.client.json_request("GET", "/admin/scheduler/lease", require_auth=True)
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        lease = payload.get("lease")
        if lease is None:
            return {}
        self.assertIsInstance(lease, dict)
        return lease

    def lease_owner(self, lease: dict[str, Any]) -> str:
        owner = lease.get("owner")
        return str(owner) if owner else ""

    def verify_cpu_audio_does_not_take_gpu_lease(self) -> None:
        tts_model = os.getenv("B1_WORKFLOWS_CPU_TTS_MODEL", os.getenv("B1_WORKFLOWS_TTS_MODEL", "tts-fast"))
        stt_model = os.getenv("B1_WORKFLOWS_CPU_STT_MODEL", os.getenv("B1_WORKFLOWS_STT_MODEL", "stt-default"))
        tts_measurement = self.require_measured_model(tts_model, expected_runtime="audio-cpu")
        stt_measurement = self.require_measured_model(stt_model, expected_runtime="audio-cpu")
        runtime_policy = os.getenv("B1_WORKFLOWS_CPU_AUDIO_RUNTIME_POLICY", "non_comfy_only")
        before = self.scheduler_lease_snapshot()

        status, tts_headers, tts_content = self.client.request(
            "POST",
            "/v1/audio/speech",
            body={
                "model": tts_model,
                "input": os.getenv("B1_WORKFLOWS_CPU_TTS_TEXT", "B1 AI Hub CPU audio lease acceptance speech."),
                "voice": os.getenv("B1_WORKFLOWS_CPU_TTS_VOICE", "default"),
                "response_format": os.getenv("B1_WORKFLOWS_CPU_TTS_FORMAT", "wav"),
                "runtime_policy": runtime_policy,
            },
            headers={"Accept": "audio/*"},
            require_auth=True,
        )
        self.assertEqual(status, 200, tts_content[:200])
        self.assertGreater(len(tts_content), 0, "CPU TTS returned an empty response")
        tts_header_map = {key.lower(): value for key, value in tts_headers.items()}
        self.assertEqual(str(tts_header_map.get("x-b1-gpu-lease-required", "")).lower(), "false", tts_header_map)
        tts_proof = placeholder_proof(tts_headers, None, runtime="audio-cpu")

        status, stt_headers, stt_payload = self.client.json_request(
            "POST",
            "/v1/audio/transcriptions",
            body={
                "model": stt_model,
                "audio": os.getenv("B1_WORKFLOWS_CPU_STT_AUDIO_BASE64", "") or silence_wav_base64(),
                "audio_mime_type": os.getenv("B1_WORKFLOWS_CPU_STT_AUDIO_MIME_TYPE", "audio/wav"),
                "runtime_policy": runtime_policy,
            },
            require_auth=True,
        )
        self.assertEqual(status, 200, stt_payload)
        self.assertIsInstance(stt_payload, dict)
        self.assertIs(stt_payload.get("gpu_lease_required"), False, stt_payload)
        stt_proof = placeholder_proof(stt_headers, stt_payload, runtime="audio-cpu")

        after = self.scheduler_lease_snapshot()
        before_owner = self.lease_owner(before)
        after_owner = self.lease_owner(after)
        self.assertEqual(
            after_owner,
            before_owner,
            {
                "message": "CPU audio acceptance changed the GPU scheduler owner",
                "before": before,
                "after": after,
            },
        )
        self.record_check(
            "cpu_audio_does_not_take_gpu_lease",
            "incomplete" if tts_proof.get("placeholder_failure") or stt_proof.get("placeholder_failure") else "ok",
            tts_model=tts_model,
            stt_model=stt_model,
            tts_resolved_model_version=tts_measurement.get("resolved_model_version"),
            stt_resolved_model_version=stt_measurement.get("resolved_model_version"),
            model_measurements={"tts": tts_measurement, "stt": stt_measurement},
            tts_placeholder_proof=tts_proof,
            stt_placeholder_proof=stt_proof,
            runtime_policy=runtime_policy,
            scheduler_owner_before=before_owner or "none",
            scheduler_owner_after=after_owner or "none",
            tts_gpu_lease_required=tts_header_map.get("x-b1-gpu-lease-required"),
            stt_gpu_lease_required=stt_payload.get("gpu_lease_required"),
            tts_byte_count=len(tts_content),
            stt_text_length=len(str(stt_payload.get("text") or "")),
        )
        self.samples.append(
            {
                "label": "cpu-audio-no-gpu-lease",
                "tts_model": tts_model,
                "stt_model": stt_model,
                "tts_resolved_model_version": tts_measurement.get("resolved_model_version"),
                "stt_resolved_model_version": stt_measurement.get("resolved_model_version"),
                "scheduler_owner_before": before_owner or "none",
                "scheduler_owner_after": after_owner or "none",
            }
        )
        self.raise_if_disallowed_placeholder(tts_proof, "cpu-tts")
        self.raise_if_disallowed_placeholder(stt_proof, "cpu-stt")

    def media_job_body(
        self,
        json_env: str,
        file_env: str,
        *,
        modality: str,
        operation: str,
        model: str,
        priority: str,
        input_payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = load_json_from_env(json_env, file_env)
        if body is not None:
            return body
        return {
            "modality": modality,
            "operation": operation,
            "model": model,
            "priority": priority,
            "runtime_policy": "any",
            "input": input_payload,
        }

    def verify_media_job(self, check_name: str, label: str, body: dict[str, Any]) -> None:
        model = body.get("model")
        measurement = self.require_measured_model(str(model)) if isinstance(model, str) and model else {}
        status, _, job = self.client.json_request(
            "POST",
            "/v1/media/jobs",
            body=body,
            headers={"Idempotency-Key": f"workflow-{label}-{uuid.uuid4().hex}"},
            require_auth=True,
        )
        self.assertEqual(status, 202, job)
        self.assertIsInstance(job, dict)
        assert_media_job_links(self, job)
        final_job = self.wait_for_terminal_job(job)
        assert_media_job_links(self, final_job)
        self.assertEqual(final_job.get("state"), "completed", final_job)
        if measurement:
            self.assertEqual(final_job.get("resolved_model_version"), measurement.get("resolved_model_version"), final_job)
            self.assertEqual(final_job.get("runtime"), measurement.get("runtime"), final_job)
        status, _, artifact_payload = self.client.json_request("GET", media_job_link(final_job, "artifacts", "/artifacts"), require_auth=True)
        self.assertEqual(status, 200, artifact_payload)
        artifacts = artifact_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0, artifact_payload)
        artifact = artifacts[0]
        artifact_url = artifact.get("url")
        self.assertIsInstance(artifact_url, str)
        self.assertTrue(artifact_url.startswith("/artifacts/"), artifact)
        artifact_mime_type = artifact.get("mime_type")
        self.assertIsInstance(artifact_mime_type, str, artifact)
        self.assertTrue(artifact_mime_type, artifact)
        artifact_bytes = artifact.get("bytes")
        self.assertIsInstance(artifact_bytes, int, artifact)
        self.assertGreater(artifact_bytes, 0, artifact)
        artifact_sha256 = artifact.get("sha256")
        self.assertIsInstance(artifact_sha256, str, artifact)
        self.assertRegex(artifact_sha256, r"^[a-f0-9]{64}$", artifact)
        status, headers, content = self.client.request("GET", artifact_url, headers={"Accept": "*/*"}, require_auth=True)
        self.assertEqual(status, 200, content[:200])
        self.assertGreater(len(content), 0)
        digest = hashlib.sha256(content).hexdigest()
        self.assertEqual(artifact_bytes, len(content), artifact)
        self.assertEqual(artifact_sha256, digest, artifact)
        content_type_header = self.response_header(headers, "content-type")
        content_length_header = self.response_header(headers, "content-length")
        etag_header = self.response_header(headers, "etag")
        accept_ranges_header = self.response_header(headers, "accept-ranges")
        self.assertTrue(content_type_header, headers)
        self.assertEqual(content_length_header, str(len(content)), headers)
        self.assertTrue(etag_header, headers)
        self.assertEqual(accept_ranges_header.lower(), "bytes", headers)
        self.__class__.media_artifact_proofs[label] = {
            "job_id": str(job["id"]),
            "artifact_url": artifact_url,
            "artifact_id": str(artifact.get("id") or ""),
            "artifact_kind": str(artifact.get("kind") or ""),
            "artifact_mime_type": artifact_mime_type,
            "artifact_bytes": artifact_bytes,
            "artifact_sha256": artifact_sha256,
            "download_content_type": content_type_header,
            "download_content_length": content_length_header,
            "download_etag": etag_header,
            "download_accept_ranges": accept_ranges_header,
        }
        self.record_check(
            check_name,
            job_id=job["id"],
            model=body.get("model"),
            resolved_model_version=final_job.get("resolved_model_version"),
            runtime=final_job.get("runtime"),
            modality=body.get("modality"),
            operation=body.get("operation"),
            load_time_ms=final_job.get("load_time_ms"),
            run_time_ms=final_job.get("run_time_ms"),
            peak_vram_mib=final_job.get("peak_vram_mib"),
            peak_ram_mib=final_job.get("peak_ram_mib"),
            model_measurement=measurement,
            job_links=final_job.get("links"),
            artifact_count=len(artifacts),
            first_artifact_bytes=len(content),
            first_artifact_sha256=digest,
            first_artifact_mime_type=artifact_mime_type,
            first_artifact_url=artifact_url,
            content_type_header=content_type_header,
            content_length_header=content_length_header,
            etag_header=etag_header,
            accept_ranges_header=accept_ranges_header,
        )
        self.samples.append(
            {
                "label": label,
                "job_id": job["id"],
                "model": body.get("model"),
                "resolved_model_version": final_job.get("resolved_model_version"),
                "runtime": final_job.get("runtime"),
                "load_time_ms": final_job.get("load_time_ms"),
                "run_time_ms": final_job.get("run_time_ms"),
                "peak_vram_mib": final_job.get("peak_vram_mib"),
                "peak_ram_mib": final_job.get("peak_ram_mib"),
                "job_links": final_job.get("links"),
                "artifact_count": len(artifacts),
                "first_artifact_bytes": len(content),
                "first_artifact_sha256": digest,
                "first_artifact_mime_type": artifact_mime_type,
            }
        )

    def verify_media_artifacts_verified(self) -> None:
        required_labels = ("image-generation", "image-edit", "short-video")
        missing = [label for label in required_labels if label not in self.media_artifact_proofs]
        if missing:
            raise AssertionError(f"missing artifact verification for media workflows: {missing}")
        self.record_check(
            "media_artifacts_verified",
            workflow_labels=list(required_labels),
            artifact_count=len(required_labels),
            artifacts={label: self.media_artifact_proofs[label] for label in required_labels},
        )

    def wait_for_terminal_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        deadline = time.monotonic() + self.job_timeout_seconds
        last: dict[str, Any] | None = job
        while time.monotonic() < deadline:
            status, _, payload = self.client.json_request("GET", media_job_link(last or job, "self"), require_auth=True)
            self.assertEqual(status, 200, payload)
            self.assertIsInstance(payload, dict)
            last = payload
            if payload.get("state") in TERMINAL_STATES:
                return payload
            time.sleep(2)
        raise AssertionError(f"job {job_id} did not reach a terminal state before timeout; last={last}")


if __name__ == "__main__":
    unittest.main()
