from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHER_PATH = ROOT / "deploy" / "open-webui" / "patch_reasoning_tool_loops.py"


def load_patcher():
    spec = importlib.util.spec_from_file_location("b1_open_webui_reasoning_tool_loops_test", PATCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patched_function(patcher):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "middleware.py"
        path.write_text(patcher.FUNCTION_ANCHOR, encoding="utf-8")
        patcher.patch_reasoning_tool_loops(path)
        patcher.patch_reasoning_tool_loops(path)
        source = path.read_text(encoding="utf-8")
    namespace: dict[str, object] = {}
    exec(compile(source, str(path), "exec"), namespace)
    return source, namespace["get_reasoning_format"]


class OpenWebUIReasoningToolLoopTests(unittest.TestCase):
    def test_explicit_capability_preserves_openai_compatible_reasoning(self) -> None:
        patcher = load_patcher()
        source, get_reasoning_format = patched_function(patcher)

        self.assertEqual(source.count("B1 is exposed to OpenWebUI"), 1)
        self.assertEqual(
            get_reasoning_format(
                {
                    "id": "laguna-s-quality",
                    "provider": "",
                    "capabilities": {"reasoning_content": True, "tool_calls": True},
                }
            ),
            "reasoning_content",
        )
        self.assertEqual(
            get_reasoning_format(
                {
                    "id": "deepseek-quality",
                    "provider": "openai",
                    "openai": {"capabilities": {"reasoning_content": True}},
                }
            ),
            "reasoning_content",
        )

    def test_existing_provider_fallbacks_are_unchanged(self) -> None:
        patcher = load_patcher()
        _source, get_reasoning_format = patched_function(patcher)

        self.assertEqual(get_reasoning_format({"provider": "ollama"}), "think_tags")
        self.assertEqual(get_reasoning_format({"provider": "llama.cpp"}), "reasoning_content")

    def test_ordinary_openai_compatible_models_remain_strict(self) -> None:
        patcher = load_patcher()
        _source, get_reasoning_format = patched_function(patcher)

        self.assertIsNone(get_reasoning_format({"id": "chat-default", "provider": "openai"}))
        self.assertIsNone(
            get_reasoning_format(
                {
                    "id": "chat-default",
                    "provider": "openai",
                    "capabilities": {"reasoning_content": False},
                }
            )
        )
        self.assertIsNone(
            get_reasoning_format(
                {
                    "id": "some-model",
                    "provider": "openai",
                    "capabilities": {"thinking": True},
                }
            )
        )

    def test_patch_fails_closed_when_upstream_anchor_changes(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text("def get_reasoning_format(model):\n    return None\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "reasoning-format anchor"):
                patcher.patch_reasoning_tool_loops(path)


if __name__ == "__main__":
    unittest.main()
