from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_proxy():
    path = ROOT / "deploy" / "voicebox" / "b1_voicebox_proxy.py"
    spec = importlib.util.spec_from_file_location("b1_voicebox_proxy_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Voicebox proxy")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeManager:
    def __init__(self) -> None:
        self.restarts: list[tuple[float, str]] = []

    def restart(self, timeout_seconds: float, reason: str) -> dict[str, object]:
        self.restarts.append((timeout_seconds, reason))
        return {"strategy": "upstream_process_restart", "pid": 123, "reason": reason}


class VoiceboxProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy = load_proxy()

    def test_model_matching_accepts_folder_path_basename_and_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engines" / "quality").mkdir(parents=True)
            (root / "engines" / "quality" / "voicebox-quality.safetensors").write_text("model", encoding="utf-8")

            entries = self.proxy.iter_model_entries((root,), max_entries=100)

        self.assertIn("engines/quality/voicebox-quality.safetensors", entries)
        self.assertTrue(self.proxy.model_available(["engines/quality/voicebox-quality.safetensors"], entries))
        self.assertTrue(self.proxy.model_available(["voicebox-quality.safetensors"], entries))
        self.assertTrue(self.proxy.model_available(["voicebox-quality"], entries))
        self.assertFalse(self.proxy.model_available(["other"], entries))

    def test_strict_model_list_missing_model_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"B1_VOICEBOX_HOOK_MODEL_ROOTS": tmp, "B1_VOICEBOX_HOOK_STRICT_MODEL_LIST": "true"},
                clear=False,
            ):
                result = self.proxy.handle_load({"model": "missing-model"})

        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["reason"], "model_not_listed")

    def test_warm_defaults_to_unconfirmed_without_synthesis_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "B1_VOICEBOX_HOOK_MODEL_ROOTS": tmp,
                    "B1_VOICEBOX_HOOK_WARM_ENABLED": "false",
                    "B1_VOICEBOX_HOOK_STRICT_MODEL_LIST": "false",
                },
                clear=False,
            ):
                result = asyncio.run(self.proxy.handle_warm({"model": "voicebox-quality"}))

        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["reason"], "warm_disabled")

    def test_unload_restarts_upstream_when_idle(self) -> None:
        manager = FakeManager()
        tracker = self.proxy.NativeRequestTracker()

        with patch.dict("os.environ", {"B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD": "true", "B1_VOICEBOX_HOOK_UNLOAD_TIMEOUT_SECONDS": "7"}, clear=False):
            result = self.proxy.handle_unload({}, manager, tracker)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "upstream_process_restart")
        self.assertEqual(result["pid"], 123)
        self.assertEqual(manager.restarts, [(7.0, "b1_runtime_unload")])

    def test_unload_waits_when_native_requests_are_active(self) -> None:
        manager = FakeManager()
        tracker = self.proxy.NativeRequestTracker()
        tracker.begin()

        result = self.proxy.handle_unload({}, manager, tracker)

        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["reason"], "native_requests_active")
        self.assertEqual(result["active_requests"], 1)
        self.assertEqual(manager.restarts, [])

    def test_runtime_control_auth_requires_configured_bearer_token(self) -> None:
        with patch.dict(
            "os.environ",
            {"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"},
            clear=False,
        ):
            missing = self.proxy.runtime_control_auth_failure({})
            accepted = self.proxy.runtime_control_auth_failure({"authorization": "Bearer hook-token"})

        self.assertEqual(missing[0], 401)
        self.assertEqual(missing[1]["reason"], "runtime_control_token_required")
        self.assertIsNone(accepted)


if __name__ == "__main__":
    unittest.main()
