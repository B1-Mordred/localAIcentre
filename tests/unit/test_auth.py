from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.auth import (  # noqa: E402
    Role,
    generate_api_key,
    generate_csrf_token,
    generate_session_token,
    hash_api_key,
    hash_password,
    hash_session_token,
    key_prefix_from_token,
    password_policy_errors,
    scopes_for_role,
    verify_api_key,
    verify_password,
)


class AuthTests(unittest.TestCase):
    def test_api_key_prefix_and_hash_verification(self) -> None:
        prefix, api_key = generate_api_key()
        self.assertTrue(api_key.startswith(prefix + "."))
        self.assertEqual(key_prefix_from_token(api_key), prefix)
        salt, digest = hash_api_key(api_key)
        self.assertTrue(verify_api_key(api_key, salt, digest))
        self.assertFalse(verify_api_key(api_key + "x", salt, digest))

    def test_service_role_scope_grant_is_bounded(self) -> None:
        scopes = scopes_for_role(Role.SERVICE, ["jobs:read", "modelhub:sync"])
        self.assertEqual(scopes, frozenset({"jobs:read", "modelhub:sync"}))
        with self.assertRaises(ValueError):
            scopes_for_role(Role.SERVICE, ["admin:write"])

    def test_admin_can_receive_wildcard_scope(self) -> None:
        self.assertEqual(scopes_for_role(Role.ADMIN), frozenset({"*"}))

    def test_password_policy_and_scrypt_verification(self) -> None:
        self.assertTrue(password_policy_errors("short"))
        password = "Correct-Horse-7"
        encoded = hash_password(password)
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password(password + "x", encoded))
        self.assertFalse(verify_password(password, "pbkdf2$bad"))

    def test_browser_session_tokens_are_random_and_hashed(self) -> None:
        token = generate_session_token()
        self.assertTrue(token.startswith("b1s_"))
        self.assertEqual(len(hash_session_token(token)), 64)
        self.assertNotEqual(hash_session_token(token), hash_session_token(token + "x"))
        self.assertGreaterEqual(len(generate_csrf_token()), 32)


if __name__ == "__main__":
    unittest.main()
