from __future__ import annotations

import asyncio
import json
import sys
import unittest
from contextlib import suppress
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

    def lan_worker_resolution(self) -> Any:
        return main.RuntimeResolution(
            public_alias="chat-quality",
            model_id="b1-openai-gpt-oss-20b-mxfp4-localai",
            model_version="b97cbb",
            resolved_model_version="b1-openai-gpt-oss-20b-mxfp4-localai@b97cbb",
            runtime="lan-localai-worker",
            preferred_runtime="lan-localai-worker",
            requires_gpu=True,
            resource_label="measured",
            runtime_policy="any",
        )

    def test_chat_request_keeps_openai_tool_and_reasoning_fields(self) -> None:
        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "preserve this across the agent loop",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_check", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "{\"status\":\"ok\"}"},
            ],
            tools=[{"type": "function", "function": {"name": "run_check", "parameters": {"type": "object"}}}],
            tool_choice="auto",
            parallel_tool_calls=False,
            b1_tools=["web_search"],
            chat_template_kwargs={"enable_thinking": True, "reasoning_effort": "max"},
        )

        dumped = payload.model_dump(exclude_none=True)
        forwarded = main.strip_b1_chat_fields(dumped, preserve_client_tools=True)

        self.assertIn("tools", dumped)
        self.assertEqual(dumped["tool_choice"], "auto")
        self.assertFalse(dumped["parallel_tool_calls"])
        self.assertEqual(dumped["messages"][0]["reasoning_content"], "preserve this across the agent loop")
        self.assertEqual(dumped["messages"][0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(dumped["messages"][1]["tool_call_id"], "call_1")
        self.assertEqual(
            dumped["chat_template_kwargs"],
            {"enable_thinking": True, "reasoning_effort": "max"},
        )
        self.assertEqual(forwarded["tools"], dumped["tools"])
        self.assertEqual(forwarded["tool_choice"], "auto")
        self.assertFalse(forwarded["parallel_tool_calls"])
        self.assertEqual(forwarded["messages"][0]["reasoning_content"], "preserve this across the agent loop")
        self.assertEqual(forwarded["messages"][0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(forwarded["messages"][1]["tool_call_id"], "call_1")
        self.assertEqual(forwarded["chat_template_kwargs"], dumped["chat_template_kwargs"])
        self.assertNotIn("b1_tools", forwarded)
        self.assertNotIn("b1_tool_max_iterations", forwarded)

    def test_open_webui_chat_id_is_trusted_only_for_internal_client(self) -> None:
        internal = type("Auth", (), {"subject_id": main.OPEN_WEBUI_CLIENT_ID})()
        external = type("Auth", (), {"subject_id": "external_client"})()

        key = main.trusted_open_webui_conversation_key("chat-1234", internal)

        self.assertEqual(key, main.hashlib.sha256(b"chat-1234").hexdigest())
        self.assertIsNone(main.trusted_open_webui_conversation_key("chat-1234", external))
        self.assertIsNone(main.trusted_open_webui_conversation_key(None, internal))
        with self.assertRaises(main.HTTPException) as caught:
            main.trusted_open_webui_conversation_key("bad chat id", internal)
        self.assertEqual(caught.exception.status_code, 422)

    def test_open_webui_stream_registry_supersedes_only_matching_conversation(self) -> None:
        main.open_webui_streams.clear()
        self.addCleanup(main.open_webui_streams.clear)

        first, previous = main.register_open_webui_stream("conversation-a")
        other, other_previous = main.register_open_webui_stream("conversation-b")
        second, replaced = main.register_open_webui_stream("conversation-a")

        self.assertIsNone(previous)
        self.assertIsNone(other_previous)
        self.assertIs(replaced, first)
        self.assertTrue(first.supersede_requested.is_set())
        self.assertFalse(other.supersede_requested.is_set())
        main.finish_open_webui_stream(first)
        self.assertIs(main.open_webui_streams["conversation-a"], second)
        main.finish_open_webui_stream(second)
        main.finish_open_webui_stream(other)
        self.assertEqual(main.open_webui_streams, {})

    def test_explicit_open_webui_cancel_waits_for_registered_stream_cleanup(self) -> None:
        main.open_webui_streams.clear()
        self.addCleanup(main.open_webui_streams.clear)

        async def exercise() -> tuple[dict[str, Any], Any]:
            state, _previous = main.register_open_webui_stream("conversation-a")

            async def finish_after_cancel() -> None:
                await state.supersede_requested.wait()
                main.finish_open_webui_stream(state)

            finisher = asyncio.create_task(finish_after_cancel())
            result = await main.request_open_webui_stream_cancel("conversation-a")
            await finisher
            return result, state

        result, state = asyncio.run(exercise())

        self.assertEqual(
            result,
            {
                "status": "ok",
                "active": True,
                "completed": True,
                "request_id": state.request_id,
            },
        )
        self.assertTrue(state.supersede_requested.is_set())
        self.assertTrue(state.finished.is_set())
        self.assertEqual(main.open_webui_streams, {})

    def test_explicit_open_webui_cancel_is_idempotent_without_active_stream(self) -> None:
        main.open_webui_streams.clear()
        result = asyncio.run(main.request_open_webui_stream_cancel("missing-conversation"))
        self.assertEqual(result, {"status": "ok", "active": False, "completed": True})

    def test_open_webui_status_sse_uses_native_status_event(self) -> None:
        raw = main.open_webui_status_sse("Waiting for the P40 worker", done=False)
        payload = json.loads(raw.removeprefix("data: ").strip())

        self.assertEqual(payload["event"]["type"], "status")
        self.assertEqual(payload["event"]["data"], {"description": "Waiting for the P40 worker", "done": False})

    def test_gpt_oss_reasoning_effort_is_forwarded_as_native_template_kwarg(self) -> None:
        resolution = main.RuntimeResolution(
            public_alias="gpt-oss-reasoning",
            model_id="b1-openai-gpt-oss-20b-mxfp4-localai",
            model_version="b97cbb",
            resolved_model_version="b1-openai-gpt-oss-20b-mxfp4-localai@b97cbb",
            runtime="localai",
            preferred_runtime="localai",
            requires_gpu=True,
            resource_label="offload-required",
            runtime_policy="any",
        )

        async def no_policy(_alias: str):
            return None

        original = main.database.get_model_alias_policy
        main.database.get_model_alias_policy = no_policy
        self.addCleanup(lambda: setattr(main.database, "get_model_alias_policy", original))

        effort = asyncio.run(main.gpt_oss_reasoning_effort(main.ChatCompletionRequest(model="gpt-oss-reasoning"), resolution))
        payload = main.inject_gpt_oss_reasoning_effort({"messages": [{"role": "user", "content": "solve it"}]}, effort)

        self.assertEqual(effort, "high")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "solve it"}])

    def test_agent_transcript_is_bounded_and_keeps_structured_tool_history(self) -> None:
        messages = [
            {"role": "assistant", "content": "", "reasoning_content": "inspect", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"},
        ]
        transcript = main.bounded_agent_transcript_messages(messages)
        merged = main.prepend_agent_transcript({"messages": [{"role": "user", "content": "continue"}]}, transcript)

        self.assertEqual(transcript[0]["reasoning_content"], "inspect")
        self.assertEqual(transcript[1]["tool_call_id"], "call_1")
        self.assertEqual(merged["messages"][-1]["content"], "continue")

    def test_matching_resident_gpu_model_skips_cold_load_reserve_check(self) -> None:
        resolution = self.resolution()

        async def current_runtime_state(_runtime: str):
            return {
                "status": "idle",
                "active_model": resolution.model_id,
                "resolved_model_version": resolution.resolved_model_version,
            }

        async def hardware_resource_policy_snapshot():
            raise AssertionError("matching resident model must not run cold-load reserve admission")

        self.patch_settings(runtime_deployment_mode="production")
        self.patch_attr("current_runtime_state", current_runtime_state)
        self.patch_attr("hardware_resource_policy_snapshot", hardware_resource_policy_snapshot)

        asyncio.run(main.enforce_gpu_hardware_admission(resolution))

    def test_lan_worker_does_not_use_main_appliance_hardware_policy(self) -> None:
        async def hardware_resource_policy_snapshot():
            raise AssertionError("remote P40 admission must not use main-appliance GPU metrics")

        self.patch_settings(runtime_deployment_mode="production")
        self.patch_attr("hardware_resource_policy_snapshot", hardware_resource_policy_snapshot)

        asyncio.run(main.enforce_gpu_hardware_admission(self.lan_worker_resolution()))

    def test_lan_worker_cancel_uses_runtime_control_hook(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeRunner:
            def runtime_unload_payload(self, runtime: str, job: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"job_id": job["id"], "runtime": runtime, "model": (state or {}).get("active_model")}

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                calls.append({"runtime": runtime, "action": action, "payload": payload})
                return {"status": "ok"}

            def runtime_hook_status(self, result: dict[str, Any]) -> str:
                return str(result.get("status"))

            async def unload_vram_release_check(self, runtime: str) -> tuple[bool, dict[str, Any]]:
                calls.append({"runtime": runtime, "verification": "vram_released"})
                return True, {"status": "ok", "memory_used_mib": 0}

            def compact_hook_result(self, result: dict[str, Any]) -> dict[str, Any]:
                return result

            async def upsert_runtime_state(self, payload: dict[str, Any]) -> dict[str, Any]:
                calls.append({"runtime_state": payload})
                return payload

        self.patch_attr("runtime_control_runner", lambda *_args, **_kwargs: FakeRunner())

        asyncio.run(main.cancel_sync_gpu_runtime(self.lan_worker_resolution(), "chat"))

        self.assertEqual(calls[0]["runtime"], "lan-localai-worker")
        self.assertEqual(calls[0]["action"], "cancel")
        self.assertEqual(calls[0]["payload"]["operation"], "cancel")
        self.assertEqual(calls[0]["payload"]["model"], "b1-openai-gpt-oss-20b-mxfp4-localai")
        self.assertEqual(calls[1], {"runtime": "lan-localai-worker", "verification": "vram_released"})
        self.assertIsNone(calls[2]["runtime_state"]["active_model"])

    def test_gpt_oss_harmony_final_channel_is_separated_from_reasoning(self) -> None:
        resolution = main.RuntimeResolution(
            public_alias="gpt-oss-20b", model_id="b1-openai-gpt-oss-20b-mxfp4-localai", model_version="v1",
            resolved_model_version="b1-openai-gpt-oss-20b-mxfp4-localai@v1", runtime="localai", preferred_runtime="localai",
            requires_gpu=True, resource_label="expected", runtime_policy="any",
        )
        body = {"choices": [{"message": {"role": "assistant", "content": "analysis text<|end|><|start|>assistant<|channel|>final\n<|message|>READY"}}]}
        message = main.normalize_gpt_oss_harmony_response(body, resolution)["choices"][0]["message"]
        self.assertEqual(message["content"], "READY")
        self.assertEqual(message["reasoning_content"], "analysis text")

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
        })
        self.assertEqual(response.headers["x-b1-runtime"], "localai")
        self.assertEqual(response.headers["x-b1-resolved-model"], "localai-chat@1.0.0")
        self.assertEqual(response.headers["x-b1-public-model"], "chat-default")
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

    def test_openai_json_retries_transient_localai_transition_error(self) -> None:
        calls: list[str] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return payload

            async def post_openai_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
                calls.append("post")
                if len(calls) == 1:
                    return 503, {}, {"detail": "backend is restarting"}
                return 200, {}, {"id": "chatcmpl_retry"}

        async def acquire_inference_lease(*_: Any, **__: Any) -> str:
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_: Any, **__: Any) -> bool:
            return True

        async def mark_sync_gpu_runtime_idle(*_: Any, **__: Any) -> None:
            return None

        async def release_inference_lease(*_: Any, **__: Any) -> None:
            return None

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
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["post", "post"])

    def test_openai_json_does_not_replay_read_timeout(self) -> None:
        calls = 0

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return payload

            async def post_openai_json(self, _path: str, _payload: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
                nonlocal calls
                calls += 1
                raise main.httpx.ReadTimeout("accepted request produced no response within the read timeout")

        async def acquire_inference_lease(*_: Any, **__: Any) -> str:
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_: Any, **__: Any) -> bool:
            return True

        async def mark_sync_gpu_runtime_idle(*_: Any, **__: Any) -> None:
            return None

        async def release_inference_lease(*_: Any, **__: Any) -> None:
            return None

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.call_openai_runtime_json(
                    "/v1/chat/completions",
                    {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}]},
                    self.lan_worker_resolution(),
                    "chat",
                )
            )

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("ReadTimeout", caught.exception.detail)
        self.assertEqual(calls, 1)

    def test_openai_json_retries_pre_acceptance_connect_error(self) -> None:
        calls = 0

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return payload

            async def post_openai_json(self, _path: str, _payload: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise main.httpx.ConnectError("worker listener is not accepting connections yet")
                return 200, {}, {"id": "chatcmpl_connected"}

        async def acquire_inference_lease(*_: Any, **__: Any) -> str:
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_: Any, **__: Any) -> bool:
            return True

        async def mark_sync_gpu_runtime_idle(*_: Any, **__: Any) -> None:
            return None

        async def release_inference_lease(*_: Any, **__: Any) -> None:
            return None

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)

        response = asyncio.run(
            main.call_openai_runtime_json(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}]},
                self.lan_worker_resolution(),
                "chat",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 2)

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

        async def cancel_sync_gpu_runtime(resolution: Any, operation: str) -> None:
            calls.append({"cancel": "runtime", "runtime": resolution.runtime, "operation": operation})

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
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
                {"cancel": "runtime", "runtime": "localai", "operation": "chat"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_openai_streaming_response_close_cancels_runtime_and_releases_lease(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def aiter_bytes(self):
                yield b"data: first\n\n"
                await asyncio.Event().wait()

        class FakeClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            def stream(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return {**payload, "model": resolution.model_id}

            def openai_url(self, path: str) -> str:
                return f"http://localai.test{path}"

            def request_headers(self) -> dict[str, str]:
                return {}

            def httpx_client_kwargs(self) -> dict[str, Any]:
                return {}

        async def acquire_inference_lease(_resolution: Any, _operation: str, owner_id: str | None = None) -> str:
            calls.append({"lease": "acquire", "owner_id": owner_id})
            return "lease_chat"

        async def prepare_sync_gpu_runtime(_resolution: Any, _operation: str) -> bool:
            calls.append({"prepare": "runtime"})
            return True

        async def cancel_sync_gpu_runtime(_resolution: Any, _operation: str) -> None:
            calls.append({"cancel": "runtime"})

        async def mark_sync_gpu_runtime_idle(_resolution: Any, _operation: str) -> None:
            calls.append({"idle": "runtime"})

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("release_inference_lease", release_inference_lease)
        self.patch_attr("httpx", type("FakeHttpx", (), {"AsyncClient": FakeClient, "HTTPError": main.httpx.HTTPError}))

        async def exercise() -> None:
            response = await main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [{"role": "user", "content": "hello"}], "stream": True},
                self.resolution(),
                "chat",
                owner_id="client_1",
            )
            self.assertEqual(await anext(response.body_iterator), b"data: first\n\n")
            await response.body_iterator.aclose()

        asyncio.run(exercise())

        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "owner_id": "client_1"},
                {"prepare": "runtime"},
                {"cancel": "runtime"},
                {"lease": "release", "owner": "lease_chat"},
            ],
        )

    def test_request_scoped_stream_close_drains_without_unloading_runtime(self) -> None:
        calls: list[Any] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return {**payload, "model": resolution.model_id}

            def openai_url(self, path: str) -> str:
                raise AssertionError("unconsumed stream must not open the upstream request")

        async def acquire_inference_lease(*_args: Any, **_kwargs: Any) -> str:
            calls.append("acquire")
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("prepare")
            return True

        async def wait_for_sync_runtime_request_drain(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("drain")
            return True

        async def mark_sync_gpu_runtime_idle(*_args: Any, **_kwargs: Any) -> None:
            calls.append("idle")

        async def cancel_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> None:
            calls.append("forced-cancel")

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"release": owner})

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("wait_for_sync_runtime_request_drain", wait_for_sync_runtime_request_drain)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
        self.patch_attr("release_inference_lease", release_inference_lease)

        async def exercise() -> None:
            response = await main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [], "stream": True},
                self.lan_worker_resolution(),
                "chat",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
                supersede_requested=asyncio.Event(),
                request_scoped_cancel=True,
            )
            await response.background()

        asyncio.run(exercise())

        self.assertEqual(calls, ["acquire", "prepare", "drain", "idle", {"release": "lease_chat"}])

    def test_request_scoped_stream_close_forces_recovery_when_drain_times_out(self) -> None:
        calls: list[Any] = []

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return {**payload, "model": resolution.model_id}

            def openai_url(self, path: str) -> str:
                raise AssertionError("unconsumed stream must not open the upstream request")

        async def acquire_inference_lease(*_args: Any, **_kwargs: Any) -> str:
            calls.append("acquire")
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("prepare")
            return True

        async def wait_for_sync_runtime_request_drain(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("drain-timeout")
            return False

        async def mark_sync_gpu_runtime_idle(*_args: Any, **_kwargs: Any) -> None:
            calls.append("idle")

        async def cancel_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> None:
            calls.append("forced-cancel")

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"release": owner})

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("wait_for_sync_runtime_request_drain", wait_for_sync_runtime_request_drain)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
        self.patch_attr("release_inference_lease", release_inference_lease)

        async def exercise() -> None:
            response = await main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [], "stream": True},
                self.lan_worker_resolution(),
                "chat",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
                supersede_requested=asyncio.Event(),
                request_scoped_cancel=True,
            )
            await response.background()

        asyncio.run(exercise())

        self.assertEqual(calls, ["acquire", "prepare", "drain-timeout", "forced-cancel", {"release": "lease_chat"}])

    def test_supersede_interrupts_blocked_upstream_stream_and_drains_request(self) -> None:
        calls: list[Any] = []
        upstream_blocked: asyncio.Event | None = None

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                calls.append("upstream-closed")

            async def aiter_bytes(self):
                yield b"data: first\n\n"
                assert upstream_blocked is not None
                await upstream_blocked.wait()

        class FakeClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            def stream(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return {**payload, "model": resolution.model_id}

            def openai_url(self, path: str) -> str:
                return f"http://localai.test{path}"

            def request_headers(self) -> dict[str, str]:
                return {}

            def httpx_client_kwargs(self) -> dict[str, Any]:
                return {}

        async def acquire_inference_lease(*_args: Any, **_kwargs: Any) -> str:
            calls.append("acquire")
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("prepare")
            return True

        async def wait_for_sync_runtime_request_drain(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("drain")
            return True

        async def mark_sync_gpu_runtime_idle(*_args: Any, **_kwargs: Any) -> None:
            calls.append("idle")

        async def cancel_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> None:
            calls.append("forced-cancel")

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"release": owner})

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("wait_for_sync_runtime_request_drain", wait_for_sync_runtime_request_drain)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
        self.patch_attr("release_inference_lease", release_inference_lease)
        self.patch_attr("httpx", type("FakeHttpx", (), {"AsyncClient": FakeClient, "HTTPError": main.httpx.HTTPError}))

        async def exercise() -> str:
            nonlocal upstream_blocked
            upstream_blocked = asyncio.Event()
            supersede_requested = asyncio.Event()
            response = await main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [], "stream": True},
                self.lan_worker_resolution(),
                "chat",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
                supersede_requested=supersede_requested,
                request_scoped_cancel=True,
            )
            iterator = response.body_iterator
            chunks = [await anext(iterator)]
            blocked_read = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            supersede_requested.set()
            chunks.append(await asyncio.wait_for(blocked_read, timeout=0.5))
            async for chunk in iterator:
                chunks.append(chunk)
            return "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)

        output = asyncio.run(exercise())

        self.assertIn("first", output)
        self.assertIn("superseded", output.lower())
        self.assertIn("[DONE]", output)
        self.assertEqual(calls, ["acquire", "prepare", "upstream-closed", "drain", "idle", {"release": "lease_chat"}])

    def test_downstream_cancel_reaps_blocked_upstream_read_task(self) -> None:
        calls: list[Any] = []
        unhandled: list[dict[str, Any]] = []

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                calls.append("upstream-closed")

            async def aiter_bytes(self):
                yield b"data: first\n\n"
                await asyncio.Event().wait()

        class FakeClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            def stream(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeAdapter:
            def openai_payload(self, payload: dict[str, Any], resolution: Any) -> dict[str, Any]:
                return {**payload, "model": resolution.model_id}

            def openai_url(self, path: str) -> str:
                return f"http://localai.test{path}"

            def request_headers(self) -> dict[str, str]:
                return {}

            def httpx_client_kwargs(self) -> dict[str, Any]:
                return {}

        async def acquire_inference_lease(*_args: Any, **_kwargs: Any) -> str:
            calls.append("acquire")
            return "lease_chat"

        async def prepare_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("prepare")
            return True

        async def wait_for_sync_runtime_request_drain(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("drain")
            return True

        async def mark_sync_gpu_runtime_idle(*_args: Any, **_kwargs: Any) -> None:
            calls.append("idle")

        async def cancel_sync_gpu_runtime(*_args: Any, **_kwargs: Any) -> None:
            calls.append("forced-cancel")

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"release": owner})

        self.patch_attr("openai_runtime_adapter", lambda _resolution: FakeAdapter())
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)
        self.patch_attr("wait_for_sync_runtime_request_drain", wait_for_sync_runtime_request_drain)
        self.patch_attr("mark_sync_gpu_runtime_idle", mark_sync_gpu_runtime_idle)
        self.patch_attr("cancel_sync_gpu_runtime", cancel_sync_gpu_runtime)
        self.patch_attr("release_inference_lease", release_inference_lease)
        self.patch_attr("httpx", type("FakeHttpx", (), {"AsyncClient": FakeClient, "HTTPError": main.httpx.HTTPError}))

        async def exercise() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            response = await main.call_openai_runtime_stream(
                "/v1/chat/completions",
                {"model": "chat-default", "messages": [], "stream": True},
                self.lan_worker_resolution(),
                "chat",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
                supersede_requested=asyncio.Event(),
                request_scoped_cancel=True,
            )
            iterator = response.body_iterator
            self.assertEqual(await anext(iterator), b"data: first\n\n")
            blocked_read = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            blocked_read.cancel()
            with suppress(asyncio.CancelledError):
                await blocked_read
            await asyncio.sleep(0)

        asyncio.run(exercise())

        self.assertEqual(unhandled, [])
        self.assertEqual(calls, ["acquire", "prepare", "upstream-closed", "drain", "idle", {"release": "lease_chat"}])

    def test_deferred_open_webui_stream_emits_waiting_and_acquired_status(self) -> None:
        gate: asyncio.Event | None = None

        async def fake_runtime_stream(*_args: Any, **_kwargs: Any) -> main.StreamingResponse:
            assert gate is not None
            await gate.wait()

            async def body():
                yield "data: {\"choices\":[]}\n\n"
                yield "data: [DONE]\n\n"

            return main.StreamingResponse(body(), media_type="text/event-stream")

        self.patch_attr("call_openai_runtime_stream", fake_runtime_stream)
        main.open_webui_streams.clear()
        self.addCleanup(main.open_webui_streams.clear)

        async def exercise() -> list[str]:
            nonlocal gate
            gate = asyncio.Event()
            response = await main.call_open_webui_runtime_stream(
                "/v1/chat/completions",
                {"stream": True},
                self.lan_worker_resolution(),
                "chat",
                conversation_key="conversation-a",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
            )
            iterator = response.body_iterator
            chunks = [await anext(iterator)]
            waiting = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            gate.set()
            chunks.append(await waiting)
            async for chunk in iterator:
                chunks.append(chunk)
            return [chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks]

        chunks = asyncio.run(exercise())

        self.assertIn("Waiting for the P40 worker", chunks[0])
        self.assertTrue(any("P40 worker acquired" in chunk for chunk in chunks))
        self.assertTrue(any("[DONE]" in chunk for chunk in chunks))
        self.assertEqual(main.open_webui_streams, {})

    def test_new_same_conversation_stream_supersedes_previous_stream(self) -> None:
        call_count = 0

        async def fake_runtime_stream(*_args: Any, **kwargs: Any) -> main.StreamingResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                supersede_requested = kwargs["supersede_requested"]
                await supersede_requested.wait()
                raise main.OpenWebUIStreamSuperseded()

            async def body():
                yield "data: [DONE]\n\n"

            return main.StreamingResponse(body(), media_type="text/event-stream")

        self.patch_attr("call_openai_runtime_stream", fake_runtime_stream)
        main.open_webui_streams.clear()
        self.addCleanup(main.open_webui_streams.clear)

        async def collect(response: main.StreamingResponse) -> str:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            return "".join(chunks)

        async def exercise() -> tuple[str, str]:
            first = await main.call_open_webui_runtime_stream(
                "/v1/chat/completions", {"stream": True}, self.lan_worker_resolution(), "chat",
                conversation_key="same-conversation", owner_id=main.OPEN_WEBUI_CLIENT_ID,
            )
            first_task = asyncio.create_task(collect(first))
            while call_count < 1:
                await asyncio.sleep(0)
            second = await main.call_open_webui_runtime_stream(
                "/v1/chat/completions", {"stream": True}, self.lan_worker_resolution(), "chat",
                conversation_key="same-conversation", owner_id=main.OPEN_WEBUI_CLIENT_ID,
            )
            second_text = await collect(second)
            return await first_task, second_text

        first_text, second_text = asyncio.run(exercise())

        self.assertIn("superseded", first_text.lower())
        self.assertIn("Stopping the previous response", second_text)
        self.assertIn("P40 worker acquired", second_text)
        self.assertEqual(main.open_webui_streams, {})

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

    def test_open_webui_chat_waits_for_busy_scheduler_lease(self) -> None:
        attempts: list[str] = []

        async def runtime_reservation_gate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"allowed": True}

        async def acquire_scheduler_owner(owner: str, _ttl: int) -> dict[str, Any]:
            attempts.append(owner)
            if len(attempts) == 1:
                return {"id": "gpu", "owner": "other", "acquired": False}
            return {"id": "gpu", "owner": owner, "acquired": True}

        self.patch_attr("OPEN_WEBUI_CHAT_LEASE_WAIT_SECONDS", 0.05)
        self.patch_attr("database", type("FakeDatabase", (), {
            "runtime_reservation_gate": staticmethod(runtime_reservation_gate),
            "acquire_scheduler_owner": staticmethod(acquire_scheduler_owner),
        })())

        owner = asyncio.run(
            main.acquire_inference_lease(
                self.resolution(),
                "chat",
                owner_id=main.OPEN_WEBUI_CLIENT_ID,
            )
        )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(owner, attempts[-1])

    def test_non_open_webui_chat_waits_for_stream_finalizer_handoff(self) -> None:
        attempts: list[str] = []

        async def runtime_reservation_gate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"allowed": True}

        async def acquire_scheduler_owner(owner: str, _ttl: int) -> dict[str, Any]:
            attempts.append(owner)
            if len(attempts) == 1:
                return {"id": "gpu", "owner": "finishing-stream", "acquired": False}
            return {"id": "gpu", "owner": owner, "acquired": True}

        self.patch_attr("SYNC_INFERENCE_HANDOFF_WAIT_SECONDS", 0.05)
        self.patch_attr("database", type("FakeDatabase", (), {
            "runtime_reservation_gate": staticmethod(runtime_reservation_gate),
            "acquire_scheduler_owner": staticmethod(acquire_scheduler_owner),
        })())

        owner = asyncio.run(main.acquire_inference_lease(self.resolution(), "chat", owner_id="other_client"))

        self.assertEqual(len(attempts), 2)
        self.assertEqual(owner, attempts[-1])

    def test_non_open_webui_chat_fails_after_bounded_handoff_wait(self) -> None:
        attempts = 0

        async def runtime_reservation_gate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"allowed": True}

        async def acquire_scheduler_owner(_owner: str, _ttl: int) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            return {"id": "gpu", "owner": "other", "acquired": False}

        self.patch_attr("SYNC_INFERENCE_HANDOFF_WAIT_SECONDS", 0.01)
        self.patch_attr("database", type("FakeDatabase", (), {
            "runtime_reservation_gate": staticmethod(runtime_reservation_gate),
            "acquire_scheduler_owner": staticmethod(acquire_scheduler_owner),
        })())

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.acquire_inference_lease(self.resolution(), "chat", owner_id="other_client"))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["waited_seconds"], 0.01)
        self.assertGreaterEqual(attempts, 1)

    def test_prepare_sync_gpu_runtime_checks_hardware_policy_after_cleanup(self) -> None:
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

        calls: list[str] = []

        class FakeRunner:
            async def unload_gpu_runtimes_for_job(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
                calls.append("unload")
                return []

            async def current_runtime_state_by_name(self) -> dict[str, dict[str, Any]]:
                return {}

            def runtime_state_has_active_model(self, state: dict[str, Any] | None) -> bool:
                return False

            def runtime_state_matches_job_model(self, state: dict[str, Any] | None, job: dict[str, Any]) -> bool:
                return False

            async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
                calls.append("verify")

            async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("cold load must not run after hardware admission fails")

            async def warm_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("warm must not run after hardware admission fails")

        async def current_runtime_state(_runtime: str):
            return None

        self.patch_attr("runtime_agent_get", runtime_agent_get)
        self.patch_attr("current_runtime_state", current_runtime_state)
        self.patch_attr("runtime_control_runner", lambda _ttl: FakeRunner())

        with self.assertRaises(main.HTTPException) as ctx:
            asyncio.run(main.prepare_sync_gpu_runtime(self.resolution(), "chat"))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["code"], "hardware_resource_policy")
        check = ctx.exception.detail["hardware_resource_policy"]
        self.assertEqual(check["name"], "hardware:resource-policy")
        self.assertEqual(check["status"], "failed")
        self.assertIn("largest GPU VRAM is 6144 MiB", check["detail"])
        self.assertEqual(check["data"]["observed"]["largest_gpu_memory_total_mib"], 6144)
        self.assertEqual(calls, ["unload", "verify"])

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
