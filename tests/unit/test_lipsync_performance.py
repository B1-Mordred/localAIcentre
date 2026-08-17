from __future__ import annotations

import base64
import hashlib
import importlib.util
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
LIPSYNC_MAIN = ROOT / "services" / "lipsync" / "app" / "main.py"
SPEC = importlib.util.spec_from_file_location("b1_lipsync_main", LIPSYNC_MAIN)
assert SPEC is not None and SPEC.loader is not None
lipsync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lipsync
SPEC.loader.exec_module(lipsync)


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

    def test_scene_face_bbox_is_validated_and_bound_to_cache_key(self) -> None:
        payload = self.payload().model_copy(
            update={
                "source_context": "scene_face_region",
                "face_bbox": {"x": 0.1, "y": 0.08, "width": 0.8, "height": 0.84},
            }
        )
        self.assertEqual(lipsync.validated_scene_face_bbox(payload), payload.face_bbox)
        portrait_path = Path(__file__)
        key = lipsync.performance_render_cache_key(payload, PERFORMANCE_PLAN, portrait_path)
        changed = payload.model_copy(update={"face_bbox": {"x": 0.12, "y": 0.08, "width": 0.78, "height": 0.84}})
        self.assertNotEqual(key, lipsync.performance_render_cache_key(changed, PERFORMANCE_PLAN, portrait_path))

    def test_face_bbox_is_rejected_for_plain_portrait(self) -> None:
        payload = self.payload().model_copy(update={"face_bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}})
        with self.assertRaises(lipsync.HTTPException) as raised:
            lipsync.validated_scene_face_bbox(payload)
        self.assertEqual(raised.exception.detail["code"], "face_bbox_not_allowed")

    def test_declared_mouth_fallback_adds_audio_driven_motion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "static.mp4"
            audio = root / "speech.wav"
            output = root / "animated.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (96, 96))
            self.assertTrue(writer.isOpened())
            frame = np.full((96, 96, 3), 180, dtype=np.uint8)
            for _ in range(20):
                writer.write(frame)
            writer.release()
            samples = (np.sin(np.arange(32000) * 2 * np.pi * 220 / 16000) * 12000).astype(np.int16)
            samples[:4000] = 0
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(samples.tobytes())
            bbox = {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}

            envelope = [0.0 if index < 4 else 1.0 for index in range(20)]
            with (
                mock.patch.object(lipsync, "audio_rms_envelope", return_value=envelope),
                mock.patch.object(
                    lipsync,
                    "mux_performance_audio",
                    side_effect=lambda video, _audio, destination: shutil.copyfile(video, destination),
                ),
            ):
                metadata = lipsync.apply_declared_mouth_fallback(
                    source,
                    audio,
                    output,
                    bbox,
                    trigger_score=0.0,
                )

            self.assertTrue(metadata["applied"])
            self.assertGreater(lipsync.mouth_motion_score(output, bbox), lipsync.DECLARED_MOUTH_MINIMUM_MOTION)


if __name__ == "__main__":
    unittest.main()
