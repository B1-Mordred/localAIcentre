from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.security import canonical_artifact_name, is_safe_public_import_url  # noqa: E402


class SecurityValidationTests(unittest.TestCase):
    def test_import_url_rejects_private_and_loopback_targets(self) -> None:
        self.assertFalse(is_safe_public_import_url("http://example.com/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://127.0.0.1/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://192.168.2.2/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(is_safe_public_import_url("https://[::1]/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://localhost/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://updates.localhost/model.gguf"))

    def test_import_url_rejects_credential_fragments_bad_ports_and_traversal(self) -> None:
        unsafe_urls = [
            "https://user:pass@example.com/model.gguf",
            "https://example.com/model.gguf#sha256",
            "https://example.com:badport/model.gguf",
            "https://example.com/../model.gguf",
            "https://example.com/%2e%2e/model.gguf",
        ]
        for url in unsafe_urls:
            with self.subTest(url=url):
                self.assertFalse(is_safe_public_import_url(url))

    def test_import_url_allows_public_https_with_optional_host_policy(self) -> None:
        self.assertTrue(is_safe_public_import_url("https://huggingface.co/org/model"))
        self.assertTrue(is_safe_public_import_url("https://huggingface.co/org/model", {"HUGGINGFACE.CO."}))
        self.assertFalse(is_safe_public_import_url("https://example.com/model", {"huggingface.co"}))
        self.assertTrue(is_safe_public_import_url("https://cdn-lfs.huggingface.co/model?X-Amz-Signature=opaque"))

    def test_artifact_names_are_canonicalized(self) -> None:
        self.assertEqual(canonical_artifact_name("../safe.png"), "safe.png")
        with self.assertRaises(ValueError):
            canonical_artifact_name("")


if __name__ == "__main__":
    unittest.main()
