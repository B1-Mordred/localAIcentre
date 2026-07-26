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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from tests.support.evidence import write_private_json  # noqa: E402
from test_live_stack import (  # noqa: E402
    LiveApiClient,
    TERMINAL_STATES,
    assert_media_job_links,
    artifact_collection_proof,
    downloaded_artifact_proof,
    measured_model_alias,
    media_job_link,
)


GPU_RUNTIMES = {"localai", "comfyui", "voicebox"}
TINY_COMFYUI_SMOKE_CLASS = "B1RuntimeTinyImage"
GPU_ACCEPTANCE_REQUIRED_CHECKS = (
    "resource_policy_and_runtime_readiness",
    "localai_exclusive_gpu_residency",
    "comfyui_switch_completed",
    "voicebox_switch_completed",
    "localai_comfyui_voicebox_switch",
    "vram_reserve_enforced",
    "bounded_runtime_recovery_action",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def contains_class_type(value: Any, class_type: str) -> bool:
    if isinstance(value, dict):
        if value.get("class_type") == class_type:
            return True
        return any(contains_class_type(item, class_type) for item in value.values())
    if isinstance(value, list):
        return any(contains_class_type(item, class_type) for item in value)
    return False


def prompt_class_types(payload: dict[str, Any]) -> list[str]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, dict):
        return []
    class_types = []
    for node in prompt.values():
        if isinstance(node, dict) and isinstance(node.get("class_type"), str):
            class_types.append(node["class_type"])
    return sorted(set(class_types))


def prompt_metadata(payload: dict[str, Any], *, source: str, file_path: str = "") -> dict[str, Any]:
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), dict) else {}
    class_types = prompt_class_types(payload)
    return {
        "source": source,
        "file_path": file_path,
        "file_name": Path(file_path).name if file_path else "",
        "node_count": len(prompt),
        "class_type_count": len(class_types),
        "class_types": class_types[:50],
        "route_level_smoke": contains_class_type(payload, TINY_COMFYUI_SMOKE_CLASS),
        "tiny_smoke_class": TINY_COMFYUI_SMOKE_CLASS,
    }


def normalize_comfy_prompt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "prompt" not in payload:
        payload = {"prompt": payload}
    prompt = payload.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        raise AssertionError("ComfyUI prompt payload must contain a non-empty prompt object")
    return payload


def load_comfy_prompt_payload_with_metadata() -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw = os.getenv("B1_GPU_ACCEPTANCE_COMFY_PROMPT_JSON", "").strip()
    file_path = os.getenv("B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE", "").strip()
    if raw and file_path:
        raise unittest.SkipTest("set only one of B1_GPU_ACCEPTANCE_COMFY_PROMPT_JSON or B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE")
    source = "env-json" if raw else "env-file"
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8")
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AssertionError("B1 GPU acceptance ComfyUI prompt must decode to a JSON object")
    payload = normalize_comfy_prompt_payload(payload)
    return payload, prompt_metadata(payload, source=source, file_path=file_path)


