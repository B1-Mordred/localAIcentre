from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
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


class FakeDatabase:
    def __init__(
        self,
        row: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        active_jobs: int = 0,
        voice_profiles: list[dict[str, Any]] | None = None,
    ) -> None:
        self.row = row
        self.records = records
        self.active_jobs = active_jobs
        self.voice_profiles = voice_profiles or []
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
        self.assertIn("catalog", result)
        self.assertEqual(result["records"], [])

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
            self.assertEqual(audit_events[0]["event_type"], "model.blobs_quarantined")
            self.assertEqual(audit_events[0]["target_id"], "chat-small@1.0.0")
            self.assertEqual(audit_events[0]["metadata"]["moved"][0]["sha256"], digest)

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
            self.assertEqual(fake_database.model_downloads[0]["credential_secret_name"], "model-download:hf")
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
        self.assertTrue(result["plan"]["license_accepted"])
        self.assertEqual(len(fake_database.model_downloads), 1)
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


if __name__ == "__main__":
    unittest.main()
