from __future__ import annotations

import json
import os
import sys
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from test_live_stack import LiveApiClient, TERMINAL_STATES  # noqa: E402


GPU_RUNTIMES = {"localai", "comfyui", "voicebox"}


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


@unittest.skipUnless(os.getenv("B1_GPU_ACCEPTANCE_LIVE_TEST") == "1", "set B1_GPU_ACCEPTANCE_LIVE_TEST=1 to run live RTX GPU acceptance checks")
class LiveCrossRuntimeGpuAcceptanceTests(unittest.TestCase):
    evidence: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        api_key = os.getenv("B1_GPU_ACCEPTANCE_API_KEY") or os.getenv("B1_SMOKE_ADMIN_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or ""
        if not api_key:
            raise unittest.SkipTest("set B1_GPU_ACCEPTANCE_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
        tls_verify = os.getenv("B1_GPU_ACCEPTANCE_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        cls.client = LiveApiClient(
            os.getenv("B1_GPU_ACCEPTANCE_API_BASE") or os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_GPU_ACCEPTANCE_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_GPU_ACCEPTANCE_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "30"))),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_GPU_ACCEPTANCE_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.job_timeout_seconds = float(os.getenv("B1_GPU_ACCEPTANCE_JOB_TIMEOUT_SECONDS", "900"))
        cls.vram_tolerance_mib = int(os.getenv("B1_GPU_ACCEPTANCE_VRAM_TOLERANCE_MIB", "256"))
        cls.require_production = env_flag("B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION", True)
        cls.enforce_vram_reserve = env_flag("B1_GPU_ACCEPTANCE_ENFORCE_VRAM_RESERVE", True)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_GPU_ACCEPTANCE_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.client.base_url,
                    "samples": cls.evidence,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def json_request(self, method: str, path: str, *, body: dict[str, Any] | None = None, expected: int = 200) -> dict[str, Any]:
        status, _, payload = self.client.json_request(method, path, body=body, require_auth=True)
        if status == 403:
            self.skipTest(f"provided acceptance key lacks scope for {method} {path}")
        self.assertEqual(status, expected, payload)
        self.assertIsInstance(payload, dict)
        return payload

    def admin_status(self) -> dict[str, Any]:
        return self.json_request("GET", "/admin/status")

    def admin_metrics(self) -> dict[str, Any]:
        return self.json_request("GET", "/admin/metrics")

    def admin_runtimes(self) -> dict[str, Any]:
        return self.json_request("GET", "/admin/runtimes")

    def active_gpu_runtime_states(self, runtimes: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        states = runtimes.get("runtime_states")
        self.assertIsInstance(states, dict)
        active: list[tuple[str, dict[str, Any]]] = []
        for runtime in sorted(GPU_RUNTIMES):
            state = states.get(runtime)
            if not isinstance(state, dict):
                continue
            if state.get("active_model") or state.get("resolved_model_version"):
                active.append((runtime, state))
        return active

    def assert_single_gpu_runtime(self, expected_runtime: str | None, label: str) -> dict[str, Any]:
        runtimes = self.admin_runtimes()
        active = self.active_gpu_runtime_states(runtimes)
        self.evidence.append({"label": label, "runtime_states": runtimes.get("runtime_states"), "active_gpu_runtimes": [item[0] for item in active]})
        self.assertLessEqual(len(active), 1, f"{label}: more than one GPU runtime reports a resident model/pipeline: {active}")
        if expected_runtime is not None:
            self.assertEqual([runtime for runtime, _ in active], [expected_runtime], f"{label}: expected only {expected_runtime} to remain GPU-resident")
        return runtimes

    def assert_vram_within_policy(self, label: str) -> None:
        status = self.admin_status()
        policy = status.get("resource_policy")
        self.assertIsInstance(policy, dict)
        reserve_mib = int(float(policy.get("gpu_reserve_vram_gib", 0)) * 1024)
        usable_mib = int(float(policy.get("gpu_usable_vram_gib", 0)) * 1024)
        metrics = self.admin_metrics()
        gpu = metrics.get("gpu") or {}
        self.evidence.append({"label": f"{label}:metrics", "gpu": gpu, "jobs": metrics.get("jobs"), "scheduler_lease": metrics.get("scheduler_lease")})
        if not self.enforce_vram_reserve:
            return
        if not gpu.get("available"):
            self.fail(f"{label}: runtime-agent GPU/NVML metrics are required for RTX acceptance")
        used = gpu.get("memory_used_mib")
        total = gpu.get("memory_total_mib")
        if isinstance(used, (int, float)) and isinstance(total, (int, float)) and reserve_mib:
            self.assertLessEqual(
                int(used),
                int(total) - reserve_mib + self.vram_tolerance_mib,
                f"{label}: current VRAM usage exceeds total-minus-reserve policy",
            )
        jobs = metrics.get("jobs") or {}
        peak = jobs.get("peak_vram_mib") or {}
        peak_max = peak.get("max")
        if isinstance(peak_max, (int, float)) and usable_mib:
            self.assertLessEqual(
                int(peak_max),
                usable_mib + self.vram_tolerance_mib,
                f"{label}: sampled job peak VRAM exceeds usable VRAM policy",
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

    def create_media_job(self, body: dict[str, Any]) -> dict[str, Any]:
        status, _, job = self.client.json_request(
            "POST",
            "/v1/media/jobs",
            body=body,
            headers={"Idempotency-Key": f"gpu-acceptance-{uuid.uuid4().hex}"},
            require_auth=True,
        )
        self.assertEqual(status, 200, job)
        self.assertIsInstance(job, dict)
        return self.wait_for_terminal_job(str(job["id"]))

    def test_resource_policy_and_runtime_readiness_are_acceptance_safe(self) -> None:
        status = self.admin_status()
        policy = status.get("resource_policy")
        self.assertIsInstance(policy, dict)
        self.assertEqual(policy.get("gpu_max_active_pipelines"), 1)
        self.assertEqual(policy.get("llm_default_parallel_requests"), 1)
        self.assertEqual(policy.get("comfyui_maximum_parallel_jobs"), 1)
        self.assertEqual(policy.get("comfyui_maximum_batch_size"), 1)

        runtimes = self.admin_runtimes()
        readiness = runtimes.get("readiness")
        self.assertIsInstance(readiness, dict)
        if self.require_production:
            self.assertEqual(runtimes.get("runtime_deployment_mode"), "production", runtimes)
            self.assertEqual(readiness.get("status"), "ok", readiness)

        health_by_name = {item.get("name"): item for item in runtimes.get("health", []) if isinstance(item, dict)}
        for runtime in {"localai", "comfyui", "audio-cpu"}:
            self.assertIn(runtime, health_by_name)
            self.assertEqual(health_by_name[runtime].get("status"), "ok", health_by_name[runtime])

        self.assert_single_gpu_runtime(expected_runtime=None, label="initial-readiness")
        self.assert_vram_within_policy("initial-readiness")

    def test_llm_comfyui_voicebox_switch_sequence_keeps_one_gpu_pipeline(self) -> None:
        chat_model = os.getenv("B1_GPU_ACCEPTANCE_CHAT_MODEL", "chat-default")
        image_model = os.getenv("B1_GPU_ACCEPTANCE_COMFY_MODEL", "image-default")
        voicebox_model = os.getenv("B1_GPU_ACCEPTANCE_VOICEBOX_MODEL", "tts-quality")

        chat_body = {
            "model": chat_model,
            "messages": [{"role": "user", "content": os.getenv("B1_GPU_ACCEPTANCE_CHAT_PROMPT", "Reply with the word ready.")}],
            "max_tokens": int(os.getenv("B1_GPU_ACCEPTANCE_CHAT_MAX_TOKENS", "8")),
            "temperature": 0,
            "runtime_policy": "non_comfy_only",
        }
        status, _, chat = self.client.json_request("POST", "/v1/chat/completions", body=chat_body, require_auth=True)
        self.assertEqual(status, 200, chat)
        self.assert_single_gpu_runtime("localai", "after-localai-chat")
        self.assert_vram_within_policy("after-localai-chat")

        comfy_prompt = load_json_from_env("B1_GPU_ACCEPTANCE_COMFY_PROMPT_JSON", "B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE")
        if comfy_prompt is None:
            self.skipTest("set B1_GPU_ACCEPTANCE_COMFY_PROMPT_JSON or B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE for ComfyUI acceptance")
        comfy_job = self.create_media_job(
            {
                "modality": os.getenv("B1_GPU_ACCEPTANCE_COMFY_MODALITY", "image"),
                "operation": os.getenv("B1_GPU_ACCEPTANCE_COMFY_OPERATION", "generation"),
                "model": image_model,
                "priority": "single_image",
                "runtime_policy": "any",
                "input": {"comfyui_prompt": comfy_prompt},
            }
        )
        self.assertEqual(comfy_job.get("state"), "completed", comfy_job)
        self.assertEqual(comfy_job.get("runtime"), "comfyui", comfy_job)
        self.assert_single_gpu_runtime("comfyui", "after-comfyui-job")
        self.assert_vram_within_policy("after-comfyui-job")

        if env_flag("B1_GPU_ACCEPTANCE_SKIP_VOICEBOX", False):
            self.skipTest("B1_GPU_ACCEPTANCE_SKIP_VOICEBOX requested")
        voicebox_job = self.create_media_job(
            {
                "modality": "tts",
                "operation": "speech",
                "model": voicebox_model,
                "priority": "interactive_audio",
                "runtime_policy": "any",
                "input": {
                    "text": os.getenv("B1_GPU_ACCEPTANCE_VOICEBOX_TEXT", "B1 AI Hub GPU acceptance speech."),
                    "voice": os.getenv("B1_GPU_ACCEPTANCE_VOICEBOX_VOICE", "default"),
                },
            }
        )
        self.assertEqual(voicebox_job.get("state"), "completed", voicebox_job)
        self.assertEqual(voicebox_job.get("runtime"), "voicebox", voicebox_job)
        self.assert_single_gpu_runtime("voicebox", "after-voicebox-job")
        self.assert_vram_within_policy("after-voicebox-job")

    def test_predefined_runtime_recovery_action_is_available_when_enabled(self) -> None:
        if not env_flag("B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION", False):
            self.skipTest("set B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1 during a maintenance acceptance window")
        runtime = os.getenv("B1_GPU_ACCEPTANCE_RECOVERY_RUNTIME", "localai")
        self.assertIn(runtime, GPU_RUNTIMES)
        result = self.json_request(
            "POST",
            f"/admin/runtimes/{runtime}/recover",
            body={"reason": "cross-runtime GPU acceptance recovery probe", "timeout_seconds": 30},
        )
        self.evidence.append({"label": f"recover-{runtime}", "result": result})
        self.assertIn(result.get("status"), {"ok", "dry_run", "skipped", "unsupported", "unconfirmed"}, result)


if __name__ == "__main__":
    unittest.main()
