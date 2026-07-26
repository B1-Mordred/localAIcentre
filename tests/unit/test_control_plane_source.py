from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_MAIN = ROOT / "services" / "control-plane" / "app" / "main.py"


class ControlPlaneSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CONTROL_PLANE_MAIN.read_text(encoding="utf-8")

    def test_voicebox_native_speech_compatibility_uses_scheduler_proxy(self) -> None:
        self.assertIn("def native_voicebox_resolution() -> RuntimeResolution:", self.source)
        self.assertIn('path.strip("/") == "v1/audio/speech"', self.source)
        self.assertIn('require_not_in_maintenance("voicebox/native-audio-speech")', self.source)
        self.assertRegex(
            self.source,
            re.compile(
                r"async def proxy_voicebox_compatibility\(.*?"
                r"acquire_inference_lease\(resolution, \"voicebox-native-speech\".*?"
                r"prepare_sync_gpu_runtime\(resolution, \"voicebox-native-speech\"\).*?"
                r"proxy_http_bytes\(\s*settings\.voicebox_url,.*?"
                r"mark_sync_gpu_runtime_idle\(resolution, \"voicebox-native-speech\"\).*?"
                r"release_inference_lease\(lease_owner\)",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.source,
            re.compile(
                r"if compatibility\.startswith\(\"voicebox\"\):\s+"
                r"auth = await require_http_compatibility_access\(.*?\)\s+"
                r"return await proxy_voicebox_compatibility\(path, request, auth\)",
                re.DOTALL,
            ),
        )


if __name__ == "__main__":
    unittest.main()