@unittest.skipUnless(os.getenv("B1_GPU_ACCEPTANCE_LIVE_TEST") == "1", "set B1_GPU_ACCEPTANCE_LIVE_TEST=1 to run live RTX GPU acceptance checks")
class LiveCrossRuntimeGpuAcceptanceTests(unittest.TestCase):
    evidence: list[dict[str, Any]] = []
    checks: dict[str, dict[str, Any]] = {}
    model_measurements: dict[str, dict[str, Any]] = {}
    required_model_aliases: set[str] = set()
    vram_samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = []
        cls.checks = {}
        cls.model_measurements = {}
        cls.required_model_aliases = set()
        cls.vram_samples = []
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
        cls.allow_comfy_tiny_smoke = env_flag("B1_GPU_ACCEPTANCE_ALLOW_COMFY_TINY_SMOKE", False)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_GPU_ACCEPTANCE_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in GPU_ACCEPTANCE_REQUIRED_CHECKS) else "incomplete"
        write_private_json(
            path,
            {
                "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "base_url": cls.client.base_url,
                "status": status,
                "required_checks": list(GPU_ACCEPTANCE_REQUIRED_CHECKS),
                "checks": cls.checks,
                "samples": cls.evidence,
                "vram_samples": cls.vram_samples,
                "required_model_aliases": sorted(cls.required_model_aliases),
                "model_measurements": cls.model_measurements,
            },
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

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

    def runtime_state_proof(self, runtimes: dict[str, Any], expected_runtime: str | None, label: str) -> dict[str, Any]:
        states = runtimes.get("runtime_states")
        self.assertIsInstance(states, dict)
        active = self.active_gpu_runtime_states(runtimes)
        active_names = [item[0] for item in active]
        expected_resident = expected_runtime is None or active_names == [expected_runtime]
        return {
            "label": label,
            "expected_runtime": expected_runtime,
            "active_gpu_runtimes": active_names,
            "active_gpu_runtime_count": len(active_names),
            "no_split_brain": len(active_names) <= 1,
            "expected_runtime_resident": expected_resident,
            "runtime_states": {runtime: states.get(runtime) for runtime in sorted(GPU_RUNTIMES) if runtime in states},
        }

    def assert_single_gpu_runtime(self, expected_runtime: str | None, label: str) -> dict[str, Any]:
        runtimes = self.admin_runtimes()
        proof = self.runtime_state_proof(runtimes, expected_runtime, label)
        self.evidence.append(proof)
        active = proof["active_gpu_runtimes"]
        self.assertLessEqual(len(active), 1, f"{label}: more than one GPU runtime reports a resident model/pipeline: {active}")
        if expected_runtime is not None:
            self.assertEqual(active, [expected_runtime], f"{label}: expected only {expected_runtime} to remain GPU-resident")
        return proof

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
        configured_total_mib = int(float(policy.get("gpu_total_vram_gib", 0)) * 1024)
        if isinstance(total, (int, float)) and configured_total_mib:
            self.assertGreaterEqual(
                int(total) + self.vram_tolerance_mib,
                configured_total_mib,
                f"{label}: physical GPU VRAM is below configured policy total",
            )
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
        sample = {
            "label": label,
            "gpu_memory_used_mib": used,
            "gpu_memory_total_mib": total,
            "reserve_mib": reserve_mib,
            "usable_mib": usable_mib,
            "job_peak_vram_mib": peak_max,
        }
        self.__class__.vram_samples.append(sample)
        self.record_check("vram_reserve_enforced", sample_count=len(self.__class__.vram_samples), latest_sample=sample, samples=list(self.__class__.vram_samples))

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

    def create_media_job(self, body: dict[str, Any]) -> dict[str, Any]:
        status, _, job = self.client.json_request(
            "POST",
            "/v1/media/jobs",
            body=body,
            headers={"Idempotency-Key": f"gpu-acceptance-{uuid.uuid4().hex}"},
            require_auth=True,
        )
        self.assertEqual(status, 202, job)
        self.assertIsInstance(job, dict)
        assert_media_job_links(self, job)
        final_job = self.wait_for_terminal_job(job)
        assert_media_job_links(self, final_job)
        return final_job

    def verify_artifact_download(self, artifact: dict[str, Any], index: int) -> dict[str, Any]:
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
        proof = downloaded_artifact_proof(artifact, headers, content, index=index)
        self.assertEqual(proof["download_bytes"], artifact_bytes, artifact)
        self.assertEqual(proof["download_sha256"], artifact_sha256, artifact)
        self.assertTrue(proof["download_content_type"], headers)
        self.assertEqual(proof["download_content_length"], str(artifact_bytes), headers)
        self.assertTrue(proof["download_etag"], headers)
        self.assertEqual(str(proof["download_accept_ranges"]).lower(), "bytes", headers)
        return proof

    def verified_media_job_artifacts(self, job: dict[str, Any]) -> dict[str, Any]:
        status, _, artifact_payload = self.client.json_request("GET", media_job_link(job, "artifacts", "/artifacts"), require_auth=True)
        self.assertEqual(status, 200, artifact_payload)
        self.assertIsInstance(artifact_payload, dict)
        artifacts = artifact_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0, artifact_payload)
        artifact_proofs = [self.verify_artifact_download(artifact, index) for index, artifact in enumerate(artifacts)]
        return artifact_collection_proof(str(job.get("id") or ""), artifact_proofs)

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
        self.record_check(
            "resource_policy_and_runtime_readiness",
            runtime_deployment_mode=runtimes.get("runtime_deployment_mode"),
            readiness_status=readiness.get("status"),
        )

    def test_llm_comfyui_voicebox_switch_sequence_keeps_one_gpu_pipeline(self) -> None:
        chat_model = os.getenv("B1_GPU_ACCEPTANCE_CHAT_MODEL", "chat-default")
        image_model = os.getenv("B1_GPU_ACCEPTANCE_COMFY_MODEL", "image-default")
        voicebox_model = os.getenv("B1_GPU_ACCEPTANCE_VOICEBOX_MODEL", "tts-quality")
        chat_measurement = self.require_measured_model(chat_model, expected_runtime="localai")
        comfyui_measurement = self.require_measured_model(image_model, expected_runtime="comfyui")

        chat_body = {
            "model": chat_model,
            "messages": [{"role": "user", "content": os.getenv("B1_GPU_ACCEPTANCE_CHAT_PROMPT", "Reply with the word ready.")}],
            "max_tokens": int(os.getenv("B1_GPU_ACCEPTANCE_CHAT_MAX_TOKENS", "8")),
            "temperature": 0,
            "runtime_policy": "non_comfy_only",
        }
        status, _, chat = self.client.json_request("POST", "/v1/chat/completions", body=chat_body, require_auth=True)
        self.assertEqual(status, 200, chat)
        localai_runtime_state = self.assert_single_gpu_runtime("localai", "after-localai-chat")
        self.assert_vram_within_policy("after-localai-chat")
        self.record_check(
            "localai_exclusive_gpu_residency",
            chat_model=chat_model,
            chat_resolved_model_version=chat_measurement.get("resolved_model_version"),
            after_runtime_state=localai_runtime_state,
        )

        comfy_prompt_with_metadata = load_comfy_prompt_payload_with_metadata()
        if comfy_prompt_with_metadata is None:
            self.skipTest("set B1_GPU_ACCEPTANCE_COMFY_PROMPT_JSON or B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE for ComfyUI acceptance")
        comfy_prompt, comfy_prompt_metadata = comfy_prompt_with_metadata
        if comfy_prompt_metadata.get("route_level_smoke") is True and not self.allow_comfy_tiny_smoke:
            self.record_check(
                "comfyui_switch_completed",
                "incomplete",
                comfyui_model=image_model,
                comfyui_resolved_model_version=comfyui_measurement.get("resolved_model_version"),
                comfyui_prompt=comfy_prompt_metadata,
            )
            raise AssertionError(
                "B1 GPU acceptance ComfyUI prompt uses the bundled route-level B1RuntimeTinyImage smoke node. "
                "Use a real installed text/image workflow prompt for handoff, or set "
                "B1_GPU_ACCEPTANCE_ALLOW_COMFY_TINY_SMOKE=1 only for a labelled dry run."
            )
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
        comfy_native_prompt_id = str(comfy_job.get("native_prompt_id") or "")
        self.assertTrue(comfy_native_prompt_id, comfy_job)
        comfy_artifacts = self.verified_media_job_artifacts(comfy_job)
        comfyui_runtime_state = self.assert_single_gpu_runtime("comfyui", "after-comfyui-job")
        self.assert_vram_within_policy("after-comfyui-job")
        comfy_check_status = "incomplete" if comfy_prompt_metadata.get("route_level_smoke") is True else "ok"
        self.record_check(
            "comfyui_switch_completed",
            status=comfy_check_status,
            comfyui_model=image_model,
            comfyui_resolved_model_version=comfyui_measurement.get("resolved_model_version"),
            comfyui_job_id=comfy_job.get("id"),
            comfyui_native_prompt_id=comfy_native_prompt_id,
            comfyui_prompt=comfy_prompt_metadata,
            comfyui_artifacts=comfy_artifacts,
            comfyui_artifact_count=comfy_artifacts.get("artifact_count"),
            comfyui_verified_artifact_count=comfy_artifacts.get("verified_artifact_count"),
            comfyui_first_artifact_url=comfy_artifacts.get("first_artifact_url"),
            comfyui_first_artifact_sha256=comfy_artifacts.get("first_artifact_sha256"),
            after_runtime_state=comfyui_runtime_state,
        )

        if env_flag("B1_GPU_ACCEPTANCE_SKIP_VOICEBOX", False):
            self.skipTest("B1_GPU_ACCEPTANCE_SKIP_VOICEBOX requested")
        voicebox_measurement = self.require_measured_model(voicebox_model, expected_runtime="voicebox")
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
        voicebox_runtime_state = self.assert_single_gpu_runtime("voicebox", "after-voicebox-job")
        self.assert_vram_within_policy("after-voicebox-job")
        self.record_check(
            "voicebox_switch_completed",
            voicebox_model=voicebox_model,
            voicebox_resolved_model_version=voicebox_measurement.get("resolved_model_version"),
            voicebox_job_id=voicebox_job.get("id"),
            after_runtime_state=voicebox_runtime_state,
        )
        self.record_check(
            "localai_comfyui_voicebox_switch",
            runtime_order=["localai", "comfyui", "voicebox"],
            runtime_state_sequence=[localai_runtime_state, comfyui_runtime_state, voicebox_runtime_state],
            chat_model=chat_model,
            chat_resolved_model_version=chat_measurement.get("resolved_model_version"),
            comfyui_model=image_model,
            comfyui_resolved_model_version=comfyui_measurement.get("resolved_model_version"),
            voicebox_model=voicebox_model,
            voicebox_resolved_model_version=voicebox_measurement.get("resolved_model_version"),
            model_measurements={
                "chat": chat_measurement,
                "comfyui": comfyui_measurement,
                "voicebox": voicebox_measurement,
            },
            comfyui_job_id=comfy_job.get("id"),
            comfyui_native_prompt_id=comfy_native_prompt_id,
            comfyui_prompt=comfy_prompt_metadata,
            comfyui_artifact_count=comfy_artifacts.get("artifact_count"),
            comfyui_verified_artifact_count=comfy_artifacts.get("verified_artifact_count"),
            voicebox_job_id=voicebox_job.get("id"),
        )

    def test_predefined_runtime_recovery_action_is_available_when_enabled(self) -> None:
        if not env_flag("B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION", False):
            self.skipTest("set B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1 during the handoff maintenance acceptance window")
        runtime = os.getenv("B1_GPU_ACCEPTANCE_RECOVERY_RUNTIME", "localai")
        self.assertIn(runtime, GPU_RUNTIMES)
        result = self.json_request(
            "POST",
            f"/admin/runtimes/{runtime}/recover",
            body={"reason": "cross-runtime GPU acceptance recovery probe", "timeout_seconds": 30},
        )
        self.evidence.append({"label": f"recover-{runtime}", "result": result})
        agent_result = result.get("runtime_agent")
        self.assertIsInstance(agent_result, dict, result)
        agent_status = str(agent_result.get("status") or "unknown")
        allowed_statuses = {"ok"}
        if env_flag("B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN", False):
            allowed_statuses.add("dry_run")
        self.assertIn(agent_status, allowed_statuses, result)
        self.record_check(
            "bounded_runtime_recovery_action",
            status="ok" if agent_status == "ok" else agent_status,
            runtime=runtime,
            result_status=agent_status,
            strategy=agent_result.get("strategy"),
        )


if __name__ == "__main__":
    unittest.main()
