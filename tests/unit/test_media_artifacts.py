from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import media_artifacts  # noqa: E402


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8ffff3f0005fe02fea7f3c553"
    "0000000049454e44ae426082"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
WEBP_BYTES = b"RIFF" + (22).to_bytes(4, "little") + b"WEBP" + b"VP8X" + (10).to_bytes(4, "little") + (b"\x00" * 10)
GIF_BYTES = b"GIF89a" + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + b"\x00\x00\x00;"
WAV_BYTES = (
    b"RIFF"
    + (38).to_bytes(4, "little")
    + b"WAVE"
    + b"fmt "
    + (16).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little")
    + (16000).to_bytes(4, "little")
    + (32000).to_bytes(4, "little")
    + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little")
    + b"data"
    + (2).to_bytes(4, "little")
    + b"\x00\x00"
)
MP3_BYTES = b"\xff\xfb\x90\x64" + (b"\x00" * 10)
OGG_BYTES = b"OggS" + b"\x00" + b"\x02" + (b"\x00" * 20) + b"\x01\x00"
MP4_BYTES = (
    (24).to_bytes(4, "big")
    + b"ftyp"
    + b"isom"
    + (512).to_bytes(4, "big")
    + b"isomiso2"
)
WEBM_BYTES = b"\x1a\x45\xdf\xa3\x42\x82\x84webm" + (b"\x00" * 8)


class MediaArtifactTests(unittest.TestCase):
    def test_sniffs_supported_structural_media_types(self) -> None:
        for content, expected in (
            (PNG_BYTES, "image/png"),
            (JPEG_BYTES, "image/jpeg"),
            (WEBP_BYTES, "image/webp"),
            (GIF_BYTES, "image/gif"),
            (WAV_BYTES, "audio/wav"),
            (MP3_BYTES, "audio/mpeg"),
            (OGG_BYTES, "audio/ogg"),
            (MP4_BYTES, "video/mp4"),
            (WEBM_BYTES, "video/webm"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(media_artifacts.sniff_media_mime_type(content), expected)

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

    def test_staged_input_rejects_header_only_media_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content, declared_mime_type in (
                ("prefix.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
                ("prefix.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
                ("prefix.webp", b"RIFFxxxxWEBP", "image/webp"),
            ):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "unsupported media input type"):
                        media_artifacts.write_staged_input_bytes(
                            root,
                            owner_id="client",
                            field_name="media",
                            content=content,
                            declared_mime_type=declared_mime_type,
                            filename=name,
                        )

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
