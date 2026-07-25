from __future__ import annotations

import unittest

from tests.integration.test_live_installed_workflows import placeholder_proof


class InstalledWorkflowHarnessTests(unittest.TestCase):
    def test_audio_cpu_requires_explicit_non_placeholder_marker(self) -> None:
        proof = placeholder_proof({}, None, runtime="audio-cpu")

        self.assertTrue(proof["placeholder_failure"])
        self.assertIn("audio_cpu_non_placeholder_marker_missing", proof["reasons"])

    def test_audio_cpu_scaffold_header_fails(self) -> None:
        proof = placeholder_proof(
            {"X-B1-Placeholder": "true", "X-B1-CPU-Audio-Engine": "scaffold"},
            None,
            runtime="audio-cpu",
        )

        self.assertTrue(proof["placeholder_failure"])
        self.assertIn("explicit_placeholder_marker", proof["reasons"])
        self.assertIn("scaffold_cpu_audio_engine", proof["reasons"])

    def test_audio_cpu_false_header_passes(self) -> None:
        proof = placeholder_proof(
            {"X-B1-Placeholder": "false", "X-B1-CPU-Audio-Engine": "piper"},
            None,
            runtime="audio-cpu",
        )

        self.assertFalse(proof["placeholder_failure"])
        self.assertFalse(proof["placeholder"])
        self.assertEqual(proof["cpu_audio_engine"], "piper")

    def test_audio_cpu_stt_false_body_passes(self) -> None:
        proof = placeholder_proof({}, {"b1_placeholder": False, "b1_stt_engine": "vosk"}, runtime="audio-cpu")

        self.assertFalse(proof["placeholder_failure"])
        self.assertFalse(proof["placeholder"])
        self.assertEqual(proof["cpu_audio_engine"], "vosk")

    def test_non_audio_runtime_without_marker_passes(self) -> None:
        proof = placeholder_proof({}, {"choices": [{"message": {"content": "ready"}}]}, runtime="localai")

        self.assertFalse(proof["placeholder_failure"])
        self.assertIsNone(proof["placeholder"])


if __name__ == "__main__":
    unittest.main()
