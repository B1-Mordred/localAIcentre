from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_proxy() -> ModuleType:
    localai_dir = ROOT / "deploy" / "localai"
    if str(localai_dir) not in sys.path:
        sys.path.insert(0, str(localai_dir))
    spec = importlib.util.spec_from_file_location("b1_localai_proxy", localai_dir / "b1_localai_proxy.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load LocalAI proxy")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy = load_proxy()


class LocalAIProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy.ACTIVE_PROXY_REQUESTS = 0

    def patch_subprocess(self, stdout: str) -> None:
        original = subprocess.run

        def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

        proxy.subprocess.run = fake_run
        self.addCleanup(lambda: setattr(proxy.subprocess, "run", original))

    def test_payload_model_candidates_strips_versions_and_deduplicates(self) -> None:
        payload = {
            "model": "chat-default@old",
            "resolved_model_version": "llama-3.1-8b-q4@sha256:abc",
            "model_alias": "chat-default",
        }

        self.assertEqual(proxy.payload_model_candidates(payload), ["chat-default", "llama-3.1-8b-q4"])

    def test_model_ids_from_openai_style_list(self) -> None:
        payload = {"data": [{"id": "chat-default"}, {"id": "embedding-default"}]}

        self.assertEqual(proxy.model_ids_from_list(payload), {"chat-default", "embedding-default"})
        self.assertTrue(proxy.model_available(["chat-default@sha256:abc"], {"chat-default"}))

    def test_smoke_request_for_chat_uses_static_redaction_safe_prompt(self) -> None:
        status, reason, request = proxy.smoke_request(
            {
                "model": "chat-default",
                "modality": "llm",
                "operation": "chat",
            }
        )

        self.assertEqual(status, "ok")
        self.assertEqual(reason, "chat")
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"]["model"], "chat-default")
        self.assertEqual(request["body"]["messages"][0]["content"], "b1 local runtime smoke")
        self.assertFalse(request["body"]["stream"])

    def test_smoke_request_for_embeddings_and_images(self) -> None:
        embedding = proxy.smoke_request({"model": "embedding-default", "modality": "embeddings", "operation": "embeddings"})
        image = proxy.smoke_request({"model": "image-default", "modality": "image", "operation": "text-to-image"})

        self.assertEqual(embedding[2]["path"], "/v1/embeddings")
        self.assertEqual(image[2]["path"], "/v1/images/generations")
        self.assertEqual(image[2]["body"]["size"], os.getenv("B1_LOCALAI_SMOKE_IMAGE_SIZE", "64x64"))

    def test_video_smoke_is_disabled_unless_explicitly_enabled(self) -> None:
        with patch.dict(os.environ, {"B1_LOCALAI_VIDEO_SMOKE_ENABLED": "false"}, clear=False):
            status, reason, request = proxy.smoke_request({"model": "video-text", "modality": "video", "operation": "text-to-video"})

        self.assertEqual(status, "unsupported")
        self.assertEqual(reason, "video_generation_smoke_not_available")
        self.assertIsNone(request)

    def test_operation_kind_distinguishes_image_and_video_generation(self) -> None:
        self.assertEqual(proxy.operation_kind({"modality": "image", "operation": "generation"}), "image_generation")
        self.assertEqual(proxy.operation_kind({"modality": "video", "operation": "generation"}), "video_generation")
        self.assertEqual(proxy.operation_kind({"operation": "text-to-image"}), "image_generation")
        self.assertEqual(proxy.operation_kind({"operation": "text-to-video"}), "video_generation")
        self.assertEqual(proxy.operation_kind({"operation": "image-to-video"}), "video_image")

    def test_runtime_control_auth_requires_configured_bearer_token(self) -> None:
        with patch.dict(os.environ, {"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"}, clear=False):
            missing = proxy.runtime_control_auth_failure({})
            accepted = proxy.runtime_control_auth_failure({"Authorization": "Bearer hook-token"})

        self.assertEqual(missing[0], 401)
        self.assertEqual(missing[1]["reason"], "runtime_control_token_required")
        self.assertIsNone(accepted)

    def test_inference_proxy_strips_runtime_bearer_before_forwarding(self) -> None:
        forwarded = proxy.forwarded_request_headers(
            {
                "Authorization": "Bearer hook-token",
                "Host": "p40-worker.b1.germering",
                "Connection": "keep-alive",
                "Content-Type": "application/json",
                "X-Request-ID": "request-1",
            }
        )

        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("Host", forwarded)
        self.assertNotIn("Connection", forwarded)
        self.assertEqual(forwarded["Content-Type"], "application/json")
        self.assertEqual(forwarded["X-Request-ID"], "request-1")

    def test_build_info_reports_pinned_localai_wrapper_identity(self) -> None:
        with patch.dict(
            os.environ,
            {
                "B1_LOCALAI_UPSTREAM_VERSION": "v4.7.1-gpu-nvidia-cuda-12",
                "B1_LOCALAI_UPSTREAM_COMMIT": "b224c96db6f4b87306a33a808650bfce63b12588",
                "B1_LOCALAI_UPSTREAM_IMAGE": "localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:" + "a" * 64,
            },
            clear=False,
        ):
            info = proxy.build_info_response()

        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["action"], "build-info")
        self.assertEqual(info["proxy_version"], proxy.LOCALAI_PROXY_VERSION)
        self.assertEqual(info["upstream"], "localai/localai")
        self.assertTrue(info["pinned"])
        self.assertIn("status", info["capabilities"]["actions"])
        self.assertIn("unload", info["capabilities"]["actions"])

    def test_status_reports_guardrails_and_redacted_model_probe(self) -> None:
        class FakeClient:
            def request_json(self, method: str, path: str, body: dict[str, object] | None = None):
                self.request = (method, path, body)
                return 200, {"data": [{"id": "chat-secret-model"}, {"id": "image-secret-model"}]}

        with (
            patch.dict(
                os.environ,
                {
                    "LOCALAI_MAX_ACTIVE_BACKENDS": "1",
                    "LOCALAI_WATCHDOG_IDLE": "true",
                    "LOCALAI_FORCE_EVICTION_WHEN_BUSY": "false",
                },
                clear=False,
            ),
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
        ):
            status = proxy.status_response()

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["action"], "status")
        self.assertEqual(status["guardrails"]["status"], "ok")
        self.assertEqual(status["guardrails"]["max_active_backends"], 1)
        self.assertEqual(status["model_probe"]["status"], "ok")
        self.assertEqual(status["model_probe"]["model_count"], 2)
        self.assertNotIn("chat-secret-model", str(status))
        self.assertNotIn("image-secret-model", str(status))

    def test_status_degrades_when_localai_backend_guardrail_is_relaxed(self) -> None:
        class FakeClient:
            def request_json(self, method: str, path: str, body: dict[str, object] | None = None):
                return 200, {"data": []}

        with (
            patch.dict(
                os.environ,
                {
                    "LOCALAI_MAX_ACTIVE_BACKENDS": "2",
                    "LOCALAI_WATCHDOG_IDLE": "false",
                    "LOCALAI_FORCE_EVICTION_WHEN_BUSY": "true",
                },
                clear=False,
            ),
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
        ):
            status = proxy.status_response()

        self.assertEqual(status["status"], "degraded")
        self.assertIn("LOCALAI_MAX_ACTIVE_BACKENDS must be 1", status["guardrails"]["blockers"])
        self.assertIn("LOCALAI_WATCHDOG_IDLE must be true", status["guardrails"]["blockers"])
        self.assertIn("LOCALAI_FORCE_EVICTION_WHEN_BUSY must be false", status["guardrails"]["blockers"])

    def test_gpu_metrics_exposes_bounded_worker_measurements(self) -> None:
        self.patch_subprocess(
            "GPU-test, Tesla P40, 23040, 11433, 11474, 95, 43, 158.64, 250.00, P0\n"
        )

        result = proxy.gpu_metrics_response()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["work"], {"active_requests": 0, "queued_requests": 0})
        device = result["gpu"]["devices"][0]
        self.assertEqual(device["uuid"], "GPU-test")
        self.assertEqual(device["memory_used_mib"], 11433)
        self.assertEqual(device["utilization_gpu_percent"], 95)
        self.assertEqual(device["power_draw_w"], 158.64)

    def test_worker_readiness_requires_upstream_and_gpu_metrics(self) -> None:
        class FakeClient:
            def request_json(self, _method: str, _path: str, _body: dict[str, Any] | None = None) -> tuple[int, Any]:
                return 200, None

        with (
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
            patch.object(proxy, "gpu_metrics_response", return_value={"status": "ok", "gpu": {"devices": [{"name": "Tesla P40"}]}}),
        ):
            ready = proxy.worker_ready_response()

        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["gpu_count"], 1)

    def test_worker_readiness_fails_closed_when_nvml_is_unavailable(self) -> None:
        class FakeClient:
            def request_json(self, _method: str, _path: str, _body: dict[str, Any] | None = None) -> tuple[int, Any]:
                return 200, None

        with (
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
            patch.object(proxy, "gpu_metrics_response", return_value={"status": "degraded", "gpu": {"devices": []}}),
        ):
            ready = proxy.worker_ready_response()

        self.assertEqual(ready["status"], "degraded")
        self.assertEqual(ready["reason"], "gpu_metrics_unavailable")

    def test_idle_cancel_recover_and_unload_are_idempotent(self) -> None:
        self.patch_subprocess(
            "GPU-test, Tesla P40, 23040, 0, 22906, 0, 36, 10.30, 250.00, P8\n"
        )

        class FakeClient:
            def request_json(self, _method: str, _path: str, _body: dict[str, Any]) -> tuple[int, Any]:
                return 500, {"error": "backend not loaded"}

        with patch.object(proxy, "hook_timeout_client", return_value=FakeClient()):
            for action in ("cancel", "recover", "unload"):
                with self.subTest(action=action):
                    result = proxy.handle_runtime_action(action, {})
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["strategy"], "backend_already_idle")

    def test_busy_failed_recovery_does_not_report_success(self) -> None:
        self.patch_subprocess(
            "GPU-test, Tesla P40, 23040, 11433, 11474, 95, 43, 158.64, 250.00, P0\n"
        )

        class FakeClient:
            def request_json(self, _method: str, _path: str, _body: dict[str, Any]) -> tuple[int, Any]:
                return 500, {"error": "shutdown failed"}

        with (
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
            patch.object(proxy, "force_stop_localai_backends", return_value={"status": "failed", "reason": "backend_process_or_vram_remained"}),
        ):
            result = proxy.handle_runtime_action("recover", {"model": "chat-quality"})

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "backend_process_or_vram_remained")

    def test_busy_cancel_forces_backend_process_stop_after_graceful_rejection(self) -> None:
        self.patch_subprocess(
            "GPU-test, Tesla P40, 23040, 11433, 11474, 95, 43, 158.64, 250.00, P0\n"
        )

        class FakeClient:
            def request_json(self, _method: str, _path: str, _body: dict[str, Any]) -> tuple[int, Any]:
                return 500, {"error": "backend is busy"}

        with (
            patch.object(proxy, "hook_timeout_client", return_value=FakeClient()),
            patch.object(
                proxy,
                "force_stop_localai_backends",
                return_value={"status": "ok", "reason": "backend_process_terminated", "terminated_processes": 1, "escalated": False},
            ),
        ):
            result = proxy.handle_runtime_action("cancel", {"model": "chat-quality"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "backend_process_terminated")
        self.assertEqual(result["terminated_processes"], 1)
        self.assertFalse(result["escalated"])


if __name__ == "__main__":
    unittest.main()
