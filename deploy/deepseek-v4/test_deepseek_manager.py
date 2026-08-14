from __future__ import annotations

import importlib.util
import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("deepseek_manager.py")
SPEC = importlib.util.spec_from_file_location("deepseek_manager", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load DeepSeek manager")
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.pid = 4242

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        self.returncode = -value

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


class DeepSeekManagerTests(unittest.TestCase):
    def test_responses_reasoning_effort_maps_to_deepseek_template_modes(self) -> None:
        normal = manager.translate_responses_reasoning({"reasoning": {"effort": "low"}})
        high = manager.translate_responses_reasoning({"reasoning": {"effort": "high"}})
        maximum = manager.translate_responses_reasoning({"reasoning": {"effort": "xhigh"}})
        explicit = manager.translate_responses_reasoning(
            {
                "reasoning": {"effort": "xhigh"},
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )

        self.assertEqual(normal["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(high["chat_template_kwargs"], {"enable_thinking": True, "reasoning_effort": "high"})
        self.assertEqual(maximum["chat_template_kwargs"], {"enable_thinking": True, "reasoning_effort": "max"})
        self.assertEqual(explicit["chat_template_kwargs"], {"enable_thinking": False, "reasoning_effort": "max"})
        original = {"reasoning": {"effort": "unknown"}}
        self.assertIs(manager.translate_responses_reasoning(original), original)
        with self.assertRaises(ValueError):
            manager.translate_responses_reasoning(
                {"reasoning": {"effort": "high"}, "chat_template_kwargs": "invalid"}
            )

    def write_profiles(self, payload: dict[str, object]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "profiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def valid_payload(self) -> dict[str, object]:
        return {
            "schema_version": manager.PROFILE_SCHEMA,
            "profiles": [{
                "model_id": "b1-unsloth-deepseek-v4-flash-0731-main",
                "version": "revision-1",
                "artifacts": [
                    {"role": "main-shard-1", "path": "/models/deepseek/00001.gguf",
                     "sha256": "a" * 64, "size_bytes": 1024},
                    {"role": "main-shard-2", "path": "/models/deepseek/00002.gguf",
                     "sha256": "b" * 64, "size_bytes": 2048},
                ],
                "aliases": ["deepseek-main"],
                "server_args": ["--ctx-size", "16384", "--threads", "36"],
            }],
        }

    def quality_profile(self) -> manager.RuntimeProfile:
        payload = self.valid_payload()
        payload["profiles"][0]["model_id"] = "b1-unsloth-deepseek-v4-flash-0731-quality"
        payload["profiles"][0]["aliases"] = ["deepseek-quality"]
        return manager.load_profiles(self.write_profiles(payload)).popitem()[1]

    def test_quality_request_policy_defaults_maps_and_bounds_reasoning(self) -> None:
        profile = self.quality_profile()
        defaulted = manager.apply_quality_request_policy({"model": "deepseek-quality"}, profile)
        self.assertEqual(defaulted["reasoning_budget_tokens"], 4096)
        self.assertEqual(defaulted["temperature"], 1.0)
        for level, expected in manager.DEEPSEEK_QUALITY_REASONING_BUDGETS.items():
            with self.subTest(level=level):
                mapped = manager.apply_quality_request_policy({"reasoning": level}, profile)
                self.assertEqual(mapped["reasoning_budget_tokens"], expected)
                self.assertNotIn("reasoning", mapped)
        for raw in (-1, 8193, True, 1.5, "4096"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                manager.apply_quality_request_policy({"reasoning_budget_tokens": raw}, profile)

    def test_quality_request_policy_preserves_explicit_sampling_and_raw_compatibility(self) -> None:
        profile = self.quality_profile()
        payload = manager.apply_quality_request_policy(
            {"thinking_budget_tokens": 384, "temperature": 0.7, "top_k": 20}, profile
        )
        self.assertEqual(payload["reasoning_budget_tokens"], 384)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["top_k"], 20)
        self.assertNotIn("thinking_budget_tokens", payload)
        with self.assertRaisesRegex(ValueError, "disagree"):
            manager.apply_quality_request_policy(
                {"reasoning_budget_tokens": 2048, "thinking_budget_tokens": 4096}, profile
            )

    def test_quality_request_policy_does_not_modify_main(self) -> None:
        profile = manager.load_profiles(self.write_profiles(self.valid_payload())).popitem()[1]
        payload = {"reasoning": "deep"}
        self.assertIs(manager.apply_quality_request_policy(payload, profile), payload)

    def test_split_artifacts_are_required_and_manager_options_are_rejected(self) -> None:
        profiles = manager.load_profiles(self.write_profiles(self.valid_payload()))
        profile = profiles["b1-unsloth-deepseek-v4-flash-0731-main"]
        self.assertEqual(len(profile.artifacts), 2)
        self.assertEqual(profile.model_path.name, "00001.gguf")
        for option in ("--host", "--parallel", "--cache-ram", "--model-draft", "--alias"):
            with self.subTest(option=option):
                payload = self.valid_payload()
                payload["profiles"][0]["server_args"] = [option, "unsafe"]
                with self.assertRaisesRegex(ValueError, "manager-owned option"):
                    manager.load_profiles(self.write_profiles(payload))

    def test_first_artifact_must_be_primary_split_shard(self) -> None:
        payload = self.valid_payload()
        payload["profiles"][0]["artifacts"][0]["role"] = "main-shard-2"
        with self.assertRaisesRegex(ValueError, "main-shard-1"):
            manager.load_profiles(self.write_profiles(payload))

    def test_duplicate_artifact_paths_are_rejected(self) -> None:
        payload = self.valid_payload()
        payload["profiles"][0]["artifacts"][1]["path"] = payload["profiles"][0]["artifacts"][0]["path"]
        with self.assertRaisesRegex(ValueError, "duplicate paths"):
            manager.load_profiles(self.write_profiles(payload))

    def test_dspark_is_separate_and_no_draft_device_is_injected(self) -> None:
        payload = self.valid_payload()
        payload["profiles"][0]["draft"] = {
            "role": "dspark-draft", "path": "/models/deepseek/dspark.gguf",
            "sha256": "c" * 64, "size_bytes": 4096,
        }
        profile = manager.load_profiles(self.write_profiles(payload)).popitem()[1]
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        command = runtime.command(profile)
        self.assertIn("--spec-draft-model", command)
        self.assertNotIn("--spec-draft-device", command)
        self.assertEqual(command[command.index("--parallel") + 1], "1")
        self.assertEqual(command[command.index("--cache-ram") + 1], "1024")

    def test_inference_cannot_autoload(self) -> None:
        profile = manager.load_profiles(self.write_profiles(self.valid_payload())).popitem()[1]
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        with self.assertRaisesRegex(RuntimeError, "loaded by B1"):
            runtime.enter_request("deepseek-main")

    def test_load_requires_idle_cool_power_capped_gpu(self) -> None:
        profile = manager.load_profiles(self.write_profiles(self.valid_payload())).popitem()[1]
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        safe = {"devices": [{"power_limit_w": 125.0, "temperature_c": 60, "memory_used_mib": 256}]}
        with mock.patch.object(manager, "gpu_metrics", return_value=safe):
            runtime._assert_load_safety()
        busy = {"devices": [{"power_limit_w": 125.0, "temperature_c": 40, "memory_used_mib": 257}]}
        with mock.patch.object(manager, "gpu_metrics", return_value=busy), self.assertRaisesRegex(
            RuntimeError, "not idle"
        ):
            runtime._assert_load_safety()

    def test_all_split_shards_are_attested_and_hash_cache_uses_identity(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        files = []
        artifacts = []
        for index, content in enumerate((b"one", b"two"), start=1):
            path = root / f"{index}.gguf"
            path.write_bytes(content)
            files.append(path)
            artifacts.append(manager.Artifact(
                role=f"main-shard-{index}", path=path,
                sha256=manager.hashlib.sha256(content).hexdigest(), size_bytes=len(content),
            ))
        profile = manager.RuntimeProfile(
            model_id="b1-unsloth-deepseek-v4-flash-0731-main", version="revision-1",
            artifacts=tuple(artifacts), server_args=("--ctx-size", "16384"),
        )
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        with mock.patch.object(manager, "MODEL_ROOT", root), mock.patch.object(
            manager, "sha256", wraps=manager.sha256
        ) as digest_call:
            first = runtime.attest(profile.model_id)
            second = runtime.attest(profile.model_id)
        self.assertEqual([item["path"] for item in first["files"]], ["1.gguf", "2.gguf"])
        self.assertEqual(second["status"], "ok")
        self.assertEqual(digest_call.call_count, 2)

    def test_unknown_models_never_fall_through_to_another_backend(self) -> None:
        profile = manager.load_profiles(self.write_profiles(self.valid_payload())).popitem()[1]
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        router = manager.Router(runtime)
        status, result = router.lifecycle("load", {"model": "laguna-s-quality"})
        self.assertEqual(status, 404)
        self.assertEqual(result["reason"], "unknown_model")

    def test_graceful_unload_refuses_active_inference_but_cancel_forces_recovery(self) -> None:
        profile = manager.load_profiles(self.write_profiles(self.valid_payload())).popitem()[1]
        runtime = manager.DeepSeekRuntime({profile.model_id: profile})
        self.addCleanup(runtime.shutdown)
        process = FakeProcess()
        runtime.process = process
        runtime.active_model = profile.model_id
        runtime.active_job_id = "job-1"
        runtime.active_requests = 1
        router = manager.Router(runtime)

        status, graceful = router.lifecycle("unload", {"model": "deepseek-main"})
        self.assertEqual(status, 503)
        self.assertEqual(graceful["reason"], "RuntimeError")
        self.assertIsNone(process.returncode)
        self.assertEqual(runtime.active_model, profile.model_id)

        status, forced = router.lifecycle("cancel", {"model": "deepseek-main"})
        self.assertEqual(status, 200)
        self.assertEqual(forced["strategy"], "process_stopped")
        self.assertTrue(forced["escalated"])
        self.assertEqual(process.signals, [signal.SIGKILL])
        self.assertEqual(runtime.active_model, "")
        self.assertEqual(runtime.active_job_id, "")

        runtime.leave_request()
        self.assertEqual(runtime.active_requests, 0)


if __name__ == "__main__":
    unittest.main()
