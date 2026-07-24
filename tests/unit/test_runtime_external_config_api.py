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
    import app.adapters as adapters  # noqa: E402
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
class RuntimeExternalConfigApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(
        self,
        runtime_configs: dict[str, dict[str, Any]],
        encrypted_secrets: dict[str, dict[str, Any]],
        audit_events: list[dict[str, Any]],
        *,
        role: Any = None,
        scopes: frozenset[str] = frozenset({"*"}),
        allow_external: bool = True,
        master_key: str = "m" * 64,
    ) -> None:
        role = role or Role.ADMIN

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_list_runtime_configurations() -> list[dict[str, Any]]:
            return list(runtime_configs.values())

        async def fake_get_runtime_configuration(runtime: str) -> dict[str, Any] | None:
            return runtime_configs.get(runtime)

        async def fake_upsert_runtime_configuration(payload: dict[str, Any]) -> dict[str, Any]:
            now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
            existing = runtime_configs.get(payload["runtime"])
            row = {
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                **payload,
            }
            runtime_configs[payload["runtime"]] = row
            return row

        async def fake_get_encrypted_secret(name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
            row = encrypted_secrets.get(name)
            if row is None or (row.get("deleted_at") is not None and not include_deleted):
                return None
            return row

        fake_database = type(
            "FakeDatabase",
            (),
            {
                "list_runtime_configurations": staticmethod(fake_list_runtime_configurations),
                "get_runtime_configuration": staticmethod(fake_get_runtime_configuration),
                "upsert_runtime_configuration": staticmethod(fake_upsert_runtime_configuration),
                "get_encrypted_secret": staticmethod(fake_get_encrypted_secret),
            },
        )
        self.patch_attr("settings", replace(main.settings, allow_external_providers=allow_external, master_key=master_key))
        self.patch_attr("database", fake_database)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("runtime_registry", None)
        self.patch_attr("runtime_configuration_cache", None)
        original_resolver = adapters.resolve_hostname_addresses
        adapters.resolve_hostname_addresses = lambda hostname, port: ["93.184.216.34"]
        self.addCleanup(lambda: setattr(adapters, "resolve_hostname_addresses", original_resolver))

    def encrypted_secret(self, name: str, value: str, category: str = "remote-provider", master_key: str = "m" * 64) -> dict[str, Any]:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        return {
            "name": name,
            "display_name": name,
            "category": category,
            "description": "",
            "secret_envelope": secret_store.encrypt_value(master_key, name, value, created_at=now),
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }

    def test_external_runtime_config_persists_and_rebuilds_registry(self) -> None:
        runtime_configs: dict[str, dict[str, Any]] = {}
        encrypted_secrets = {"remote:openai": self.encrypted_secret("remote:openai", "provider-token")}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(runtime_configs, encrypted_secrets, audit_events)

        result = asyncio.run(
            main.admin_external_runtime_configuration_set(
                "openai-compatible",
                main.RuntimeExternalConfigRequest(
                    enabled=True,
                    base_url="https://API.EXAMPLE.COM/v1/",
                    api_key_secret_name="remote:openai",
                    confirm_external_data=True,
                    notes="operator approved",
                ),
                authorization="Bearer key",
            )
        )

        self.assertEqual(result["runtime"], "openai-compatible")
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["base_url"], "https://api.example.com/v1")
        self.assertTrue(result["api_key_configured"])
        self.assertNotIn("provider-token", str(result))
        adapter = main.runtime_registry_snapshot().adapter("openai-compatible")
        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.configured)
        self.assertEqual(adapter.openai_url("/v1/models"), "https://api.example.com/v1/models")
        self.assertEqual(adapter.request_headers(), {"Authorization": "Bearer provider-token"})
        self.assertEqual(runtime_configs["openai-compatible"]["base_url"], "https://api.example.com/v1")
        self.assertEqual(audit_events[0]["event_type"], "runtime_external_config.upserted")

    def test_external_runtime_enable_requires_ack_safe_url_and_remote_provider_secret(self) -> None:
        runtime_configs: dict[str, dict[str, Any]] = {}
        encrypted_secrets = {
            "runtime:token": self.encrypted_secret("runtime:token", "provider-token", category="runtime"),
        }
        audit_events: list[dict[str, Any]] = []
        self.patch_common(runtime_configs, encrypted_secrets, audit_events)

        with self.assertRaises(HTTPException) as missing_ack:
            asyncio.run(
                main.admin_external_runtime_configuration_set(
                    "openai-compatible",
                    main.RuntimeExternalConfigRequest(enabled=True, base_url="https://api.example.com/v1"),
                )
            )
        self.assertEqual(missing_ack.exception.status_code, 422)

        with self.assertRaises(HTTPException) as unsafe_url:
            asyncio.run(
                main.admin_external_runtime_configuration_set(
                    "openai-compatible",
                    main.RuntimeExternalConfigRequest(enabled=True, base_url="https://127.0.0.1/v1", confirm_external_data=True),
                )
            )
        self.assertEqual(unsafe_url.exception.status_code, 422)

        with self.assertRaises(HTTPException) as wrong_secret_category:
            asyncio.run(
                main.admin_external_runtime_configuration_set(
                    "openai-compatible",
                    main.RuntimeExternalConfigRequest(
                        enabled=True,
                        base_url="https://api.example.com/v1",
                        api_key_secret_name="runtime:token",
                        confirm_external_data=True,
                    ),
                )
            )
        self.assertEqual(wrong_secret_category.exception.status_code, 422)
        self.assertEqual(runtime_configs, {})

    def test_external_runtime_config_list_shows_environment_fallback_and_global_disable(self) -> None:
        runtime_configs: dict[str, dict[str, Any]] = {}
        encrypted_secrets: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(runtime_configs, encrypted_secrets, audit_events, allow_external=False)
        self.patch_attr("settings", replace(main.settings, openai_compatible_base_url="https://api.example.com/v1", allow_external_providers=False))

        result = asyncio.run(main.admin_external_runtime_configurations(authorization="Bearer key"))
        by_runtime = {item["runtime"]: item for item in result["data"]}

        self.assertFalse(result["allow_external_providers"])
        self.assertEqual(by_runtime["openai-compatible"]["source"], "environment")
        self.assertEqual(by_runtime["openai-compatible"]["status"], "configured-global-disabled")
        self.assertFalse(by_runtime["openai-compatible"]["eligible"])
        self.assertEqual(by_runtime["generic-http"]["status"], "disabled")

    def test_external_runtime_config_rejects_non_admin(self) -> None:
        runtime_configs: dict[str, dict[str, Any]] = {}
        encrypted_secrets: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(runtime_configs, encrypted_secrets, audit_events, role=Role.OPERATOR, scopes=frozenset({"runtimes:read", "runtimes:write"}))

        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_external_runtime_configurations(authorization="Bearer key"))
        self.assertEqual(exc.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
