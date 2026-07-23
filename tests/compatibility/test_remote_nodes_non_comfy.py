from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "comfyui-b1-remote-nodes"))

from comfyui_b1_remote_nodes import nodes  # noqa: E402


@unittest.skipUnless(os.getenv("B1_REMOTE_NODES_LIVE_TEST") == "1", "set B1_REMOTE_NODES_LIVE_TEST=1 to run live remote-node compatibility tests")
class RemoteNodesNonComfyCompatibilityTests(unittest.TestCase):
    def test_tts_fast_uses_unified_api_without_server_side_comfyui(self) -> None:
        self.assertTrue(os.getenv("B1_AI_HUB_API_KEY"), "B1_AI_HUB_API_KEY is required")
        output_dir = Path(os.getenv("B1_AI_HUB_DOWNLOAD_DIR", "b1-artifacts")).resolve()
        file_path, byte_count, digest = nodes.B1TextToSpeech().run(
            "tts-fast",
            "B1 remote-node non-Comfy compatibility test.",
            "default",
            runtime_policy="non_comfy_only",
            filename="b1-remote-node-non-comfy.wav",
        )
        self.assertEqual(Path(file_path).parent.resolve(), output_dir)
        self.assertGreater(byte_count, 0)
        self.assertEqual(len(digest), 64)
        self.assertTrue(Path(file_path).is_file())


if __name__ == "__main__":
    unittest.main()
