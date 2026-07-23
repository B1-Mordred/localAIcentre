from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
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
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    HTTPException = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def voice_profile_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    row = {
        "id": "vp_1",
        "display_name": "Narrator",
        "owner_id": "operator_1",
        "runtime": "voicebox",
        "engine": "voicebox",
        "model_alias": "tts-quality",
        "profile_type": "clone",
        "status": "active",
        "visibility_roles": ["admin", "operator"],
        "metadata": {"language": "en"},
        "sample_artifacts": [],
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


class FakeAlias:
    def __init__(self, modality: str, runtimes: list[str]) -> None:
        self.alias = SimpleNamespace(modality=modality)
        self.runtimes = runtimes


class FakeCatalog:
    def require_alias(self, alias: str) -> FakeAlias:
        if alias == "tts-quality":
            return FakeAlias("tts", ["voicebox"])
        if alias == "tts-fast":
            return FakeAlias("tts", ["audio-cpu"])
        if alias == "vision-default":
            return FakeAlias("vlm", ["localai"])
        raise main.CatalogError(f"unknown model alias: {alias}")


class FakeVoiceProfileDatabase:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = {row["id"]: dict(row) for row in rows or []}
        self.list_kwargs: dict[str, Any] | None = None

    async def list_voice_profiles(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_kwargs = dict(kwargs)
        rows = list(self.rows.values())
        if not kwargs.get("include_deleted"):
            rows = [row for row in rows if row.get("deleted_at") is None]
        if kwargs.get("owner_id"):
            rows = [row for row in rows if row.get("owner_id") == kwargs["owner_id"]]
        if kwargs.get("runtime"):
            rows = [row for row in rows if row.get("runtime") == kwargs["runtime"]]
        if kwargs.get("status"):
            rows = [row for row in rows if row.get("status") == kwargs["status"]]
        return [dict(row) for row in rows]

    async def insert_voice_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = voice_profile_row(**payload)
        self.rows[row["id"]] = dict(row)
        return dict(row)

    async def get_voice_profile(self, profile_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
        row = self.rows.get(profile_id)
        if row is None:
            return None
        if row.get("deleted_at") is not None and not include_deleted:
            return None
        return dict(row)

    async def update_voice_profile(self, profile_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        row = await self.get_voice_profile(profile_id)
        if row is None:
            return None
        row.update(changes)
        row["updated_at"] = datetime(2026, 7, 22, 12, 30, tzinfo=UTC)
        self.rows[profile_id] = dict(row)
        return dict(row)

    async def delete_voice_profile(self, profile_id: str) -> dict[str, Any] | None:
        row = self.rows.get(profile_id)
        if row is None:
            return None
        row["status"] = "deleted"
        row["deleted_at"] = datetime(2026, 7, 22, 12, 45, tzinfo=UTC)
        self.rows[profile_id] = dict(row)
        return dict(row)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class VoiceboxProfilesApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, fake_database: FakeVoiceProfileDatabase, auth: Any | None = None) -> list[dict[str, Any]]:
        audit_events: list[dict[str, Any]] = []

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth or AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"runtimes:read", "runtimes:write"}))

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("database", fake_database)
        self.patch_attr("catalog_snapshot", lambda: FakeCatalog())
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        return audit_events

    def test_create_voice_profile_records_safe_artifact_references(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        audit_events = self.patch_common(fake_database)
        payload = main.VoiceProfileCreate(
            display_name="Narrator",
            metadata={"language": "en", "style": "neutral"},
            sample_artifacts=[
                main.VoiceProfileSampleArtifact(
                    url="/artifacts/voicebox/references/narrator.wav",
                    sha256="A" * 64,
                    mime_type="audio/wav",
                    bytes=4096,
                )
            ],
        )

        result = asyncio.run(main.admin_voice_profile_create(payload))

        self.assertTrue(result["id"].startswith("vp_"))
        self.assertEqual(result["model_alias"], "tts-quality")
        self.assertEqual(result["sample_artifacts"][0]["sha256"], "a" * 64)
        self.assertEqual(audit_events[0]["event_type"], "voice_profile.created")
        self.assertEqual(audit_events[0]["metadata"]["sample_artifact_count"], 1)
        self.assertNotIn("sample_artifacts", audit_events[0]["metadata"])

    def test_create_rejects_inline_voice_sample_metadata(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)
        payload = main.VoiceProfileCreate(display_name="Unsafe", metadata={"sample": "data:audio/wav;base64,AAAA"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_create(payload))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(fake_database.rows, {})

    def test_create_rejects_non_tts_alias_and_runtime_mismatch(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_create(main.VoiceProfileCreate(display_name="Vision", model_alias="vision-default")))
        self.assertEqual(caught.exception.status_code, 422)

        with self.assertRaises(HTTPException) as mismatch:
            asyncio.run(main.admin_voice_profile_create(main.VoiceProfileCreate(display_name="CPU fast", model_alias="tts-fast")))
        self.assertEqual(mismatch.exception.status_code, 422)

    def test_service_role_cannot_manage_voice_profiles_even_with_runtime_scope(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        auth = AuthContext(subject_id="service_1", role=Role.SERVICE, scopes=frozenset({"runtimes:read", "runtimes:write"}))
        self.patch_common(fake_database, auth=auth)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_create(main.VoiceProfileCreate(display_name="Service")))

        self.assertEqual(caught.exception.status_code, 403)

    def test_list_update_export_and_delete_voice_profile(self) -> None:
        fake_database = FakeVoiceProfileDatabase(
            [
                voice_profile_row(
                    sample_artifacts=[
                        {"url": "/artifacts/voicebox/references/narrator.wav", "sha256": "b" * 64, "mime_type": "audio/wav", "bytes": 123}
                    ]
                )
            ]
        )
        audit_events = self.patch_common(fake_database)

        listed = asyncio.run(main.admin_voice_profiles(include_deleted=False, runtime="voicebox", status="active", owner_id="operator_1"))
        self.assertEqual(listed["data"][0]["id"], "vp_1")
        self.assertEqual(
            fake_database.list_kwargs,
            {"include_deleted": False, "owner_id": "operator_1", "runtime": "voicebox", "status": "active"},
        )

        updated = asyncio.run(
            main.admin_voice_profile_update(
                "vp_1",
                main.VoiceProfileUpdate(status="disabled", visibility_roles=[Role.ADMIN], metadata={"language": "en", "consent_record_id": "consent-1"}),
            )
        )
        self.assertEqual(updated["status"], "disabled")
        self.assertEqual(updated["visibility_roles"], ["admin"])
        self.assertEqual(audit_events[0]["event_type"], "voice_profile.updated")
        self.assertEqual(audit_events[0]["metadata"]["changed_fields"], ["metadata", "status", "visibility_roles"])

        exported = asyncio.run(main.admin_voice_profile_export("vp_1"))
        self.assertEqual(exported["format"], "b1-ai-hub-voice-profile/v1")
        self.assertTrue(exported["contains_sensitive_data"])
        self.assertEqual(audit_events[1]["event_type"], "voice_profile.exported")

        deleted = asyncio.run(main.admin_voice_profile_delete("vp_1"))
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(audit_events[2]["event_type"], "voice_profile.deleted")


if __name__ == "__main__":
    unittest.main()
