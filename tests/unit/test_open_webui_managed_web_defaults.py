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
    def test_patch_enables_web_search_for_every_ui_request_and_is_idempotent(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text(
                "import os\n\n"
                "async def process(form_data):\n"
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
        compile(source, str(path), "exec")

    def test_patch_fails_closed_when_upstream_anchor_changes(self) -> None:
        patcher = load_patcher()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "middleware.py"
            path.write_text("async def process(form_data):\n    return {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "features anchor"):
                patcher.patch_managed_web_defaults(path)


if __name__ == "__main__":
    unittest.main()
