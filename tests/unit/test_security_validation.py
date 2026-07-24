from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

import app.security as security  # noqa: E402
from app.security import canonical_artifact_name, has_credential_query_parameter, is_safe_public_import_url, query_key_may_carry_credentials  # noqa: E402


class SecurityValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_resolver(["93.184.216.34"])

    def patch_resolver(self, addresses: list[str], *, raises: OSError | None = None) -> None:
        original = security.resolve_hostname_addresses

        def fake_resolver(hostname: str, port: int | None) -> list[str]:
            self.resolver_calls.append({"hostname": hostname, "port": port})
            if raises is not None:
                raise raises
            return list(addresses)

        self.resolver_calls: list[dict[str, Any]] = []
        security.resolve_hostname_addresses = fake_resolver
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original))

    def test_import_url_rejects_private_and_loopback_targets(self) -> None:
        self.assertFalse(is_safe_public_import_url("http://example.com/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://127.0.0.1/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://192.168.2.2/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(is_safe_public_import_url("https://[::1]/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://localhost/model.gguf"))
        self.assertFalse(is_safe_public_import_url("https://updates.localhost/model.gguf"))

    def test_import_url_rejects_private_dns_answers_and_dns_failures(self) -> None:
        self.patch_resolver(["93.184.216.34", "10.0.0.8"])
        self.assertFalse(is_safe_public_import_url("https://models.example.org/model.gguf"))
        self.assertEqual(self.resolver_calls[-1], {"hostname": "models.example.org", "port": None})

        self.patch_resolver([], raises=OSError("dns unavailable"))
        self.assertFalse(is_safe_public_import_url("https://models.example.org/model.gguf"))

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

    def test_credential_query_classifier_detects_signed_url_and_token_keys(self) -> None:
        sensitive_keys = [
            "token",
            "download_token",
            "access_token",
            "api-key",
            "X-Amz-Signature",
            "X-Amz-Credential",
            "X-Goog-Signature",
            "AWSAccessKeyId",
            "sig",
            "SharedAccessSignature",
        ]
        for key in sensitive_keys:
            with self.subTest(key=key):
                self.assertTrue(query_key_may_carry_credentials(key))

        self.assertFalse(query_key_may_carry_credentials("download"))
        self.assertFalse(query_key_may_carry_credentials("filename"))
        self.assertTrue(has_credential_query_parameter("download=1&X-Amz-Signature=secret"))
        self.assertFalse(has_credential_query_parameter("download=1&filename=model.gguf"))

    def test_artifact_names_are_canonicalized(self) -> None:
        self.assertEqual(canonical_artifact_name("../safe.png"), "safe.png")
        with self.assertRaises(ValueError):
            canonical_artifact_name("")


if __name__ == "__main__":
    unittest.main()
