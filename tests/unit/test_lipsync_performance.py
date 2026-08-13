from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "lipsync"))

from app import main as lipsync  # noqa: E402


PERFORMANCE_PLAN = {
    "schema_version": "dialecticore.character_performance.v1",
    "on_camera_energy": "restrained",
    "gaze_style": "reflective",
    "head_motion": "minimal",
    "expression_range": "contained",
    "gesture_frequency": "none",
    "signature_habit": "brief thoughtful pause before emphasis",
    "variation_seed": 2104,
}


class PerformanceRenderCacheTests(unittest.TestCase):
    def payload(self) -> lipsync.LipsyncRequest:
        audio = b"RIFFtest"
        return lipsync.LipsyncRequest(
            portrait_b64=base64.b64encode(b"portrait").decode("ascii"),
            audio_b64=base64.b64encode(audio).decode("ascii"),
            audio_sha256=hashlib.sha256(audio).hexdigest(),
            width=512,
            height=512,
            fps=12,
            duration_ms=4000,
        )

    def test_cache_is_bound_to_plan_and_restores_exact_content(self) -> None:
        original_cache_root = lipsync.CACHE_ROOT
        with tempfile.TemporaryDirectory() as temporary_directory:
            lipsync.CACHE_ROOT = Path(temporary_directory)
            try:
                portrait_path = Path(temporary_directory) / "portrait.png"
                portrait_path.write_bytes(b"portrait")
                payload = self.payload()
                key = lipsync.performance_render_cache_key(payload, PERFORMANCE_PLAN, portrait_path)
                changed_plan = {**PERFORMANCE_PLAN, "variation_seed": 2105}
                self.assertNotEqual(key, lipsync.performance_render_cache_key(payload, changed_plan, portrait_path))
                changed_timing_payload = payload.model_copy(update={"timing_sha256": "b" * 64})
                self.assertNotEqual(key, lipsync.performance_render_cache_key(changed_timing_payload, PERFORMANCE_PLAN, portrait_path))

                evidence = {
                    "mode": "audio_driven_character_performance",
                    "applied": True,
                    "plan_sha256": "a" * 64,
                    "variation_seed": 2104,
                }
                lipsync.store_cached_performance_render(key, b"mp4-bytes", evidence)
                self.assertEqual(
                    lipsync.load_cached_performance_render(key),
                    (b"mp4-bytes", {"performance": evidence, "performance_render_cache": "hit"}),
                )
            finally:
                lipsync.CACHE_ROOT = original_cache_root


if __name__ == "__main__":
    unittest.main()
