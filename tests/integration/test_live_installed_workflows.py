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

from test_live_stack import LiveApiClient, TERMINAL_STATES  # noqa: E402


INSTALLED_WORKFLOWS_EVIDENCE_FORMAT = "b1-ai-hub-installed-workflows-acceptance/v1"
INSTALLED_WORKFLOWS_REQUIRED_CHECKS = (
    "chat_completed",
    "tts_completed",
    "stt_completed",
    "cpu_audio_does_not_take_gpu_lease",
    "image_generation_completed",
    "image_edit_completed",
    "short_video_completed",
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


@unittest.skipUnless(os.getenv("B1_WORKFLOWS_LIVE_TEST") == "1", "set B1_WORKFLOWS_LIVE_TEST=1 to run installed workflow acceptance")
class LiveInstalledWorkflowAcceptanceTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
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

    def assert_not_placeholder(self, headers: dict[str, str], payload: Any, label: str) -> None:
        header_map = {key.lower(): value for key, value in headers.items()}
        header_placeholder = str(header_map.get("x-b1-placeholder", "")).lower()
        body_placeholder = payload.get("b1_placeholder") if isinstance(payload, dict) else None
        placeholder = header_placeholder == "true" or body_placeholder is True
        if placeholder and not self.allow_placeholder:
            raise AssertionError(f"{label} returned placeholder output; install a real model/runtime before acceptance")

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

    def verify_chat(self) -> None:
        model = os.getenv("B1_WORKFLOWS_CHAT_MODEL", "chat-default")
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
        self.assert_not_placeholder(headers, payload, "chat")
        self.record_check("chat_completed", model=model, choice_count=len(choices))
        self.samples.append({"label": "chat", "model": model, "choice_count": len(choices)})

    def verify_tts(self) -> None:
        model = os.getenv("B1_WORKFLOWS_TTS_MODEL", "tts-fast")
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
        self.assert_not_placeholder(headers, None, "tts")
        digest = hashlib.sha256(content).hexdigest()
        self.record_check("tts_completed", model=model, byte_count=len(content), sha256=digest)
        self.samples.append({"label": "tts", "model": model, "byte_count": len(content), "sha256": digest})

    def verify_stt(self) -> None:
        model = os.getenv("B1_WORKFLOWS_STT_MODEL", "stt-default")
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
        self.assert_not_placeholder(headers, payload, "stt")
        text_length = len(str(payload.get("text") or ""))
        self.record_check("stt_completed", model=model, text_length=text_length)
        self.samples.append({"label": "stt", "model": model, "text_length": text_length})

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
        self.assert_not_placeholder(tts_headers, None, "cpu-tts")

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
        self.assert_not_placeholder(stt_headers, stt_payload, "cpu-stt")

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
            tts_model=tts_model,
            stt_model=stt_model,
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
                "scheduler_owner_before": before_owner or "none",
                "scheduler_owner_after": after_owner or "none",
            }
        )

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
        status, _, job = self.client.json_request(
            "POST",
            "/v1/media/jobs",
            body=body,
            headers={"Idempotency-Key": f"workflow-{label}-{uuid.uuid4().hex}"},
            require_auth=True,
        )
        self.assertEqual(status, 200, job)
        self.assertIsInstance(job, dict)
        final_job = self.wait_for_terminal_job(str(job["id"]))
        self.assertEqual(final_job.get("state"), "completed", final_job)
        status, _, artifact_payload = self.client.json_request("GET", f"/v1/media/jobs/{job['id']}/artifacts", require_auth=True)
        self.assertEqual(status, 200, artifact_payload)
        artifacts = artifact_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0, artifact_payload)
        artifact = artifacts[0]
        artifact_url = artifact.get("url")
        self.assertIsInstance(artifact_url, str)
        status, headers, content = self.client.request("GET", artifact_url, headers={"Accept": "*/*"}, require_auth=True)
        self.assertEqual(status, 200, content[:200])
        self.assertGreater(len(content), 0)
        digest = hashlib.sha256(content).hexdigest()
        self.record_check(
            check_name,
            job_id=job["id"],
            model=body.get("model"),
            runtime=final_job.get("runtime"),
            modality=body.get("modality"),
            operation=body.get("operation"),
            artifact_count=len(artifacts),
            first_artifact_bytes=len(content),
            first_artifact_sha256=digest,
            content_length_header=headers.get("Content-Length") or headers.get("content-length"),
        )
        self.samples.append(
            {
                "label": label,
                "job_id": job["id"],
                "model": body.get("model"),
                "runtime": final_job.get("runtime"),
                "artifact_count": len(artifacts),
                "first_artifact_bytes": len(content),
                "first_artifact_sha256": digest,
            }
        )

    def wait_for_terminal_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.job_timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status, _, payload = self.client.json_request("GET", f"/v1/media/jobs/{job_id}", require_auth=True)
            self.assertEqual(status, 200, payload)
            self.assertIsInstance(payload, dict)
            last = payload
            if payload.get("state") in TERMINAL_STATES:
                return payload
            time.sleep(2)
        raise AssertionError(f"job {job_id} did not reach a terminal state before timeout; last={last}")


if __name__ == "__main__":
    unittest.main()
