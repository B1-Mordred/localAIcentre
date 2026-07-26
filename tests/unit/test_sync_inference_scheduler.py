from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class SyncInferenceSchedulerTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_settings(self, **changes: Any) -> None:
        self.patch_attr("settings", replace(main.settings, **changes))

    def resolution(self) -> Any:
        return main.RuntimeResolution(
            public_alias="chat-default",
            model_id="localai-chat",
            model_version="1.0.0",
            resolved_model_version="localai-chat@1.0.0",
            runtime="localai",
            preferred_runtime="localai",
            requires_gpu=True,
            resource_label="expected",
            runtime_policy="any",
        )

    def cpu_resolution(self) -> Any:
        return main.RuntimeResolution(
            public_alias="tts-fast",
            model_id="piper-fast",
            model_version="1.0.0",
            resolved_model_version="piper-fast@1.0.0",
            runtime="audio-cpu",
            preferred_runtime="audio-cpu",
            requires_gpu=False,
            resource_label="recommended",
            runtime_policy="any",
        )

    def test_openai_json_forwarding_prepares_gpu_runtime_after_lease(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                forwarded = dict(payload)
                forwarded["model"] = resolution.model_id
                return forwarded

            async def post_openai_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
                calls.append({"post": path, "payload": payload})
                return 200, {"content-type": "application/json"}, {"id": "chatcmpl_test", "model": payload["model"]}

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_chat"

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> bool:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})
            return True

        async def mark_sync_gpu_runtime_idle(resolution: Any, operation: str) -> None:
            calls.append({"idle": "runtime", "runtime": resolution.runtime, "operation": operation})

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)

        response = asyncio.run(
            main.call_openai_runtime_json(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}]},
                self.resolution(),
                "chat",
                owner_id="client_1",
            )
        )

        self.assertIsNotNone(response)
        self.assertEqual(json.loads(response.body), {
            "id": "chatcmpl_test",
            "model": "localai-chat",
            "b1_runtime": "localai",
            "b1_resolved_model": "localai-chat@1.0.0",
            "b1_public_model": "chat-default",
        })
        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "runtime": "localai", "operation": "chat", "owner_id": "client_1"},
                {"prepare": "runtime", "runtime": "localai", "operation": "chat"},
                {
                    "post": "/v1/chat/completions",
                    "payload": {"model": "localai-chat", "messages": [{"role": "user", "content": "hello"}]},
                },
                {"idle": "runtime", "runtime": "localai", "operation": "chat"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_openai_json_forwarding_classifies_runtime_preparation_failure(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                forwarded = dict(payload)
                forwarded["model"] = resolution.model_id
                return forwarded

            async def post_openai_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
                calls.append({"post": path, "payload": payload})
                return 200, {}, {}

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_chat"

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> bool:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})
            raise main.RuntimePreparationError("localai runtime load hook reported failed: model_missing")

        async def mark_sync_gpu_runtime_idle(resolution: Any, operation: str) -> None:
            calls.append({"idle": "runtime", "runtime": resolution.runtime, "operation": operation})

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.call_openai_runtime_json(
                    "/v1/chat/completions",
                    {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}]},
                    self.resolution(),
                    "chat",
                    owner_id="client_1",
                )
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["type"], "runtime_prepare_failed")
        self.assertEqual(caught.exception.detail["runtime"], "localai")
        self.assertEqual(caught.exception.detail["public_model"], "chat-default")
        self.assertFalse(caught.exception.detail["retryable"])
        self.assertIn("model_missing", caught.exception.detail["message"])
        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "runtime": "localai", "operation": "chat", "owner_id": "client_1"},
                {"prepare": "runtime", "runtime": "localai", "operation": "chat"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_openai_stream_forwarding_classifies_runtime_preparation_failure(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                forwarded = dict(payload)
                forwarded["model"] = resolution.model_id
                return forwarded

            def openai_url(self, path: str) -> str:
                calls.append({"stream": path})
                return "http://localai.test/v1/chat/completions"

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_chat"

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> bool:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})
            raise main.RuntimePreparationError("localai runtime warm hook reported unhealthy: vram_check_failed")

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("release_inference_lease", release_inference_lease)

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.call_openai_runtime_stream(
                    "/v1/chat/completions",
                    {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}], "stream": True},
                    self.resolution(),
                    "chat",
                    owner_id="client_1",
                )
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["type"], "runtime_prepare_failed")
        self.assertEqual(caught.exception.detail["runtime"], "localai")
        self.assertEqual(caught.exception.detail["resolved_model"], "localai-chat@1.0.0")
        self.assertIn("vram_check_failed", caught.exception.detail["message"])
        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "runtime": "localai", "operation": "chat", "owner_id": "client_1"},
                {"prepare": "runtime", "runtime": "localai", "operation": "chat"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_openai_streaming_response_background_releases_unconsumed_lease(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                forwarded = dict(payload)
                forwarded["model"] = resolution.model_id
                return forwarded

            def openai_url(self, path: str) -> str:
                raise AssertionError("stream body must not be opened before the response is consumed")

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_chat"

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> bool:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})
            return True

        async def mark_sync_gpu_runtime_idle(resolution: Any, operation: str) -> None:
            calls.append({"idle": "runtime", "runtime": resolution.runtime, "operation": operation})

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)

        response = asyncio.run(
            main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}], "stream": True},
                self.resolution(),
                "chat",
                owner_id="client_1",
            )
        )

        self.assertIsNotNone(response)
        self.assertIsNotNone(response.background)
        asyncio.run(response.background())

        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "runtime": "localai", "operation": "chat", "owner_id": "client_1"},
                {"prepare": "runtime", "runtime": "localai", "operation": "chat"},
                {"idle": "runtime", "runtime": "localai", "operation": "chat"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_lease_renewal_helper_renews_waiting_synchronous_call(self) -> None:
        calls: list[Any] = []
        original_wait = main.asyncio.wait

        async def runtime_call() -> str:
            calls.append("runtime-call")
            await asyncio.sleep(0)
            return "ok"

        async def renew_inference_lease(owner: str | None) -> bool:
            calls.append({"renew": owner})
            return True

        wait_count = 0

        async def fake_wait(tasks: set[Any], timeout: float | None = None) -> tuple[set[Any], set[Any]]:
            nonlocal wait_count
            calls.append({"wait_timeout": timeout})
            wait_count += 1
            if wait_count == 1:
                await asyncio.sleep(0)
                return set(), set(tasks)
            return set(tasks), set()

        self.patch_attr("renew_inference_lease", renew_inference_lease)
        main.asyncio.wait = fake_wait  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.asyncio, "wait", original_wait))
        self.patch_settings(sync_inference_lease_ttl_seconds=90)

        result = asyncio.run(main.await_with_inference_lease_renewal("lease_chat", "chat", runtime_call()))

        self.assertEqual(result, "ok")
        self.assertIn({"renew": "lease_chat"}, calls)
        self.assertIn({"wait_timeout": 30}, calls)

    def test_lease_renewal_helper_cancels_when_scheduler_lease_is_lost(self) -> None:
        calls: list[Any] = []
        original_wait = main.asyncio.wait

        async def runtime_call() -> str:
            calls.append("runtime-call")
            await asyncio.Event().wait()
            return "unreachable"

        async def renew_inference_lease(owner: str | None) -> bool:
            calls.append({"renew": owner})
            return False

        async def fake_wait(tasks: set[Any], timeout: float | None = None) -> tuple[set[Any], set[Any]]:
            calls.append({"wait_timeout": timeout})
            await asyncio.sleep(0)
            return set(), set(tasks)

        self.patch_attr("renew_inference_lease", renew_inference_lease)
        main.asyncio.wait = fake_wait  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.asyncio, "wait", original_wait))
        self.patch_settings(sync_inference_lease_ttl_seconds=90)

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.await_with_inference_lease_renewal("lease_chat", "chat", runtime_call()))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["type"], "scheduler_lease_lost")
        self.assertEqual(caught.exception.detail["operation"], "chat")
        self.assertIn({"renew": "lease_chat"}, calls)

    def test_acquire_inference_lease_rejects_nonmatching_active_reservation(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeDatabase:
            async def runtime_reservation_gate(
                self,
                owner_id: str,
                runtime: str,
                resolved_model_version: str,
                runtime_names: list[str],
            ) -> dict[str, Any]:
                calls.append(
                    {
                        "gate": owner_id,
                        "runtime": runtime,
                        "resolved_model_version": resolved_model_version,
                        "runtime_names": runtime_names,
                    }
                )
                return {
                    "allowed": False,
                    "active_reservation": {
                        "id": "reservation_1",
                        "owner_id": "batch_client",
                        "runtime": "localai",
                        "model_alias": "chat-default",
                        "resolved_model_version": "localai-chat@1.0.0",
                        "expires_at": None,
                    },
                }

            async def acquire_scheduler_owner(self, owner: str, ttl_seconds: int) -> dict[str, Any]:
                raise AssertionError("scheduler lease must not be acquired when a reservation blocks the request")

        original_database = main.database
        main.database = FakeDatabase()
        self.addCleanup(lambda: setattr(main, "database", original_database))

        with self.assertRaises(main.HTTPException) as ctx:
            asyncio.run(main.acquire_inference_lease(self.resolution(), "chat", owner_id="other_client"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(calls[0]["gate"], "other_client")
        self.assertEqual(calls[0]["runtime_names"], main.GPU_RUNTIMES)

    def test_acquire_inference_lease_rejects_gpu_when_production_hardware_policy_fails(self) -> None:
        self.patch_settings(
            runtime_deployment_mode="production",
            gpu_total_vram_gib=12.0,
            gpu_reserve_vram_gib=1.5,
            host_total_ram_gib=32.0,
            host_reserve_ram_gib=6.0,
        )

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any], str | None]:
            self.assertEqual(path, "/v1/metrics")
            return (
                {
                    "gpu": {
                        "available": True,
                        "devices": [
                            {
                                "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                "memory_total_mib": 6144,
                                "memory_free_mib": 4096,
                            }
                        ],
                    },
                    "memory": {
                        "total_bytes": 31 * 1024**3,
                        "available_bytes": 8 * 1024**3,
                    },
                },
                None,
            )

        class FakeDatabase:
            async def runtime_reservation_gate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("reservation gate must not run when hardware admission blocks")

            async def acquire_scheduler_owner(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("scheduler lease must not run when hardware admission blocks")

        self.patch_attr("runtime_agent_get", runtime_agent_get)
        self.patch_attr("database", FakeDatabase())

        with self.assertRaises(main.HTTPException) as ctx:
            asyncio.run(main.acquire_inference_lease(self.resolution(), "chat", owner_id="client_1"))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["code"], "hardware_resource_policy")
        check = ctx.exception.detail["hardware_resource_policy"]
        self.assertEqual(check["name"], "hardware:resource-policy")
        self.assertEqual(check["status"], "failed")
        self.assertIn("largest GPU VRAM is 6144 MiB", check["detail"])
        self.assertEqual(check["data"]["observed"]["largest_gpu_memory_total_mib"], 6144)

    def test_acquire_inference_lease_skips_hardware_policy_for_cpu_resolution(self) -> None:
        self.patch_settings(runtime_deployment_mode="production")

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any], str | None]:
            raise AssertionError("CPU-only inference must not require GPU telemetry")

        class FakeDatabase:
            async def runtime_reservation_gate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("CPU-only inference must not use GPU reservation gate")

            async def acquire_scheduler_owner(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("CPU-only inference must not acquire a GPU scheduler lease")

        self.patch_attr("runtime_agent_get", runtime_agent_get)
        self.patch_attr("database", FakeDatabase())

        owner = asyncio.run(main.acquire_inference_lease(self.cpu_resolution(), "speech", owner_id="client_1"))

        self.assertIsNone(owner)


if __name__ == "__main__":
    unittest.main()
