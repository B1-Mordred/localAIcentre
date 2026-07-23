from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main, secret_store  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    secret_store = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None or secret_store.AESGCM is None, f"{MISSING_DEPENDENCY or 'cryptography'} is not installed in this lightweight test environment")
class EncryptedSecretsApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(
        self,
        store: dict[str, dict[str, Any]],
        audit_events: list[dict[str, Any]],
        *,
        role: Any = None,
        scopes: frozenset[str] = frozenset({"*"}),
        master_key: str = "m" * 64,
    ) -> None:
        role = role or Role.ADMIN

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_list_encrypted_secrets(*, category: str | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
            rows = list(store.values())
            if category:
                rows = [row for row in rows if row["category"] == category]
            if not include_deleted:
                rows = [row for row in rows if row.get("deleted_at") is None]
            return rows

        async def fake_get_encrypted_secret(name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
            row = store.get(name)
            if row is None or (row.get("deleted_at") is not None and not include_deleted):
                return None
            return row

        async def fake_upsert_encrypted_secret(payload: dict[str, Any]) -> dict[str, Any]:
            now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
            existing = store.get(payload["name"])
            row = {
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "deleted_at": None,
                **payload,
            }
            store[payload["name"]] = row
            return row

        async def fake_delete_encrypted_secret(name: str) -> dict[str, Any] | None:
            row = store.get(name)
            if row is None:
                return None
            deleted = {**row, "deleted_at": datetime(2026, 7, 22, 12, 1, tzinfo=UTC), "updated_at": datetime(2026, 7, 22, 12, 1, tzinfo=UTC)}
            store[name] = deleted
            return deleted

        self.patch_attr("settings", replace(main.settings, master_key=master_key))
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("database", type("FakeDatabase", (), {
            "list_encrypted_secrets": staticmethod(fake_list_encrypted_secrets),
            "get_encrypted_secret": staticmethod(fake_get_encrypted_secret),
            "upsert_encrypted_secret": staticmethod(fake_upsert_encrypted_secret),
            "delete_encrypted_secret": staticmethod(fake_delete_encrypted_secret),
        }))

    def test_store_verify_list_and_delete_never_return_secret_material(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(store, audit_events)

        created = asyncio.run(
            main.admin_encrypted_secret_set(
                "remote:openai",
                main.EncryptedSecretSetRequest(
                    display_name="OpenAI optional provider",
                    category="remote-provider",
                    description="disabled by default",
                    value="sk-test-value",
                ),
            )
        )

        self.assertEqual(created["name"], "remote:openai")
        self.assertTrue(created["encrypted"])
        self.assertNotIn("value", created)
        self.assertNotIn("secret_envelope", created)
        self.assertNotIn("ciphertext", created)
        stored_envelope = store["remote:openai"]["secret_envelope"]
        self.assertIn("ciphertext", stored_envelope)
        self.assertNotIn("sk-test-value", str(stored_envelope))

        listed = asyncio.run(main.admin_encrypted_secrets(category=None, include_deleted=False))
        self.assertEqual(listed["object"], "list")
        self.assertEqual(listed["master_key"]["usable"], True)
        self.assertEqual(listed["data"][0]["name"], "remote:openai")
        self.assertNotIn("ciphertext", listed["data"][0])

        verified = asyncio.run(main.admin_encrypted_secret_verify("remote:openai"))
        self.assertEqual(verified["status"], "verified")
        self.assertNotIn("sk-test-value", str(verified))

        deleted = asyncio.run(main.admin_encrypted_secret_delete("remote:openai"))
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(audit_events[0]["event_type"], "encrypted_value.upserted")
        self.assertEqual(audit_events[1]["event_type"], "encrypted_value.deleted")

    def test_secret_routes_reject_non_admin_and_missing_master_key(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(store, audit_events, role=Role.OPERATOR, scopes=frozenset({"admin:read"}))

        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_encrypted_secrets(category=None, include_deleted=False))
        self.assertEqual(exc.exception.status_code, 403)

        self.patch_common(store, audit_events, master_key="")
        with self.assertRaises(HTTPException) as missing:
            asyncio.run(
                main.admin_encrypted_secret_set(
                    "runtime:voicebox",
                    main.EncryptedSecretSetRequest(display_name="Voicebox token", category="runtime", value="token"),
                )
            )
        self.assertEqual(missing.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
