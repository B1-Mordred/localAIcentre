from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHER_PATH = ROOT / "deploy" / "open-webui" / "patch_managed_web_defaults.py"


def load_patcher():
    spec = importlib.util.spec_from_file_location("b1_open_webui_managed_web_defaults_test", PATCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenWebUIManagedWebDefaultsTests(unittest.TestCase):
    def test_patch_keeps_native_web_tools_for_compatible_models_and_is_idempotent(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text(
                "import os\n\n"
                "async def process(form_data, model):\n"
                "    features = form_data.pop('features', None) or {}\n"
                "    return features\n",
                encoding="utf-8",
            )

            patcher.patch_managed_web_defaults(path)
            patcher.patch_managed_web_defaults(path)
            source = path.read_text(encoding="utf-8")

        self.assertEqual(source.count("B1_OPEN_WEBUI_DEFAULT_WEB_ACCESS"), 1)
        self.assertIn("features['web_search'] = True", source)
        self.assertLess(source.index("features = form_data.pop"), source.index("features['web_search'] = True"))
        namespace: dict[str, object] = {}
        exec(compile(source, str(path), "exec"), namespace)

        import asyncio

        process = namespace["process"]
        deepseek = {
            "info": {
                "meta": {
                    "capabilities": {
                        "chat_template": "unsloth_deepseek4_e643c31fcec17f34",
                        "reasoning_content": True,
                    }
                }
            }
        }
        ordinary = {"info": {"meta": {"capabilities": {}}}}
        self.assertEqual(asyncio.run(process({}, deepseek)), {"web_search": True})
        self.assertEqual(asyncio.run(process({}, ordinary)), {"web_search": True})

    def test_patch_routes_laguna_web_access_through_b1_managed_fallback(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text(
                "import os\n\n"
                "async def process(form_data, model):\n"
                "    features = form_data.pop('features', None) or {}\n"
                "    return features\n",
                encoding="utf-8",
            )
            patcher.patch_managed_web_defaults(path)
            namespace: dict[str, object] = {}
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)

        import asyncio

        laguna_internal = {
            "info": {
                "meta": {
                    "capabilities": {
                        "chat_template": "laguna_glm_thinking_v8",
                        "reasoning_content": True,
                    }
                }
            }
        }
        laguna_live = {
            "capabilities": {
                "chat_template": "laguna_glm_thinking_v8",
                "reasoning_content": True,
            },
            "openai": {
                "capabilities": {
                    "chat_template": "laguna_glm_thinking_v8",
                    "reasoning_content": True,
                }
            },
        }
        laguna_upstream_only = {"openai": laguna_live["openai"]}
        process = namespace["process"]
        for laguna in (laguna_internal, laguna_live, laguna_upstream_only):
            self.assertEqual(asyncio.run(process({}, laguna)), {})
            self.assertEqual(asyncio.run(process({"features": {"web_search": True}}, laguna)), {})

    def test_patch_fails_closed_when_upstream_anchor_changes(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text("async def process(form_data):\n    return {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "features anchor"):
                patcher.patch_managed_web_defaults(path)


if __name__ == "__main__":
    unittest.main()
