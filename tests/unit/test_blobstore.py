from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BLOBSTORE_PATH = ROOT / "services" / "artifact-server" / "app" / "blobstore.py"
spec = importlib.util.spec_from_file_location("artifact_blobstore", BLOBSTORE_PATH)
assert spec is not None and spec.loader is not None
blobstore = importlib.util.module_from_spec(spec)
sys.modules["artifact_blobstore"] = blobstore
spec.loader.exec_module(blobstore)

BlobStoreError = blobstore.BlobStoreError
RangeNotSatisfiable = blobstore.RangeNotSatisfiable
etag_for_sha256 = blobstore.etag_for_sha256
if_none_match_matches = blobstore.if_none_match_matches
iter_file_range = blobstore.iter_file_range
parse_byte_range = blobstore.parse_byte_range
resolve_inside = blobstore.resolve_inside
sha256_file = blobstore.sha256_file
validate_sha256 = blobstore.validate_sha256


class BlobStoreTests(unittest.TestCase):
    def test_validate_sha256_normalizes_and_rejects_bad_values(self) -> None:
        digest = "A" * 64
        self.assertEqual(validate_sha256(digest), "a" * 64)
        for value in ("not-a-sha", "a" * 64 + "\n", "a" * 63, "a" * 64 + "0"):
            with self.subTest(value=value):
                with self.assertRaises(BlobStoreError):
                    validate_sha256(value)

    def test_parse_byte_ranges(self) -> None:
        self.assertIsNone(parse_byte_range(None, 10))
        self.assertEqual(parse_byte_range("bytes=0-4", 10), (0, 4))
        self.assertEqual(parse_byte_range("bytes=5-", 10), (5, 9))
        self.assertEqual(parse_byte_range("bytes=-3", 10), (7, 9))
        self.assertEqual(parse_byte_range("bytes=0-99", 10), (0, 9))

        for header in ("items=0-1", "bytes=9-1", "bytes=20-21", "bytes=-0", "bytes=0-1,3-4"):
            with self.assertRaises(RangeNotSatisfiable, msg=header):
                parse_byte_range(header, 10)

    def test_etag_if_none_match(self) -> None:
        digest = "b" * 64
        etag = etag_for_sha256(digest)
        self.assertEqual(etag, '"sha256:' + digest + '"')
        self.assertTrue(if_none_match_matches(etag, etag))
        self.assertTrue(if_none_match_matches('W/"ignored", "sha256:' + digest + '"', etag))
        self.assertTrue(if_none_match_matches('W/"sha256:' + digest + '"', etag))
        self.assertTrue(if_none_match_matches("*", etag))
        self.assertFalse(if_none_match_matches('"other"', etag))

    def test_resolve_inside_and_hash_range_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"0123456789"
            blob = root / "blob"
            blob.write_bytes(payload)
            self.assertEqual(resolve_inside(root, "blob"), blob.resolve())
            self.assertEqual(sha256_file(blob), hashlib.sha256(payload).hexdigest())
            self.assertEqual(b"".join(iter_file_range(blob, 2, 5, chunk_size=2)), b"2345")
            for relative in (
                "",
                "/absolute",
                "../escape",
                "safe/../escape",
                "safe\\escape",
                "safe%2Fescape",
                "safe%5Cescape",
                "safe%3Fquery",
                "safe%23fragment",
                "%00escape",
                "%/escape",
                "%2/escape",
                "%zzescape",
                "%ffescape",
            ):
                with self.subTest(relative=relative):
                    with self.assertRaises(BlobStoreError):
                        resolve_inside(root, relative)


if __name__ == "__main__":
    unittest.main()
