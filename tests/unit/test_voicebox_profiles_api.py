from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
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


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8ffff3f0005fe02fea7f3c553"
    "0000000049454e44ae426082"
)
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


class FakeRawRequest:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def stream(self):
        yield self._body


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

    def patch_settings(self, **changes: Any) -> None:
        original = main.settings
        main.settings = replace(main.settings, **changes)
        self.addCleanup(lambda: setattr(main, "settings", original))

    def test_create_voice_profile_records_safe_artifact_references(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        audit_events = self.patch_common(fake_database)
        payload = main.VoiceProfileCreate(
            display_name="Narrator",
            metadata={"language": "en", "style": "neutral", "upstream_voice": "native-narrator"},
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

    def test_upload_voice_profile_sample_stores_voicebox_artifact(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        audit_events = self.patch_common(fake_database)
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, upload_max_bytes=1024, artifact_storage_reserve_bytes=0)
            result = asyncio.run(
                main.admin_voicebox_sample_artifact_upload(
                    FakeRawRequest(WAV_BYTES, {"content-type": "audio/wav"}),
                    x_b1_filename="../narrator.wav",
                )
            )

            artifact = result["artifact"]
            self.assertEqual(result["object"], "voicebox.sample_artifact")
            self.assertTrue(artifact["url"].startswith("/artifacts/voicebox/references/operator_1/sample_"))
            self.assertEqual(artifact["mime_type"], "audio/wav")
            self.assertEqual(artifact["bytes"], len(WAV_BYTES))
            self.assertEqual(artifact["sha256"], hashlib.sha256(WAV_BYTES).hexdigest())
            stored = Path(tmp) / artifact["url"].removeprefix("/artifacts/")
            self.assertEqual(stored.read_bytes(), WAV_BYTES)

        self.assertEqual(audit_events[0]["event_type"], "voice_profile.sample_uploaded")
        self.assertEqual(audit_events[0]["target_type"], "voice_profile_sample")

    def test_upload_voice_profile_sample_rejects_non_audio(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, upload_max_bytes=1024, artifact_storage_reserve_bytes=0)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(
                    main.admin_voicebox_sample_artifact_upload(
                        FakeRawRequest(PNG_BYTES, {"content-type": "image/png"}),
                        x_b1_filename="reference.png",
                    )
                )

        self.assertEqual(caught.exception.status_code, 415)

    def test_create_rejects_encoded_voice_sample_artifact_escape(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)

        for unsafe_url in [
            "/artifacts/voicebox/%2e%2e/private.wav",
            "/artifacts/voicebox/safe%2Fprivate.wav",
            "/artifacts/voicebox/%00sample.wav",
        ]:
            with self.subTest(unsafe_url=unsafe_url):
                payload = main.VoiceProfileCreate(
                    display_name="Unsafe",
                    sample_artifacts=[
                        main.VoiceProfileSampleArtifact(
                            url=unsafe_url,
                            sha256="a" * 64,
                            mime_type="audio/wav",
                            bytes=4096,
                        )
                    ],
                )
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(main.admin_voice_profile_create(payload))
                self.assertEqual(caught.exception.status_code, 422)

        self.assertEqual(fake_database.rows, {})

    def test_create_rejects_inline_voice_sample_metadata(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)
        payload = main.VoiceProfileCreate(display_name="Unsafe", metadata={"sample": "data:audio/wav;base64,AAAA"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_create(payload))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(fake_database.rows, {})

    def test_create_rejects_unsafe_upstream_voice_selector(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)
        payload = main.VoiceProfileCreate(display_name="Unsafe", metadata={"upstream_voice": "data:audio/wav;base64,AAAA"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_create(payload))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(fake_database.rows, {})

    def test_profile_policy_reports_safe_metadata_contract(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        self.patch_common(fake_database)

        result = asyncio.run(main.admin_voice_profile_policy())

        self.assertEqual(result["object"], "voicebox.profile_policy")
        self.assertEqual(result["metadata_max_bytes"], main.VOICE_PROFILE_MAX_METADATA_BYTES)
        self.assertEqual(result["metadata_string_max_length"], 256)
        self.assertEqual(result["sample_artifact_url_prefix"], "/artifacts/voicebox/references/")
        fields = {field["key"]: field for field in result["safe_metadata_fields"]}
        self.assertEqual(fields["upstream_voice_id"]["runtime_field"], "voice")
        self.assertEqual(fields["upstream_speaker_id"]["runtime_field"], "speaker_id")
        self.assertEqual(fields["speed"]["input_mode"], "decimal")
        self.assertIn("sample", result["forbidden_metadata_key_fragments"])

    def test_service_role_cannot_read_voice_profile_policy(self) -> None:
        fake_database = FakeVoiceProfileDatabase()
        auth = AuthContext(subject_id="service_1", role=Role.SERVICE, scopes=frozenset({"runtimes:read"}))
        self.patch_common(fake_database, auth=auth)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_voice_profile_policy())

        self.assertEqual(caught.exception.status_code, 403)

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
