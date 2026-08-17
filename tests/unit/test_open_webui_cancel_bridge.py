from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "deploy" / "open-webui" / "b1_cancel_bridge.py"
PATCHER_PATH = ROOT / "deploy" / "open-webui" / "patch_tasks.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenWebUICancelBridgeTests(unittest.IsolatedAsyncioTestCase):
    def load_bridge(self, name: str):
        fake_aiohttp = types.SimpleNamespace(
            ClientTimeout=lambda *, total: types.SimpleNamespace(total=total),
            ClientSession=None,
        )
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            return load_module(name, BRIDGE_PATH)

    async def test_bridge_calls_authenticated_controller_cancel_before_local_stop(self) -> None:
        bridge = self.load_bridge("b1_open_webui_cancel_bridge_test")
        calls: list[dict[str, object]] = []

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self) -> str:
                return "ok"

        class FakeSession:
            def __init__(self, *, timeout):
                calls.append({"timeout": timeout.total})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, url, *, headers, json):
                calls.append({"url": url, "headers": headers, "json": json})
                return FakeResponse()

        with patch.dict(
            os.environ,
            {
                "B1_OPEN_WEBUI_API_BASE_URL": "http://control-plane:8000/v1/",
                "OPENAI_API_KEY": "internal-test-key",
            },
            clear=False,
        ), patch.object(bridge.aiohttp, "ClientSession", FakeSession):
            await bridge.cancel_b1_chat_stream("chat-123")

        self.assertEqual(calls[0], {"timeout": 20})
        self.assertEqual(
            calls[1],
            {
                "url": "http://control-plane:8000/v1/open-webui/chat/cancel",
                "headers": {"Authorization": "Bearer internal-test-key"},
                "json": {"chat_id": "chat-123"},
            },
        )

    async def test_bridge_is_noop_without_internal_configuration(self) -> None:
        bridge = self.load_bridge("b1_open_webui_cancel_bridge_noop_test")

        class UnexpectedSession:
            def __init__(self, **_kwargs):
                raise AssertionError("unconfigured bridge must not open a session")

        with patch.dict(os.environ, {"B1_OPEN_WEBUI_API_BASE_URL": "", "OPENAI_API_KEY": ""}), patch.object(
            bridge.aiohttp, "ClientSession", UnexpectedSession
        ):
            await bridge.cancel_b1_chat_stream("chat-123")


class OpenWebUITasksPatchTests(unittest.TestCase):
    def test_patch_injects_cancel_bridge_once(self) -> None:
        patcher = load_module("b1_open_webui_tasks_patcher_test", PATCHER_PATH)
        original = """from open_webui.env import REDIS_KEY_PREFIX

async def stop_item_tasks(redis: Redis, item_id: str):
    \"\"\"
    Stop all tasks associated with a specific item ID.
    \"\"\"
    task_ids = []
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.py"
            path.write_text(original, encoding="utf-8")
            patcher.patch_tasks(path)
            patcher.patch_tasks(path)
            patched = path.read_text(encoding="utf-8")

        self.assertEqual(patched.count("from open_webui.b1_cancel_bridge import cancel_b1_chat_stream"), 1)
        self.assertEqual(patched.count("await cancel_b1_chat_stream(item_id)"), 1)
        self.assertLess(patched.index("await cancel_b1_chat_stream(item_id)"), patched.index("task_ids = []"))


if __name__ == "__main__":
    unittest.main()
