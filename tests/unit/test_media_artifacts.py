from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import media_artifacts  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt "


class MediaArtifactTests(unittest.TestCase):
    def test_staged_input_round_trip_sniffs_and_hashes_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client/../one",
                field_name="image",
                filename="../input.png",
                declared_mime_type="application/octet-stream",
                content=PNG_BYTES,
            )

            self.assertEqual(reference["source"], "staged_upload")
            self.assertEqual(reference["kind"], "image")
            self.assertEqual(reference["mime_type"], "image/png")
            self.assertEqual(reference["sha256"], hashlib.sha256(PNG_BYTES).hexdigest())
            self.assertTrue(reference["path"].startswith("inputs/client_.._one/upload_"))
            content, mime_type, filename = media_artifacts.read_staged_input_bytes(root, reference)
            self.assertEqual(content, PNG_BYTES)
            self.assertEqual(mime_type, "image/png")
            self.assertEqual(filename, "input.png")

    def test_staged_input_rejects_unknown_media_and_oversize_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                media_artifacts.write_staged_input_bytes(
                    root,
                    owner_id="client",
                    field_name="file",
                    content=b"not media",
                    declared_mime_type="text/plain",
                )
            with self.assertRaises(ValueError):
                media_artifacts.write_staged_input_bytes(
                    root,
                    owner_id="client",
                    field_name="audio",
                    content=WAV_BYTES,
                    declared_mime_type="audio/wav",
                    max_size_bytes=len(WAV_BYTES) - 1,
                )

    def test_staged_input_does_not_trust_declared_allowed_mime_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "unsupported media input type"):
                media_artifacts.write_staged_input_bytes(
                    root,
                    owner_id="client",
                    field_name="image",
                    content=b"not really a png",
                    declared_mime_type="image/png",
                    filename="claimed.png",
                )

            reference = media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client",
                field_name="image",
                content=PNG_BYTES,
                declared_mime_type="image/jpeg",
                filename="declared.jpg",
            )
            self.assertEqual(reference["mime_type"], "image/png")
            self.assertEqual(reference["filename"], "declared.jpg.png")

    def test_read_staged_input_rejects_non_input_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "localai" / "job").mkdir(parents=True)
            (root / "localai" / "job" / "0.png").write_bytes(PNG_BYTES)
            with self.assertRaises(ValueError):
                media_artifacts.read_staged_input_bytes(
                    root,
                    {
                        "source": "staged_upload",
                        "path": "localai/job/0.png",
                        "mime_type": "image/png",
                        "bytes": len(PNG_BYTES),
                        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
                    },
                )


if __name__ == "__main__":
    unittest.main()
