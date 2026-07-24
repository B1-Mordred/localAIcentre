from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import job_redaction  # noqa: E402


class JobRedactionTests(unittest.TestCase):
    def test_media_job_redaction_preserves_reproducibility_metadata(self) -> None:
        redacted = job_redaction.redact_request(
            {
                "modality": "image",
                "operation": "generation",
                "model": "image-default",
                "priority": "single_image",
                "runtime_policy": "any",
                "input": {
                    "workflow_id": "text-to-image",
                    "workflow_version": "1.0.0",
                    "parameters": {
                        "prompt": "secret castle prompt",
                        "negative_prompt": "secret negative prompt",
                        "seed": 42,
                        "steps": 18,
                        "width": 1024,
                        "height": 768,
                        "image": {
                            "source": "staged_upload",
                            "filename": "private-face.png",
                            "path": "/srv/b1-ai-hub/artifacts/temporary/upload.png",
                            "sha256": "a" * 64,
                            "bytes": 1200,
                        },
                    },
                },
            }
        )

        self.assertEqual(redacted["model"], "image-default")
        self.assertEqual(redacted["priority"], "single_image")
        self.assertEqual(redacted["runtime_policy"], "any")
        self.assertEqual(redacted["input"]["workflow_id"], "text-to-image")
        self.assertEqual(redacted["input"]["workflow_version"], "1.0.0")
        self.assertEqual(redacted["input"]["parameters"]["seed"], 42)
        self.assertEqual(redacted["input"]["parameters"]["steps"], 18)
        self.assertEqual(redacted["input"]["parameters"]["width"], 1024)
        self.assertEqual(redacted["input"]["parameters"]["height"], 768)
        self.assertEqual(redacted["input"]["parameters"]["prompt"], job_redaction.REDACTED)
        self.assertEqual(redacted["input"]["parameters"]["negative_prompt"], job_redaction.REDACTED)
        self.assertEqual(redacted["input"]["parameters"]["image"], job_redaction.REDACTED)
        self.assertNotIn("secret castle prompt", str(redacted))
        self.assertNotIn("private-face.png", str(redacted))
        self.assertNotIn("/srv/b1-ai-hub", str(redacted))
        self.assertNotIn("a" * 64, str(redacted))

    def test_non_object_input_is_redacted(self) -> None:
        redacted = job_redaction.redact_request({"model": "embedding-default", "input": "private text"})

        self.assertEqual(redacted["model"], "embedding-default")
        self.assertEqual(redacted["input"], job_redaction.REDACTED)
        self.assertNotIn("private text", str(redacted))

    def test_secret_shaped_strings_are_redacted_inside_safe_fields(self) -> None:
        redacted = job_redaction.redact_request(
            {
                "model": "github_pat_abc123",
                "source": "https://example.invalid/model.bin?download_token=secret-value",
                "headers": "Authorization: Bearer b1adm_secret",
            }
        )

        self.assertEqual(redacted["model"], job_redaction.REDACTED)
        self.assertIn("download_token=<redacted>", redacted["source"])
        self.assertEqual(redacted["headers"], job_redaction.REDACTED)
        self.assertNotIn("github_pat_abc123", str(redacted))
        self.assertNotIn("secret-value", str(redacted))
        self.assertNotIn("b1adm_secret", str(redacted))

    def test_lists_and_strings_are_bounded(self) -> None:
        redacted = job_redaction.redact_request({"parameters": {"values": list(range(25)), "note": "x" * 700}})

        self.assertEqual(redacted["parameters"]["values"][-1], {"truncated_items": 5})
        self.assertTrue(redacted["parameters"]["note"].endswith("...<truncated>"))


if __name__ == "__main__":
    unittest.main()
