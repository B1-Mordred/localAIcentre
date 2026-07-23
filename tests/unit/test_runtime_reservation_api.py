from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def reservation_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    row = {
        "id": "reservation_1",
        "owner_id": "client_1",
        "runtime": "localai",
        "model_alias": "chat-default",
        "resolved_model_version": "chat-model@1.0.0",
        "duration_seconds": 300,
        "reason": "batch window",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "expires_at": datetime.now(tz=UTC) + timedelta(minutes=5),
        "cancelled_at": None,
    }
    row.update(overrides)
    return row


class FakeReservationDatabase:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row or reservation_row()
        self.list_kwargs: dict[str, Any] | None = None
        self.cancelled: list[str] = []
        self.inserted: list[dict[str, Any]] = []

    async def list_runtime_reservations(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_kwargs = dict(kwargs)
        return [dict(self.row)]

    async def get_runtime_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        if self.row and self.row["id"] == reservation_id:
            return dict(self.row)
        return None

    async def cancel_runtime_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        row = await self.get_runtime_reservation(reservation_id)
        if row is None:
            return None
        self.cancelled.append(reservation_id)
        self.row = {**row, "status": "cancelled", "cancelled_at": datetime.now(tz=UTC)}
        return dict(self.row)

    async def insert_runtime_reservation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.inserted.append(dict(payload))
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        self.row = {
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "cancelled_at": None,
            **payload,
        }
        return dict(self.row)


def fake_catalog_alias(*, runtimes: list[str] | None = None, model_id: str = "chat-model", version: str = "1.0.0") -> Any:
    return SimpleNamespace(
        runtimes=runtimes or ["localai"],
        manifest=SimpleNamespace(id=model_id, version=version),
    )


def fake_runtime_registry(**adapters: Any) -> Any:
    return SimpleNamespace(adapters=adapters)


def fake_adapter(*, configured: bool = True, requires_gpu: bool = True, external: bool = False) -> Any:
    return SimpleNamespace(configured=configured, requires_gpu=requires_gpu, external=external)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class RuntimeReservationApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, auth: Any) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", fake_authenticate)

    def patch_audit(self, audit_events: list[dict[str, Any]]) -> None:
        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("record_audit_event", fake_record_audit_event)

    def test_admin_reservation_listing_passes_filters(self) -> None:
        fake_database = FakeReservationDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"runtimes:read"})))

        result = asyncio.run(main.admin_runtime_reservations(limit=25, status="active", runtime="localai", owner_id="client_1"))

        self.assertEqual(result["object"], "list")
        self.assertEqual(result["data"][0]["id"], "reservation_1")
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 25, "owner_id": "client_1", "status": "active", "runtime": "localai"},
        )

    def test_admin_reservation_listing_requires_operator_or_admin(self) -> None:
        fake_database = FakeReservationDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_runtime_reservations(limit=25))

        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(fake_database.list_kwargs)

    def test_create_reservation_records_installed_gpu_alias_and_audit(self) -> None:
        fake_database = FakeReservationDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:write"})))
        self.patch_audit(audit_events)
        self.patch_attr("require_catalog_alias", lambda *args, **kwargs: fake_catalog_alias())
        self.patch_attr("runtime_registry_snapshot", lambda: fake_runtime_registry(localai=fake_adapter()))

        result = asyncio.run(
            main.runtime_reservation_create(
                main.RuntimeReservationCreate(runtime="localai", model="chat-default", duration_seconds=600, reason="batch window")
            )
        )

        self.assertEqual(result["runtime"], "localai")
        self.assertEqual(result["model_alias"], "chat-default")
        self.assertEqual(result["resolved_model_version"], "chat-model@1.0.0")
        self.assertEqual(fake_database.inserted[0]["owner_id"], "client_1")
        self.assertEqual(fake_database.inserted[0]["duration_seconds"], 600)
        self.assertEqual(audit_events[0]["event_type"], "runtime_reservation.created")

    def test_create_reservation_rejects_cpu_runtime_before_insert(self) -> None:
        fake_database = FakeReservationDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:write"})))
        self.patch_attr("require_catalog_alias", lambda *args, **kwargs: fake_catalog_alias(runtimes=["audio-cpu"]))
        self.patch_attr("runtime_registry_snapshot", lambda: fake_runtime_registry(**{"audio-cpu": fake_adapter(requires_gpu=False)}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.runtime_reservation_create(
                    main.RuntimeReservationCreate(runtime="audio-cpu", model="tts-fast", duration_seconds=300, reason="cpu job")
                )
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("does not participate in GPU scheduler reservations", str(caught.exception.detail))
        self.assertEqual(fake_database.inserted, [])

    def test_create_reservation_rejects_unconfigured_runtime_before_insert(self) -> None:
        fake_database = FakeReservationDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:write"})))
        self.patch_attr("require_catalog_alias", lambda *args, **kwargs: fake_catalog_alias())
        self.patch_attr("runtime_registry_snapshot", lambda: fake_runtime_registry(localai=fake_adapter(configured=False)))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.runtime_reservation_create(
                    main.RuntimeReservationCreate(runtime="localai", model="chat-default", duration_seconds=300, reason="batch")
                )
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("not configured", str(caught.exception.detail))
        self.assertEqual(fake_database.inserted, [])

    def test_public_get_rejects_other_owner(self) -> None:
        self.patch_attr("database", FakeReservationDatabase(reservation_row(owner_id="other_client")))
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.runtime_reservation_get("reservation_1"))

        self.assertEqual(caught.exception.status_code, 403)

    def test_public_delete_rejects_other_owner_before_cancel(self) -> None:
        fake_database = FakeReservationDatabase(reservation_row(owner_id="other_client"))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:write"})))
        self.patch_audit([])

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.runtime_reservation_delete("reservation_1"))

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(fake_database.cancelled, [])

    def test_owner_can_cancel_reservation_and_audit(self) -> None:
        fake_database = FakeReservationDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"runtimes:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.runtime_reservation_delete("reservation_1"))

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(fake_database.cancelled, ["reservation_1"])
        self.assertEqual(audit_events[0]["event_type"], "runtime_reservation.cancelled")


if __name__ == "__main__":
    unittest.main()
