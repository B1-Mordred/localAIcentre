from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main, security  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def manifest_payload(sha256: str, size: int) -> dict[str, Any]:
    return {
        "id": "chat-small",
        "version": "1.0.0",
        "display_name": "Chat Small",
        "modality": "llm",
        "operations": ["chat"],
        "source": {"type": "catalog", "url": "https://models.ai.b1.germering/test", "revision": "1.0.0"},
        "files": [{"path": "chat-small.gguf", "sha256": sha256, "size_bytes": size}],
        "runtimes": ["localai"],
        "preferred_runtime": "localai",
        "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
        "license": {"name": "test", "redistribution": "downloadable"},
        "execution_modes": ["hosted-inference", "downloadable"],
        "aliases": ["chat-default"],
    }


def measured_manifest_payload(alias: str, runtime: str, *, model_id: str | None = None, version: str = "1.0.0") -> dict[str, Any]:
    data = f"{alias}:{runtime}:{version}".encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        **manifest_payload(digest, len(data)),
        "id": model_id or alias.replace("-", "_"),
        "version": version,
        "display_name": f"{alias} model",
        "aliases": [alias],
        "preferred_runtime": runtime,
        "runtimes": [runtime],
    }
    payload["measurements"] = {
        "schema": "b1-ai-hub-model-measurements/v1",
        "source": "control-plane",
        "updated_at": "2026-07-24T12:00:00+00:00",
        "original_resource_estimate": payload["resource_estimate"],
        "latest_resource_estimate": payload["resource_estimate"],
        "runs": [
            {
                "id": f"modelsmoke_{alias}",
                "type": "install-smoke",
                "status": "ok",
                "runtime": runtime,
                "model_alias": alias,
                "resolved_model_version": f"{payload['id']}@{version}",
                "started_at": "2026-07-24T12:00:00+00:00",
                "completed_at": "2026-07-24T12:00:03+00:00",
                "duration_ms": 3000,
                "run_time_ms": 500,
                "peak_vram_mib": 4096,
                "peak_ram_mib": 2048,
                "unsafe_extra": "not public",
            }
        ],
    }
    return payload


def model_record_from_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload["id"],
        "version": payload["version"],
        "display_name": payload["display_name"],
        "modality": payload["modality"],
        "preferred_runtime": payload["preferred_runtime"],
        "status": "installed",
        "resource_label": "expected",
        "manifest": payload,
    }


