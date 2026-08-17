from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import secret_store  # noqa: E402


@unittest.skipIf(secret_store.AESGCM is None, "cryptography is not installed in this lightweight test environment")
class SecretStoreTests(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip_uses_bound_name_and_redacts_public_record(self) -> None:
        master_key = "m" * 64
        created_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

        envelope = secret_store.encrypt_value(master_key, "remote:openai", "sk-test-value", created_at=created_at)

        self.assertEqual(envelope["scheme"], secret_store.ENVELOPE_SCHEME)
        self.assertEqual(envelope["key_id"], secret_store.master_key_fingerprint(master_key))
        self.assertNotIn("sk-test-value", str(envelope))
        self.assertEqual(secret_store.decrypt_value(master_key, "remote:openai", envelope), "sk-test-value")
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.decrypt_value(master_key, "remote:other", envelope)

        public = secret_store.public_secret_record(
            {
                "name": "remote:openai",
                "display_name": "OpenAI optional provider",
                "category": "remote-provider",
                "description": "disabled by default",
                "secret_envelope": envelope,
                "created_at": created_at,
                "updated_at": created_at,
                "deleted_at": None,
            }
        )

        self.assertTrue(public["encrypted"])
        self.assertEqual(public["key_id"], envelope["key_id"])
        self.assertNotIn("ciphertext", public)
        self.assertNotIn("nonce", public)

    def test_wrong_key_and_invalid_inputs_fail_closed(self) -> None:
        envelope = secret_store.encrypt_value("a" * 64, "model-download:hf", "hf_token")

        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.decrypt_value("b" * 64, "model-download:hf", envelope)
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.encrypt_value("short", "model-download:hf", "value")
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.encrypt_value("a" * 64, "../escape", "value")
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.encrypt_value("a" * 64, "ok", "")
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.validate_secret_category("unsupported")


if __name__ == "__main__":
    unittest.main()
