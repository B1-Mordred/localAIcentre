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
    from app import main  # noqa: E402
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
    def __init__(self, row: dict[str, Any], records: list[dict[str, Any]], *, active_jobs: int = 0) -> None:
        self.row = row
        self.records = records
        self.active_jobs = active_jobs
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