class FakeDatabase:
    def __init__(
        self,
        row: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        active_jobs: int = 0,
        voice_profiles: list[dict[str, Any]] | None = None,
        runtime_reservations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.row = row
        self.records = records
        self.active_jobs = active_jobs
        self.voice_profiles = voice_profiles or []
        self.runtime_reservations = runtime_reservations or []
        self.alias_policies: dict[str, dict[str, Any]] = {}
        self.encrypted_secrets: dict[str, dict[str, Any]] = {}
        self.model_downloads: list[dict[str, Any]] = []
        self.leases: list[dict[str, Any]] = []
        self.releases: list[str] = []
        self.runtime_states: list[dict[str, Any]] = []

    async def get_model_record(self, model_id: str, version: str) -> dict[str, Any] | None:
        if self.row["id"] == model_id and self.row["version"] == version:
            return dict(self.row)
        return None

    async def list_model_records(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(record) for record in self.records]

    async def upsert_model_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        row = {
            "created_at": self.row.get("created_at", now) if isinstance(self.row, dict) else now,
            "updated_at": now,
            **payload,
        }
        self.row = row
        replaced = False
        for index, record in enumerate(self.records):
            if record.get("id") == row["id"] and record.get("version") == row["version"]:
                self.records[index] = row
                replaced = True
                break
        if not replaced:
            self.records.append(row)
        return dict(row)

    async def count_active_jobs_for_model(self, model_ref: str, aliases: list[str]) -> int:
        return self.active_jobs

    async def list_voice_profiles(self, include_deleted: bool = False, **kwargs: Any) -> list[dict[str, Any]]:
        rows = []
        for row in self.voice_profiles:
            if row.get("deleted_at") is not None and not include_deleted:
                continue
            rows.append(dict(row))
        return rows

    async def list_active_runtime_reservations(self, runtime_names: list[str] | None = None) -> list[dict[str, Any]]:
        rows = []
        for row in self.runtime_reservations:
            if runtime_names and row.get("runtime") not in runtime_names:
                continue
            rows.append(dict(row))
        return rows

    async def list_model_alias_policies(self) -> list[dict[str, Any]]:
        return [dict(policy) for _, policy in sorted(self.alias_policies.items())]

    async def upsert_model_alias_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        existing = self.alias_policies.get(payload["alias"])
        row = {
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            **payload,
        }
        self.alias_policies[payload["alias"]] = row
        return dict(row)

    async def delete_model_alias_policy(self, alias: str) -> dict[str, Any] | None:
        return self.alias_policies.pop(alias, None)

    async def get_encrypted_secret(self, name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        row = self.encrypted_secrets.get(name)
        if row is None or (row.get("deleted_at") and not include_deleted):
            return None
        return dict(row)

    async def insert_model_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        row = {
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "cancelled_at": None,
            "error_category": None,
            "error_message": None,
            **payload,
        }
        self.model_downloads.append(row)
        return dict(row)

    async def get_model_download(self, download_id: str) -> dict[str, Any] | None:
        for row in self.model_downloads:
            if row["id"] == download_id:
                return dict(row)
        return None

    async def request_model_download_pause(self, download_id: str) -> dict[str, Any] | None:
        for row in self.model_downloads:
            if row["id"] != download_id:
                continue
            if row["status"] in {"completed", "failed", "cancelled", "paused"}:
                return dict(row)
            if row["status"] == "queued":
                row.update({"status": "paused", "stage": "paused", "updated_at": datetime.now(tz=UTC)})
                return dict(row)
            if row["status"] in {"running", "pausing"}:
                row.update({"status": "pausing", "stage": "pausing", "updated_at": datetime.now(tz=UTC)})
                return dict(row)
            raise ValueError(f"model download cannot be paused from status {row['status']}")
        return None

    async def resume_model_download(self, download_id: str) -> dict[str, Any] | None:
        for row in self.model_downloads:
            if row["id"] != download_id:
                continue
            if row["status"] != "paused":
                raise ValueError(f"model download cannot be resumed from status {row['status']}")
            row.update({"status": "queued", "stage": "queued", "updated_at": datetime.now(tz=UTC)})
            return dict(row)
        return None

    async def retry_model_download(self, download_id: str) -> dict[str, Any] | None:
        for row in self.model_downloads:
            if row["id"] != download_id:
                continue
            if row["status"] not in {"failed", "cancelled"}:
                raise ValueError(f"model download cannot be retried from status {row['status']}")
            row.update(
                {
                    "status": "queued",
                    "stage": "queued",
                    "error_category": None,
                    "error_message": None,
                    "cancelled_at": None,
                    "completed_at": None,
                    "updated_at": datetime.now(tz=UTC),
                }
            )
            return dict(row)
        return None

    async def runtime_reservation_gate(self, owner_id: str, runtime: str, resolved_model_version: str, gpu_runtimes: list[str]) -> dict[str, Any]:
        return {"allowed": True}

    async def acquire_scheduler_owner(self, owner: str, ttl_seconds: int) -> dict[str, Any]:
        row = {"owner": owner, "ttl_seconds": ttl_seconds, "acquired": True}
        self.leases.append(row)
        return row

    async def release_scheduler_owner(self, owner: str) -> dict[str, Any]:
        self.releases.append(owner)
        return {"owner": owner, "released": True}

    async def upsert_runtime_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"updated_at": datetime.now(tz=UTC), **payload}
        self.runtime_states.append(row)
        return dict(row)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ModelAdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        original_resolver = security.resolve_hostname_addresses
        security.resolve_hostname_addresses = lambda hostname, port: ["93.184.216.34"]
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original_resolver))

    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, root: Path, fake_database: FakeDatabase, audit_events: list[dict[str, Any]] | None = None) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="test-admin", role=Role.ADMIN, scopes=frozenset({"*"}))

        async def fake_dependent_workflows(model_id: str, aliases: list[str]) -> list[dict[str, str]]:
            return [{"id": "workflow-text", "version": "1.0.0", "dependency": aliases[0]}]

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            if audit_events is not None:
                audit_events.append({"event_type": event_type, **kwargs})

        async def fake_refresh_workflow_dependency_statuses() -> dict[str, Any]:
            return {"refreshed": [], "count": 0}

        self.patch_attr("database", fake_database)
        self.patch_attr("model_catalog", None)
        self.patch_attr("settings", replace(main.settings, model_catalog_dir=str(ROOT / "model-catalog")))
        self.patch_attr("data_root_path", lambda: root)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("dependent_workflows_for_model", fake_dependent_workflows)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("refresh_workflow_dependency_statuses", fake_refresh_workflow_dependency_statuses)

    def patch_auth_context(self, auth: Any) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", fake_authenticate)

    def test_admin_model_routes_require_model_admin_guard_in_source(self) -> None:
        source = (ROOT / "services" / "control-plane" / "app" / "main.py").read_text(encoding="utf-8")
        route_handlers = [
            "admin_model_quarantine_retention_plan",
            "admin_model_quarantine_cleanup",
            "admin_models",
            "admin_model_alias_policy_update",
            "admin_model_alias_policy_delete",
            "admin_model_install_plan",
            "admin_model_download_plan",
            "admin_model_downloads",
            "admin_model_download_get",
            "admin_model_download_create",
            "admin_model_download_cancel",
            "admin_model_download_pause",
            "admin_model_download_resume",
            "admin_model_download_retry",
            "admin_model_download_install",
            "admin_model_install",
            "admin_model_smoke_test",
            "admin_model_removal_plan",
            "admin_model_remove",
            "admin_model_blob_quarantine_plan",
            "admin_model_blob_quarantine",
        ]
        for handler in route_handlers:
            with self.subTest(handler=handler):
                start = source.index(f"async def {handler}")
                end = source.find("\n@app.", start + 1)
                body = source[start:] if end == -1 else source[start:end]
                self.assertIn("require_model_admin(auth)", body)

    def test_admin_models_rejects_service_client_with_models_read_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase({}, [], active_jobs=0))
            self.patch_auth_context(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"models:read"})))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_models())

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "model administration requires admin or operator role")

    def test_admin_models_allows_operator_with_models_read_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase({}, [], active_jobs=0))
            self.patch_auth_context(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"models:read"})))

            result = asyncio.run(main.admin_models())

        self.assertEqual(result["object"], "list")
        self.assertIn("aliases", result)
        self.assertIn("profiles", result)
        self.assertIn("catalog", result)
        self.assertTrue(any(profile["id"] == "everyday-llm-7-9b-q4" for profile in result["profiles"]))
        self.assertEqual(result["records"], [])

    def test_model_removal_plan_reports_required_profile_dependencies(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "status": "installed",
            "manifest": manifest_payload(digest, len(data)),
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase(row, [row], active_jobs=0))

            result = asyncio.run(main.admin_model_removal_plan("chat-small", "1.0.0"))

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["can_quarantine"])
        self.assertEqual(result["active_jobs"], 0)
        self.assertEqual(result["dependent_model_profiles"][0]["id"], "everyday-llm-7-9b-q4")
        self.assertEqual(result["dependent_model_profiles"][0]["matched_aliases"], ["chat-default"])
        self.assertIn("runtime views will be moved to recoverable quarantine", result["quarantine"])

    def test_blob_quarantine_plan_blocks_active_jobs(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            self.patch_common(root, FakeDatabase(row, [row], active_jobs=2))

            result = asyncio.run(main.admin_model_blob_quarantine_plan("chat-small", "1.0.0"))

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["can_quarantine"])
            self.assertEqual(result["active_jobs"], 2)
            self.assertIn("model is referenced by active jobs", result["blockers"])
            self.assertEqual(result["dependent_workflows"][0]["id"], "workflow-text")
            self.assertEqual(result["dependent_model_profiles"][0]["id"], "everyday-llm-7-9b-q4")

    def test_blob_quarantine_plan_blocks_active_voice_profile_dependency(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            voice_profiles = [
                {
                    "id": "vp_chat",
                    "display_name": "Chat voice",
                    "runtime": "voicebox",
                    "engine": "voicebox",
                    "model_alias": "chat-default",
                    "profile_type": "clone",
                    "status": "active",
                    "deleted_at": None,
                }
            ]
            self.patch_common(root, FakeDatabase(row, [row], voice_profiles=voice_profiles))

            result = asyncio.run(main.admin_model_blob_quarantine_plan("chat-small", "1.0.0"))

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["can_quarantine"])
            self.assertIn("model is referenced by active voice profiles", result["blockers"])
            self.assertEqual(result["dependent_voice_profiles"][0]["id"], "vp_chat")
            self.assertEqual(result["active_voice_profiles"][0]["model_alias"], "chat-default")

    def test_blob_quarantine_plan_blocks_active_runtime_reservation_dependency(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            reservation = {
                "id": "reservation_1",
                "owner_id": "batch-client",
                "runtime": "localai",
                "model_alias": "chat-default",
                "resolved_model_version": "chat-small@1.0.0",
                "reason": "sensitive reason text",
                "idempotency_key": "secret-idempotency-key",
                "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
            }
            self.patch_common(root, FakeDatabase(row, [row], runtime_reservations=[reservation]))

            result = asyncio.run(main.admin_model_blob_quarantine_plan("chat-small", "1.0.0"))

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["can_quarantine"])
            self.assertIn("model is referenced by active runtime reservations", result["blockers"])
            self.assertEqual(result["active_runtime_reservations"][0]["id"], "reservation_1")
            self.assertEqual(result["active_runtime_reservations"][0]["model_alias"], "chat-default")
            self.assertNotIn("reason", result["active_runtime_reservations"][0])
            self.assertNotIn("idempotency_key", result["active_runtime_reservations"][0])

    def test_blob_quarantine_plan_ignores_same_alias_reservation_for_replacement_model(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            reservation = {
                "id": "reservation_replacement",
                "owner_id": "batch-client",
                "runtime": "localai",
                "model_alias": "chat-default",
                "resolved_model_version": "chat-large@2.0.0",
                "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
            }
            self.patch_common(root, FakeDatabase(row, [row], runtime_reservations=[reservation]))

            result = asyncio.run(main.admin_model_blob_quarantine_plan("chat-small", "1.0.0"))

            self.assertTrue(result["can_quarantine"])
            self.assertEqual(result["active_runtime_reservations"], [])

    def test_model_remove_blocks_active_voice_profile_dependency(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "status": "installed",
            "manifest": manifest_payload(digest, len(data)),
        }
        voice_profiles = [
            {
                "id": "vp_active",
                "display_name": "Active voice",
                "runtime": "voicebox",
                "engine": "voicebox",
                "model_alias": "chat-default",
                "profile_type": "reference",
                "status": "active",
                "deleted_at": None,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase(row, [row], voice_profiles=voice_profiles))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_remove(
                        "chat-small",
                        "1.0.0",
                        main.ModelRemoveRequest(confirm=True),
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "model is referenced by active voice profiles")
        self.assertEqual(raised.exception.detail["dependent_voice_profiles"][0]["id"], "vp_active")
        self.assertEqual(raised.exception.detail["dependent_model_profiles"][0]["id"], "everyday-llm-7-9b-q4")

    def test_model_remove_blocks_active_runtime_reservation_dependency(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "status": "installed",
            "manifest": manifest_payload(digest, len(data)),
        }
        reservation = {
            "id": "reservation_model_ref",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-quality",
            "resolved_model_version": "chat-small@1.0.0",
            "reason": "operator note",
            "idempotency_key": "reservation-key",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase(row, [row], runtime_reservations=[reservation]))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_remove(
                        "chat-small",
                        "1.0.0",
                        main.ModelRemoveRequest(confirm=True),
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "model is referenced by active runtime reservations")
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["id"], "reservation_model_ref")
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["resolved_model_version"], "chat-small@1.0.0")
        self.assertEqual(raised.exception.detail["dependent_model_profiles"][0]["matched_aliases"], ["chat-default"])
        self.assertNotIn("reason", raised.exception.detail["active_runtime_reservations"][0])
        self.assertNotIn("idempotency_key", raised.exception.detail["active_runtime_reservations"][0])

    def test_model_remove_reports_disabled_voice_profile_dependency_before_confirmation(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "status": "installed",
            "manifest": manifest_payload(digest, len(data)),
        }
        voice_profiles = [
            {
                "id": "vp_disabled",
                "display_name": "Disabled voice",
                "runtime": "voicebox",
                "engine": "voicebox",
                "model_alias": "chat-default",
                "profile_type": "preset",
                "status": "disabled",
                "deleted_at": None,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase(row, [row], voice_profiles=voice_profiles))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_model_remove("chat-small", "1.0.0", main.ModelRemoveRequest(confirm=False)))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "model removal requires explicit confirmation")
        self.assertEqual(raised.exception.detail["dependent_model_profiles"][0]["id"], "everyday-llm-7-9b-q4")
        self.assertEqual(raised.exception.detail["dependent_voice_profiles"][0]["id"], "vp_disabled")
        self.assertEqual(raised.exception.detail["active_voice_profiles"], [])

    def test_blob_quarantine_endpoint_moves_blob_and_records_audit(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "models" / "blobs" / digest
            source.parent.mkdir(parents=True)
            source.write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            self.patch_common(root, FakeDatabase(row, [row]), audit_events)

            result = asyncio.run(
                main.admin_model_blob_quarantine(
                    "chat-small",
                    "1.0.0",
                    main.ModelBlobQuarantineRequest(confirm=True),
                )
            )

            self.assertEqual(result["status"], "quarantined")
            self.assertFalse(source.exists())
            quarantine_path = Path(result["moved"][0]["quarantine_path"])
            self.assertEqual(quarantine_path.read_bytes(), data)
            self.assertEqual(result["dependent_workflows"][0]["dependency"], "chat-default")
            self.assertEqual(result["dependent_model_profiles"][0]["id"], "everyday-llm-7-9b-q4")
            self.assertEqual(audit_events[0]["event_type"], "model.blobs_quarantined")
            self.assertEqual(audit_events[0]["target_id"], "chat-small@1.0.0")
            self.assertEqual(audit_events[0]["metadata"]["moved"][0]["sha256"], digest)
            self.assertEqual(audit_events[0]["metadata"]["dependent_model_profiles"][0]["matched_aliases"], ["chat-default"])

    def test_blob_quarantine_endpoint_blocks_active_runtime_reservation_dependency(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "models" / "blobs" / digest
            source.parent.mkdir(parents=True)
            source.write_bytes(data)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "status": "quarantined",
                "manifest": manifest_payload(digest, len(data)),
            }
            reservation = {
                "id": "reservation_2",
                "owner_id": "batch-client",
                "runtime": "localai",
                "model_alias": "chat-default",
                "resolved_model_version": "chat-small@1.0.0",
                "reason": "must stay private",
                "idempotency_key": "must-stay-private",
                "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
            }
            self.patch_common(root, FakeDatabase(row, [row], runtime_reservations=[reservation]))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_blob_quarantine(
                        "chat-small",
                        "1.0.0",
                        main.ModelBlobQuarantineRequest(confirm=True),
                    )
                )

            self.assertTrue(source.exists())

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "model is referenced by active runtime reservations")
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["id"], "reservation_2")
        self.assertNotIn("must stay private", str(raised.exception.detail))
        self.assertNotIn("must-stay-private", str(raised.exception.detail))

    def test_model_quarantine_cleanup_plans_deletes_and_records_audit(self) -> None:
        data = b"quarantined model blob"
        digest = hashlib.sha256(data).hexdigest()
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_set = root / "models" / "quarantine" / "blobs" / "chat-small" / "1.0.0-20260701T120000Z"
            old_set.mkdir(parents=True)
            (old_set / digest).write_bytes(data)
            self.patch_common(root, FakeDatabase({}, []), audit_events)

            plan = asyncio.run(
                main.admin_model_quarantine_retention_plan(
                    main.ModelQuarantineRetentionRequest(delete_older_than_days=7, limit=100),
                )
            )
            cleaned = asyncio.run(
                main.admin_model_quarantine_cleanup(
                    main.ModelQuarantineRetentionRequest(delete_older_than_days=7, limit=100, confirm=True),
                )
            )

            self.assertEqual(plan["candidate_count"], 1)
            self.assertEqual(cleaned["status"], "cleaned")
            self.assertEqual(cleaned["deleted_count"], 1)
            self.assertFalse(old_set.exists())
            self.assertEqual(audit_events[0]["event_type"], "model_quarantine.cleanup")
            self.assertEqual(audit_events[0]["metadata"]["deleted_count"], 1)
            self.assertEqual(audit_events[0]["metadata"]["total_reclaimable_bytes"], len(data))

    def test_alias_policy_update_persists_refreshes_and_records_audit(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(
                main.admin_model_alias_policy_update(
                    "chat-default",
                    main.ModelAliasPolicyRequest(
                        enabled=False,
                        preferred_runtime="localai",
                        idle_timeout_seconds=600,
                        visibility_roles=[Role.ADMIN, Role.OPERATOR],
                        notes="planned maintenance",
                    ),
                )
            )

            self.assertFalse(result["policy"]["enabled"])
            self.assertEqual(result["policy"]["preferred_runtime"], "localai")
            self.assertEqual(result["policy"]["idle_timeout_seconds"], 600)
            self.assertEqual(result["policy"]["visibility_roles"], ["admin", "operator"])
            self.assertEqual(result["alias"]["status"], "disabled")
            self.assertEqual(result["alias"]["alias_policy_source"], "database")
            self.assertEqual(fake_database.alias_policies["chat-default"]["updated_by"], "test-admin")
            self.assertEqual(audit_events[0]["event_type"], "model_alias_policy.updated")

    def test_alias_policy_disable_blocks_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "reason": "operator note",
            "idempotency_key": "reservation-key",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            self.patch_common(Path(tmp), fake_database)

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_alias_policy_update(
                        "chat-default",
                        main.ModelAliasPolicyRequest(enabled=False),
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "alias has active runtime reservations and cannot be disabled")
        self.assertEqual(raised.exception.detail["active_jobs"], 0)
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["id"], "reservation_alias")
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["model_alias"], "chat-default")
        self.assertEqual(raised.exception.detail["active_runtime_reservations"][0]["resolved_model_version"], "chat-small@1.0.0")
        self.assertNotIn("operator note", str(raised.exception.detail))
        self.assertNotIn("reservation-key", str(raised.exception.detail))
        self.assertEqual(fake_database.alias_policies, {})

    def test_alias_policy_enable_allows_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            self.patch_common(Path(tmp), fake_database)

            result = asyncio.run(
                main.admin_model_alias_policy_update(
                    "chat-default",
                    main.ModelAliasPolicyRequest(enabled=True),
                )
            )

        self.assertTrue(result["policy"]["enabled"])
        self.assertTrue(fake_database.alias_policies["chat-default"]["enabled"])

    def test_alias_policy_preferred_runtime_change_blocks_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "reason": "operator note",
            "idempotency_key": "reservation-key",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            self.patch_common(Path(tmp), fake_database)

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_alias_policy_update(
                        "chat-default",
                        main.ModelAliasPolicyRequest(enabled=True, preferred_runtime="comfyui"),
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["message"], "alias has active runtime reservations and resolution policy cannot be changed")
        self.assertEqual(raised.exception.detail["resolution_changes"]["preferred_runtime"]["current"], "localai")
        self.assertEqual(raised.exception.detail["resolution_changes"]["preferred_runtime"]["proposed"], "comfyui")
        self.assertNotIn("operator note", str(raised.exception.detail))
        self.assertNotIn("reservation-key", str(raised.exception.detail))
        self.assertEqual(fake_database.alias_policies, {})

    def test_alias_policy_visibility_change_blocks_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            self.patch_common(Path(tmp), fake_database)

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_alias_policy_update(
                        "chat-default",
                        main.ModelAliasPolicyRequest(enabled=True, visibility_roles=[Role.ADMIN]),
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["resolution_changes"]["visibility_roles"]["current"], [])
        self.assertEqual(raised.exception.detail["resolution_changes"]["visibility_roles"]["proposed"], ["admin"])
        self.assertEqual(fake_database.alias_policies, {})

    def test_alias_policy_idle_timeout_and_notes_update_allow_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "localai",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            self.patch_common(Path(tmp), fake_database)

            result = asyncio.run(
                main.admin_model_alias_policy_update(
                    "chat-default",
                    main.ModelAliasPolicyRequest(
                        enabled=True,
                        idle_timeout_seconds=900,
                        notes="keep warm during maintenance",
                    ),
                )
            )

        self.assertTrue(result["policy"]["enabled"])
        self.assertEqual(result["policy"]["idle_timeout_seconds"], 900)
        self.assertEqual(result["policy"]["notes"], "keep warm during maintenance")

    def test_alias_policy_delete_blocks_resolution_change_with_active_runtime_reservation_dependency(self) -> None:
        reservation = {
            "id": "reservation_alias",
            "owner_id": "batch-client",
            "runtime": "comfyui",
            "model_alias": "chat-default",
            "resolved_model_version": "chat-small@1.0.0",
            "reason": "operator note",
            "idempotency_key": "reservation-key",
            "expires_at": datetime.now(tz=UTC) + timedelta(minutes=10),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase({}, [], active_jobs=0, runtime_reservations=[reservation])
            now = datetime.now(tz=UTC)
            fake_database.alias_policies["chat-default"] = {
                "alias": "chat-default",
                "enabled": True,
                "preferred_runtime": "comfyui",
                "idle_timeout_seconds": 600,
                "visibility_roles": [],
                "notes": "reset me later",
                "updated_by": "test-admin",
                "created_at": now,
                "updated_at": now,
            }
            self.patch_common(Path(tmp), fake_database)
            asyncio.run(main.refresh_catalog_cache())

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_model_alias_policy_delete("chat-default"))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["resolution_changes"]["preferred_runtime"]["current"], "comfyui")
        self.assertEqual(raised.exception.detail["resolution_changes"]["preferred_runtime"]["proposed"], "localai")
        self.assertNotIn("operator note", str(raised.exception.detail))
        self.assertNotIn("reservation-key", str(raised.exception.detail))
        self.assertIn("chat-default", fake_database.alias_policies)

    def test_alias_policy_delete_resets_seed_policy(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.alias_policies["chat-default"] = {
                "alias": "chat-default",
                "enabled": False,
                "preferred_runtime": "localai",
                "idle_timeout_seconds": 600,
                "visibility_roles": ["admin"],
                "notes": "reset me",
                "updated_by": "test-admin",
                "created_at": now,
                "updated_at": now,
            }
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(main.admin_model_alias_policy_delete("chat-default"))

            self.assertTrue(result["deleted"])
            self.assertEqual(result["previous_policy"]["alias"], "chat-default")
            self.assertEqual(result["alias"]["alias_policy_source"], "seed")
            self.assertEqual(result["alias"]["status"], "uninstalled")
            self.assertNotIn("chat-default", fake_database.alias_policies)
            self.assertEqual(audit_events[0]["event_type"], "model_alias_policy.reset")

    def test_model_download_create_accepts_model_download_secret_name(self) -> None:
        digest = hashlib.sha256(b"private model").hexdigest()
        payload = manifest_payload(digest, len(b"private model"))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.encrypted_secrets["model-download:hf"] = {
                "name": "model-download:hf",
                "display_name": "Hugging Face token",
                "category": "model-download",
                "description": "read token",
                "secret_envelope": {"scheme": "b1-aesgcm-sha256/v1", "key_id": "abc"},
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(
                main.admin_model_download_create(
                    main.ModelDownloadCreate(
                        manifest=payload,
                        confirm=True,
                        credential_secret_name="model-download:hf",
                    )
                )
            )

            self.assertEqual(result["download"]["credential_secret_name"], "model-download:hf")
            self.assertTrue(result["download"]["authenticated"])
            self.assertFalse(result["download"]["license_accepted"])
            self.assertEqual(fake_database.model_downloads[0]["credential_secret_name"], "model-download:hf")
            self.assertFalse(fake_database.model_downloads[0]["license_accepted"])
            self.assertTrue(audit_events[0]["metadata"]["authenticated"])
            self.assertNotIn("credential_secret_name", audit_events[0]["metadata"])

    def test_model_download_create_rejects_wrong_secret_category(self) -> None:
        digest = hashlib.sha256(b"private model").hexdigest()
        payload = manifest_payload(digest, len(b"private model"))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.encrypted_secrets["runtime:token"] = {
                "name": "runtime:token",
                "display_name": "Runtime token",
                "category": "runtime",
                "description": "wrong category",
                "secret_envelope": {"scheme": "b1-aesgcm-sha256/v1", "key_id": "abc"},
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
            self.patch_common(root, fake_database)

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_download_create(
                        main.ModelDownloadCreate(
                            manifest=payload,
                            confirm=True,
                            credential_secret_name="runtime:token",
                        )
                    )
                )

            self.assertEqual(raised.exception.status_code, 422)
            self.assertIn("category model-download", str(raised.exception.detail))
            self.assertEqual(fake_database.model_downloads, [])

    def test_model_download_create_requires_declared_license_acceptance(self) -> None:
        digest = hashlib.sha256(b"licensed model").hexdigest()
        payload = manifest_payload(digest, len(b"licensed model"))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        payload["license"]["acceptance_required"] = True
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            self.patch_common(root, fake_database, audit_events)

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(
                    main.admin_model_download_create(
                        main.ModelDownloadCreate(
                            manifest=payload,
                            confirm=True,
                        )
                    )
                )

            result = asyncio.run(
                main.admin_model_download_create(
                    main.ModelDownloadCreate(
                        manifest=payload,
                        confirm=True,
                        accept_license=True,
                    )
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("licence acceptance is required", str(raised.exception.detail))
        self.assertEqual(result["download"]["status"], "queued")
        self.assertTrue(result["download"]["requires_license_acceptance"])
        self.assertTrue(result["download"]["license_accepted"])
        self.assertTrue(result["plan"]["license_accepted"])
        self.assertEqual(len(fake_database.model_downloads), 1)
        self.assertTrue(fake_database.model_downloads[0]["license_accepted"])
        self.assertEqual(audit_events[0]["event_type"], "model_download.created")

    def test_manifest_install_permissions_block_operator_install_planning(self) -> None:
        digest = hashlib.sha256(b"admin-only model").hexdigest()
        payload = manifest_payload(digest, len(b"admin-only model"))
        payload["permissions"] = {"installable_by": ["admin"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.patch_common(root, FakeDatabase({}, [], active_jobs=0))
            self.patch_auth_context(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"models:read"})))

            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_model_install_plan(main.ModelInstallPlanRequest(manifest=payload)))

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("cannot be used for install by this role", str(raised.exception.detail))

    def test_download_plan_reports_profile_compatibility_and_resource_override(self) -> None:
        digest = hashlib.sha256(b"profile download").hexdigest()
        payload = manifest_payload(digest, len(b"profile download"))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        payload["resource_estimate"] = {"vram_gib": 9.0, "ram_gib": 12.0, "disk_gib": 12.0, "context_tokens": 8192}
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase({}, [], active_jobs=0))

            blocked = asyncio.run(main.admin_model_download_plan(main.ModelInstallPlanRequest(manifest=payload)))
            overridden = asyncio.run(main.admin_model_download_plan(main.ModelInstallPlanRequest(manifest=payload, allow_resource_override=True)))

        self.assertFalse(blocked["can_download"])
        self.assertEqual(blocked["profile_compatibility"][0]["profile_id"], "everyday-llm-7-9b-q4")
        self.assertTrue(any("profile everyday-llm-7-9b-q4" in item for item in blocked["blockers"]))
        self.assertTrue(overridden["can_download"])
        self.assertTrue(overridden["resource_override"])
        self.assertGreaterEqual(len(overridden["profile_compatibility"][0]["warnings"]), 1)

    def test_manifest_install_permissions_block_operator_download_and_install_mutations(self) -> None:
        data = b"admin-only model"
        digest = hashlib.sha256(data).hexdigest()
        payload = manifest_payload(digest, len(data))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        payload["permissions"] = {"installable_by": ["admin"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_path = root / "models" / "blobs" / digest
            blob_path.parent.mkdir(parents=True)
            blob_path.write_bytes(data)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            self.patch_common(root, fake_database)
            self.patch_auth_context(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"models:write"})))

            with self.assertRaises(main.HTTPException) as download_blocked:
                asyncio.run(main.admin_model_download_create(main.ModelDownloadCreate(manifest=payload, confirm=True)))
            with self.assertRaises(main.HTTPException) as install_blocked:
                asyncio.run(main.admin_model_install(main.ModelInstallRequest(manifest=payload, confirm=True, accept_license=True)))

        self.assertEqual(download_blocked.exception.status_code, 403)
        self.assertEqual(install_blocked.exception.status_code, 403)
        self.assertEqual(fake_database.model_downloads, [])
        self.assertEqual(fake_database.records, [])

    def test_model_download_pause_and_resume_are_audited(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_queued",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": "a" * 64,
                    "target_size_bytes": 10,
                    "bytes_downloaded": 4,
                    "status": "queued",
                    "stage": "queued",
                    "manifest": manifest_payload("a" * 64, 10),
                    "credential_secret_name": None,
                    "error_category": None,
                    "error_message": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database, audit_events)

            paused = asyncio.run(main.admin_model_download_pause("modeldl_queued"))
            resumed = asyncio.run(main.admin_model_download_resume("modeldl_queued"))

            self.assertEqual(paused["status"], "paused")
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual([event["event_type"] for event in audit_events], ["model_download.pause_requested", "model_download.resume_requested"])
            self.assertEqual(audit_events[0]["metadata"]["bytes_downloaded"], 4)

    def test_model_download_pause_running_and_reject_invalid_resume(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_running",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": "a" * 64,
                    "target_size_bytes": 10,
                    "bytes_downloaded": 4,
                    "status": "running",
                    "stage": "downloading",
                    "manifest": manifest_payload("a" * 64, 10),
                    "credential_secret_name": None,
                    "error_category": None,
                    "error_message": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database, audit_events)

            paused = asyncio.run(main.admin_model_download_pause("modeldl_running"))

            self.assertEqual(paused["status"], "pausing")
            with self.assertRaises(main.HTTPException) as resume_active:
                asyncio.run(main.admin_model_download_resume("modeldl_running"))
            self.assertEqual(resume_active.exception.status_code, 409)

            with self.assertRaises(main.HTTPException) as missing:
                asyncio.run(main.admin_model_download_pause("modeldl_missing"))
            self.assertEqual(missing.exception.status_code, 404)

    def test_model_download_retry_requeues_failed_download_and_records_audit(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_failed",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": "a" * 64,
                    "target_size_bytes": 10,
                    "bytes_downloaded": 4,
                    "status": "failed",
                    "stage": "failed",
                    "manifest": manifest_payload("a" * 64, 10),
                    "credential_secret_name": None,
                    "error_category": "ModelLifecycleError",
                    "error_message": "network interrupted",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(main.admin_model_download_retry("modeldl_failed"))

            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["stage"], "queued")
            self.assertEqual(result["bytes_downloaded"], 4)
            self.assertIsNone(result["error_category"])
            self.assertEqual(fake_database.model_downloads[0]["status"], "queued")
            self.assertEqual(audit_events[0]["event_type"], "model_download.retry_requested")
            self.assertEqual(audit_events[0]["metadata"]["bytes_downloaded"], 4)

    def test_model_download_retry_rejects_active_or_missing_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_running",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": "a" * 64,
                    "target_size_bytes": 10,
                    "bytes_downloaded": 4,
                    "status": "running",
                    "stage": "downloading",
                    "manifest": manifest_payload("a" * 64, 10),
                    "credential_secret_name": None,
                    "error_category": None,
                    "error_message": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database)

            with self.assertRaises(main.HTTPException) as active:
                asyncio.run(main.admin_model_download_retry("modeldl_running"))
            self.assertEqual(active.exception.status_code, 409)

            with self.assertRaises(main.HTTPException) as missing:
                asyncio.run(main.admin_model_download_retry("modeldl_missing"))
            self.assertEqual(missing.exception.status_code, 404)

    def test_completed_model_download_can_be_installed_from_stored_manifest(self) -> None:
        data = b"downloaded model"
        digest = hashlib.sha256(data).hexdigest()
        payload = manifest_payload(digest, len(data))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_path = root / "models" / "blobs" / digest
            blob_path.parent.mkdir(parents=True)
            blob_path.write_bytes(data)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_completed",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": digest,
                    "target_size_bytes": len(data),
                    "bytes_downloaded": len(data),
                    "status": "completed",
                    "stage": "verified",
                    "manifest": payload,
                    "credential_secret_name": "model-download:hf",
                    "error_category": None,
                    "error_message": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": now,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(
                main.admin_model_download_install(
                    "modeldl_completed",
                    main.ModelDownloadInstallRequest(confirm=True, accept_license=True),
                )
            )

        self.assertEqual(result["model"]["status"], "installed")
        self.assertEqual(fake_database.row["id"], "chat-small")
        self.assertEqual(fake_database.row["manifest"]["installation_status"], "installed")
        self.assertEqual(result["runtime_views"][0]["runtime"], "localai")
        self.assertEqual(audit_events[0]["event_type"], "model.installed")
        self.assertEqual(audit_events[0]["metadata"]["source_download_id"], "modeldl_completed")
        self.assertTrue(audit_events[0]["metadata"]["download_authenticated"])

    def test_model_download_install_blocks_incomplete_or_missing_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            now = datetime.now(tz=UTC)
            fake_database.model_downloads.append(
                {
                    "id": "modeldl_running",
                    "owner_id": "test-admin",
                    "model_id": "chat-small",
                    "model_version": "1.0.0",
                    "source_url": "https://downloads.example.org/model.gguf",
                    "target_sha256": "a" * 64,
                    "target_size_bytes": 10,
                    "bytes_downloaded": 4,
                    "status": "running",
                    "stage": "downloading",
                    "manifest": manifest_payload("a" * 64, 10),
                    "credential_secret_name": None,
                    "error_category": None,
                    "error_message": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "cancelled_at": None,
                }
            )
            self.patch_common(root, fake_database)

            with self.assertRaises(main.HTTPException) as active:
                asyncio.run(
                    main.admin_model_download_install(
                        "modeldl_running",
                        main.ModelDownloadInstallRequest(confirm=True),
                    )
                )
            with self.assertRaises(main.HTTPException) as missing:
                asyncio.run(
                    main.admin_model_download_install(
                        "modeldl_missing",
                        main.ModelDownloadInstallRequest(confirm=True),
                    )
                )

        self.assertEqual(active.exception.status_code, 409)
        self.assertIn("not completed", active.exception.detail["message"])
        self.assertEqual(active.exception.detail["download"]["install_ready"], False)
        self.assertEqual(missing.exception.status_code, 404)

    def test_model_install_plan_accepts_remote_manifest_url(self) -> None:
        data = b"tiny remote model"
        digest = hashlib.sha256(data).hexdigest()
        payload = manifest_payload(digest, len(data))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        body = main.json.dumps(payload).encode("utf-8")

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json", "content-length": str(len(body))}

            async def aiter_bytes(self):
                yield body

        class FakeStreamContext:
            async def __aenter__(self) -> FakeResponse:
                return FakeResponse()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            self.patch_common(root, fake_database)
            original_client = main.httpx.AsyncClient
            FakeAsyncClient.calls = []
            main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
            self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

            result = asyncio.run(
                main.admin_model_install_plan(
                    main.ModelInstallPlanRequest(manifest_url="https://manifests.example.org/chat-small.manifest.json")
                )
            )

        self.assertEqual(result["model_ref"], "chat-small@1.0.0")
        self.assertEqual(result["model"]["source"]["url"], "https://downloads.example.org/model.gguf")
        self.assertEqual(FakeAsyncClient.calls[0]["method"], "GET")
        self.assertEqual(FakeAsyncClient.calls[0]["url"], "https://manifests.example.org/chat-small.manifest.json")
        self.assertIn("application/json", FakeAsyncClient.calls[0]["headers"]["Accept"])
        self.assertEqual(result["profile_compatibility"][0]["profile_id"], "everyday-llm-7-9b-q4")

    def test_remote_manifest_fetch_revalidates_url_before_streaming(self) -> None:
        class FakeStreamContext:
            async def __aenter__(self) -> Any:
                raise AssertionError("stream should not open for an unsafe manifest URL")

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext()

        original_client = main.httpx.AsyncClient
        original_resolver = security.resolve_hostname_addresses
        FakeAsyncClient.calls = []
        resolve_calls = 0

        def rebinding_resolver(hostname: str, port: int | None) -> list[str]:
            nonlocal resolve_calls
            resolve_calls += 1
            return ["93.184.216.34"] if resolve_calls == 1 else ["127.0.0.1"]

        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        security.resolve_hostname_addresses = rebinding_resolver
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original_resolver))

        with self.assertRaises(main.HTTPException) as blocked:
            asyncio.run(main.fetch_remote_manifest_payload("https://manifests.example.org/chat-small.manifest.json"))

        self.assertEqual(blocked.exception.status_code, 422)
        self.assertIn("not allowed by import policy", str(blocked.exception.detail))
        self.assertGreaterEqual(resolve_calls, 2)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_remote_manifest_url_rejects_credentials_and_conflicting_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_common(Path(tmp), FakeDatabase({}, [], active_jobs=0))

            with self.assertRaises(main.HTTPException) as sensitive:
                asyncio.run(
                    main.admin_model_install_plan(
                        main.ModelInstallPlanRequest(manifest_url="https://manifests.example.org/model.json?token=secret")
                    )
                )
            self.assertEqual(sensitive.exception.status_code, 422)
            self.assertIn("credential query", str(sensitive.exception.detail))

            for manifest_url in [
                "https://manifests.example.org/model.json?download_token=secret",
                "https://manifests.example.org/model.json?X-Amz-Signature=secret",
                "https://manifests.example.org/model.json?X-Goog-Credential=secret",
            ]:
                with self.subTest(manifest_url=manifest_url):
                    with self.assertRaises(main.HTTPException) as signed:
                        asyncio.run(main.admin_model_install_plan(main.ModelInstallPlanRequest(manifest_url=manifest_url)))
                    self.assertEqual(signed.exception.status_code, 422)
                    self.assertIn("credential query", str(signed.exception.detail))

            with self.assertRaises(main.HTTPException) as conflict:
                asyncio.run(
                    main.admin_model_install_plan(
                        main.ModelInstallPlanRequest(model="chat-default", manifest_url="https://manifests.example.org/model.json")
                    )
                )
            self.assertEqual(conflict.exception.status_code, 422)
            self.assertIn("only one", str(conflict.exception.detail))

    def test_model_install_persists_available_manifest_as_installed(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        payload = manifest_payload(digest, len(data))
        payload["source"] = {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"}
        payload["installation_status"] = "available"
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_path = root / "models" / "blobs" / digest
            blob_path.parent.mkdir(parents=True)
            blob_path.write_bytes(data)
            fake_database = FakeDatabase({}, [], active_jobs=0)
            self.patch_common(root, fake_database, audit_events)

            result = asyncio.run(
                main.admin_model_install(
                    main.ModelInstallRequest(
                        manifest=payload,
                        confirm=True,
                        accept_license=True,
                        smoke_test=False,
                    )
                )
            )

        self.assertEqual(result["model"]["status"], "installed")
        self.assertEqual(fake_database.row["manifest"]["installation_status"], "installed")
        self.assertEqual(result["model"]["manifest"]["installation_status"], "installed")
        self.assertEqual(audit_events[0]["event_type"], "model.installed")

    def test_model_smoke_test_persists_measurements_and_updates_resource_label(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = {
                "id": "chat-small",
                "version": "1.0.0",
                "display_name": "Chat Small",
                "modality": "llm",
                "preferred_runtime": "localai",
                "status": "installed",
                "resource_label": "recommended",
                "manifest": manifest_payload(digest, len(data)),
            }
            fake_database = FakeDatabase(row, [row], active_jobs=0)
            self.patch_common(root, fake_database, audit_events)

            async def fake_runtime_smoke(manifest: Any, auth: Any) -> dict[str, Any]:
                return {
                    "id": "modelsmoke_test",
                    "type": "install-smoke",
                    "status": "ok",
                    "runtime": manifest.preferred_runtime,
                    "model_alias": manifest.aliases[0],
                    "resolved_model_version": f"{manifest.id}@{manifest.version}",
                    "started_at": "2026-07-23T00:00:00+00:00",
                    "completed_at": "2026-07-23T00:00:03+00:00",
                    "duration_ms": 3000,
                    "load_time_ms": 1200,
                    "run_time_ms": 300,
                    "peak_vram_mib": 11264,
                    "peak_ram_mib": 8192,
                    "hook": {"status": "ok", "runtime": "localai"},
                }

            self.patch_attr("run_model_runtime_smoke", fake_runtime_smoke)

            result = asyncio.run(
                main.admin_model_smoke_test(
                    "chat-small",
                    "1.0.0",
                    main.ModelSmokeTestRequest(persist=True),
                )
            )

            self.assertTrue(result["persisted"])
            self.assertEqual(result["smoke_test"]["status"], "ok")
            self.assertEqual(result["measurement"]["resource_label"], "offload-required")
            updated_manifest = fake_database.row["manifest"]
            self.assertEqual(updated_manifest["resource_estimate"]["vram_gib"], 11.0)
            self.assertEqual(updated_manifest["resource_estimate"]["ram_gib"], 8.0)
            self.assertEqual(updated_manifest["measurements"]["original_resource_estimate"]["vram_gib"], 4)
            self.assertEqual(updated_manifest["measurements"]["runs"][0]["id"], "modelsmoke_test")
            self.assertEqual(result["model"]["resource_label"], "offload-required")
            self.assertEqual(audit_events[0]["event_type"], "model.smoke_tested")
            self.assertTrue(audit_events[0]["metadata"]["persisted"])

    def test_acceptance_model_measurement_coverage_passes_for_default_aliases(self) -> None:
        runtime_by_alias = {
            "chat-default": "localai",
            "image-default": "comfyui",
            "tts-quality": "voicebox",
            "tts-fast": "audio-cpu",
            "stt-default": "audio-cpu",
            "image-edit": "comfyui",
            "video-text": "comfyui",
        }
        manifests = {
            alias: measured_manifest_payload(alias, runtime)
            for alias, runtime in runtime_by_alias.items()
        }
        aliases = [
            {
                "id": alias,
                "status": "installed",
                "modality": manifests[alias]["modality"],
                "preferred_runtime": runtime,
                "runtimes": [runtime],
                "resource_label": "expected",
                "resolved_model": {
                    "id": manifests[alias]["id"],
                    "version": manifests[alias]["version"],
                    "display_name": manifests[alias]["display_name"],
                },
            }
            for alias, runtime in runtime_by_alias.items()
        ]
        records = [model_record_from_manifest(manifest) for manifest in manifests.values()]

        coverage = main.acceptance_model_measurement_coverage(aliases, records)

        self.assertEqual(coverage["status"], "ok")
        self.assertEqual(coverage["missing_aliases"], [])
        self.assertEqual(coverage["ready_count"], 7)
        self.assertEqual(coverage["blocked_count"], 0)
        self.assertEqual(coverage["next_actions"], [])
        self.assertEqual(coverage["blocker_summary"], [])
        self.assertTrue(coverage["handoff_plan"]["ready"])
        gpu_group = next(group for group in coverage["groups"] if group["id"] == "gpu_acceptance")
        self.assertEqual(gpu_group["status"], "ok")
        chat = next(item for item in gpu_group["measurements"] if item["alias"] == "chat-default")
        self.assertTrue(chat["ready"])
        self.assertEqual(chat["latest_ok_run"]["peak_vram_mib"], 4096)
        self.assertNotIn("unsafe_extra", chat["latest_ok_run"])

    def test_acceptance_model_measurement_coverage_reports_missing_smoke_and_runtime_mismatch(self) -> None:
        chat_manifest = measured_manifest_payload("chat-default", "audio-cpu")
        image_manifest = measured_manifest_payload("image-default", "comfyui")
        image_manifest["measurements"]["runs"] = []
        aliases = [
            {
                "id": "chat-default",
                "status": "installed",
                "preferred_runtime": "audio-cpu",
                "runtimes": ["audio-cpu"],
                "resolved_model": {"id": chat_manifest["id"], "version": chat_manifest["version"]},
            },
            {
                "id": "image-default",
                "status": "installed",
                "preferred_runtime": "comfyui",
                "runtimes": ["comfyui"],
                "resolved_model": {"id": image_manifest["id"], "version": image_manifest["version"]},
            },
        ]
        records = [model_record_from_manifest(chat_manifest), model_record_from_manifest(image_manifest)]

        coverage = main.acceptance_model_measurement_coverage(aliases, records)

        self.assertEqual(coverage["status"], "incomplete")
        self.assertIn("chat-default", coverage["missing_aliases"])
        self.assertIn("image-default", coverage["missing_aliases"])
        localai_group = next(group for group in coverage["groups"] if group["id"] == "localai_runtime")
        chat = localai_group["measurements"][0]
        self.assertIn("does not match expected runtime localai", "; ".join(chat["blockers"]))
        gpu_group = next(group for group in coverage["groups"] if group["id"] == "gpu_acceptance")
        image = next(item for item in gpu_group["measurements"] if item["alias"] == "image-default")
        self.assertIn("no persisted ok model smoke measurement exists", image["blockers"])
        self.assertEqual(coverage["blocked_count"], 7)
        actions_by_alias = {item["alias"]: item for item in coverage["next_actions"]}
        self.assertIn("chat-default", actions_by_alias)
        self.assertIn("Select or install a localai-compatible model", actions_by_alias["chat-default"]["action"])
        self.assertIn("image-default", actions_by_alias)
        self.assertIn("Run the Control Center model smoke action", actions_by_alias["image-default"]["action"])
        summary = {item["blocker"]: item["aliases"] for item in coverage["blocker_summary"]}
        self.assertIn("no persisted ok model smoke measurement exists", summary)
        self.assertIn("image-default", summary["no persisted ok model smoke measurement exists"])

    def test_admin_models_includes_acceptance_measurement_coverage(self) -> None:
        manifest = measured_manifest_payload("chat-default", "localai")
        record = model_record_from_manifest(manifest)

        class FakeCatalog:
            def to_catalog(self) -> dict[str, Any]:
                return {
                    "aliases": [
                        {
                            "id": "chat-default",
                            "status": "installed",
                            "preferred_runtime": "localai",
                            "runtimes": ["localai"],
                            "resolved_model": {
                                "id": manifest["id"],
                                "version": manifest["version"],
                                "display_name": manifest["display_name"],
                            },
                        }
                    ],
                    "profiles": [],
                    "models": [],
                }

        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeDatabase(record, [record])
            self.patch_common(Path(tmp), fake_database, audit_events)
            self.patch_attr("catalog_snapshot", lambda: FakeCatalog())

            result = asyncio.run(main.admin_models())

        self.assertIn("acceptance_model_measurements", result)
        self.assertEqual(result["acceptance_model_measurements"]["status"], "incomplete")
        self.assertEqual(result["acceptance_model_measurements"]["groups"][0]["measurements"][0]["alias"], "chat-default")

    def test_model_smoke_runtime_job_forwards_manifest_runtime_smoke_payload(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        runtime_smoke = {
            "schema": "b1-ai-hub-runtime-smoke/v1",
            "localai": {"request": {"messages": [{"role": "user", "content": "ping"}]}},
        }
        manifest = main.model_lifecycle.parse_uploaded_manifest({**manifest_payload(digest, len(data)), "runtime_smoke": runtime_smoke})
        auth = AuthContext(subject_id="test-admin", role=Role.ADMIN, scopes=frozenset({"*"}))

        job = main.model_smoke_runtime_job(manifest, auth)
        payload = main.runtime_control_payload_for_smoke(job)

        self.assertEqual(job["request_params"]["runtime_smoke"], runtime_smoke)
        self.assertEqual(payload["runtime_smoke"], runtime_smoke)
        self.assertEqual(payload["runtime_smoke_config"], runtime_smoke["localai"])

    def test_model_runtime_smoke_uses_gpu_preparation_and_records_peak_metrics(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "preferred_runtime": "localai",
            "status": "installed",
            "resource_label": "recommended",
            "manifest": manifest_payload(digest, len(data)),
        }
        fake_database = FakeDatabase(row, [row], active_jobs=0)
        self.patch_common(Path(tempfile.gettempdir()), fake_database)
        manifest = main.model_lifecycle.parse_uploaded_manifest(row["manifest"])
        auth = AuthContext(subject_id="test-admin", role=Role.ADMIN, scopes=frozenset({"*"}))

        class FakeRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.metrics = [
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 2048}]}, "memory": {"available": True, "used_bytes": 4 * 1024 * 1024 * 1024}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 6144}]}, "memory": {"available": True, "used_bytes": 8 * 1024 * 1024 * 1024}},
                ]

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                self.calls.append(f"metrics:{path}")
                return self.metrics.pop(0)

            async def unload_other_gpu_runtimes(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
                self.calls.append("unload")
                return [{"status": "ok"}]

            async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
                self.calls.append("verify")

            async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("load")
                return {"status": "ok"}

            async def warm_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("warm")
                return {"status": "ok"}

            async def smoke_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("smoke")
                return {"status": "ok", "runtime": "localai", "measurements": {"peak_vram_mib": 4096}}

            async def record_runtime_idle_for_job(self, job: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
                self.calls.append("idle")
                return {"runtime": job["runtime"], "details": details or {}}

            def gpu_memory_used_mib(self, metrics: dict[str, Any] | None) -> list[int] | None:
                devices = ((metrics or {}).get("gpu") or {}).get("devices") or []
                values = [int(item["memory_used_mib"]) for item in devices if "memory_used_mib" in item]
                return values or None

            def host_ram_used_mib(self, metrics: dict[str, Any] | None) -> int | None:
                used = ((metrics or {}).get("memory") or {}).get("used_bytes")
                return int(used / (1024 * 1024)) if isinstance(used, int) else None

        runner = FakeRunner()
        self.patch_attr("runtime_control_runner", lambda lease_ttl_seconds=None: runner)

        result = asyncio.run(main.run_model_runtime_smoke(manifest, auth))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime"], "localai")
        self.assertEqual(result["peak_vram_mib"], 6144)
        self.assertEqual(result["peak_ram_mib"], 8192)
        self.assertEqual(
            runner.calls,
            ["metrics:/v1/metrics", "unload", "verify", "load", "warm", "smoke", "metrics:/v1/metrics", "idle"],
        )
        self.assertEqual(len(fake_database.leases), 1)
        self.assertEqual(fake_database.releases, [fake_database.leases[0]["owner"]])

    def test_model_runtime_smoke_rejects_production_hardware_policy_before_lease(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        row = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "preferred_runtime": "localai",
            "status": "installed",
            "resource_label": "recommended",
            "manifest": manifest_payload(digest, len(data)),
        }
        fake_database = FakeDatabase(row, [row], active_jobs=0)
        self.patch_common(Path(tempfile.gettempdir()), fake_database)
        self.patch_attr(
            "settings",
            replace(
                main.settings,
                runtime_deployment_mode="production",
                gpu_total_vram_gib=12.0,
                gpu_usable_vram_gib=10.5,
                gpu_reserve_vram_gib=1.5,
                host_total_ram_gib=32.0,
                host_reserve_ram_gib=6.0,
            ),
        )

        metrics_calls: list[str] = []

        async def fake_runtime_agent_get(path: str) -> tuple[dict[str, Any], None]:
            metrics_calls.append(path)
            return (
                {
                    "gpu": {
                        "available": True,
                        "devices": [
                            {
                                "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                "memory_total_mib": 6144,
                                "memory_free_mib": 4096,
                            }
                        ],
                    },
                    "memory": {"total_bytes": 31 * 1024 * 1024 * 1024, "available_bytes": 8 * 1024 * 1024 * 1024},
                },
                None,
            )

        class UnexpectedRunner:
            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

            async def unload_other_gpu_runtimes(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

            async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

            async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

            async def warm_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

            async def smoke_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("runtime preparation must not run after hardware-policy rejection")

        self.patch_attr("runtime_agent_get", fake_runtime_agent_get)
        self.patch_attr("runtime_control_runner", lambda lease_ttl_seconds=None: UnexpectedRunner())
        manifest = main.model_lifecycle.parse_uploaded_manifest(row["manifest"])
        auth = AuthContext(subject_id="test-admin", role=Role.ADMIN, scopes=frozenset({"*"}))

        with self.assertRaises(main.HTTPException) as raised:
            asyncio.run(main.run_model_runtime_smoke(manifest, auth))

        detail = raised.exception.detail
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(detail["code"], "hardware_resource_policy")
        self.assertEqual(detail["hardware_resource_policy"]["status"], "failed")
        self.assertIn("largest GPU VRAM is 6144 MiB", detail["hardware_resource_policy"]["detail"])
        self.assertEqual(metrics_calls, ["/v1/metrics"])
        self.assertEqual(fake_database.leases, [])
        self.assertEqual(fake_database.releases, [])


if __name__ == "__main__":
    unittest.main()
