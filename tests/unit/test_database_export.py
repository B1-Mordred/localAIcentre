from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import database  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "sqlalchemy":
        raise
    database = None


@unittest.skipIf(database is None, "SQLAlchemy is not installed in this lightweight test environment")
class DatabaseExportTests(unittest.TestCase):
    def test_logical_dump_payload_contains_schema_counts_and_json_safe_rows(self) -> None:
        created = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        payload = database.logical_dump_payload(
            {
                "b1_jobs": [
                    {
                        "id": "job_1",
                        "created_at": created,
                        "started_at": created,
                        "completed_at": created,
                        "native_prompt_id": "prompt_native_1",
                        "load_time_ms": 1234,
                        "run_time_ms": 5678,
                        "peak_vram_mib": 4096,
                        "peak_ram_mib": 8192,
                        "request_params": {"nested": [created]},
                    }
                ],
                "b1_api_clients": [{"id": "client_1", "scopes": ["jobs:read"]}],
                "b1_users": [{"id": "user_1", "username": "admin", "password_hash": "scrypt$test"}],
                "b1_browser_sessions": [{"id": "session_1"}],
                "b1_model_downloads": [
                    {
                        "id": "modeldl_1",
                        "owner_id": "admin_1",
                        "model_id": "chat-small",
                        "model_version": "1.0.0",
                        "source_url": "https://downloads.example.org/model.gguf",
                        "target_sha256": "a" * 64,
                        "target_size_bytes": 12,
                        "bytes_downloaded": 0,
                        "status": "queued",
                        "stage": "queued",
                        "manifest": {"id": "chat-small"},
                        "credential_secret_name": "model-download:hf",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_voice_profiles": [{"id": "vp_1", "display_name": "Narrator", "created_at": created, "updated_at": created}],
                "b1_encrypted_secrets": [
                    {
                        "name": "remote:openai",
                        "display_name": "OpenAI optional provider",
                        "category": "remote-provider",
                        "description": "disabled by default",
                        "secret_envelope": {"scheme": "b1-aesgcm-sha256/v1", "key_id": "abc"},
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_resource_policies": [
                    {
                        "id": "default",
                        "gpu_total_vram_gib": 12.0,
                        "gpu_usable_vram_gib": 10.0,
                        "gpu_reserve_vram_gib": 2.0,
                        "gpu_max_active_pipelines": 1,
                        "host_total_ram_gib": 32.0,
                        "host_reserve_ram_gib": 6.0,
                        "llm_default_context": 8192,
                        "llm_maximum_context": 16384,
                        "llm_default_parallel_requests": 1,
                        "comfyui_maximum_parallel_jobs": 1,
                        "comfyui_maximum_batch_size": 1,
                        "updated_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_admission_policies": [
                    {
                        "id": "default",
                        "max_queued_jobs_per_owner": 20,
                        "max_active_jobs_per_owner": 3,
                        "max_jobs_per_hour_per_owner": 60,
                        "max_queued_jobs_global": 100,
                        "artifact_storage_max_bytes": 0,
                        "artifact_storage_reserve_bytes": 10 * 1024**3,
                        "updated_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_backup_schedules": [
                    {
                        "id": "default",
                        "enabled": True,
                        "interval_hours": 24,
                        "keep_last": 7,
                        "delete_older_than_days": 30,
                        "label_prefix": "scheduled",
                        "next_run_at": created,
                        "last_started_at": created,
                        "last_completed_at": created,
                        "last_status": "completed",
                        "last_backup_name": "scheduled-20260722-120000",
                        "failure_message": None,
                        "updated_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_maintenance_state": [
                    {
                        "id": "default",
                        "enabled": True,
                        "reason": "update window",
                        "started_at": created,
                        "ended_at": None,
                        "updated_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_update_plans": [
                    {
                        "id": "update_1",
                        "target_version": "0.2.0",
                        "source_url": "https://example.org/release",
                        "status": "staged",
                        "stage": "backup_images_and_self_test_completed",
                        "image_refs": [{"service": "control-plane", "image": "image@sha256:" + "a" * 64}],
                        "preflight": {"pinned_images": True},
                        "image_stage": [{"service": "control-plane", "status": "dry_run"}],
                        "compose_override": {
                            "format": "b1-ai-hub-compose-image-override/v1",
                            "path": "/srv/b1-ai-hub/data/control-plane/updates/update_1/compose.images.yaml",
                            "ready_for_promotion": False,
                        },
                        "backup_name": "update-1",
                        "self_test": {"status": "ok"},
                        "rollback_result": {},
                        "notes": "test",
                        "failure_message": None,
                        "created_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                        "staged_at": created,
                        "health_checked_at": None,
                        "rolled_back_at": None,
                    }
                ],
                "b1_model_alias_policies": [
                    {
                        "alias": "chat-default",
                        "enabled": False,
                        "preferred_runtime": "localai",
                        "idle_timeout_seconds": 600,
                        "visibility_roles": ["admin", "operator"],
                        "notes": "maintenance",
                        "updated_by": "admin_1",
                        "created_at": created,
                        "updated_at": created,
                    }
                ],
                "b1_audit_events": [{"id": "audit_1", "event_type": "backup.created", "created_at": created}],
            },
            created_at=created,
        )

        self.assertEqual(payload["format"], "b1-ai-hub-postgres-logical-export/v1")
        self.assertEqual(payload["created_at"], created.isoformat())
        self.assertEqual(payload["row_counts"]["b1_jobs"], 1)
        self.assertEqual(payload["row_counts"]["b1_api_clients"], 1)
        self.assertEqual(payload["row_counts"]["b1_users"], 1)
        self.assertNotIn("b1_browser_sessions", payload["row_counts"])
        self.assertEqual(payload["row_counts"]["b1_model_downloads"], 1)
        self.assertEqual(payload["row_counts"]["b1_voice_profiles"], 1)
        self.assertEqual(payload["row_counts"]["b1_encrypted_secrets"], 1)
        self.assertEqual(payload["row_counts"]["b1_resource_policies"], 1)
        self.assertEqual(payload["row_counts"]["b1_admission_policies"], 1)
        self.assertEqual(payload["row_counts"]["b1_backup_schedules"], 1)
        self.assertEqual(payload["row_counts"]["b1_maintenance_state"], 1)
        self.assertEqual(payload["row_counts"]["b1_update_plans"], 1)
        self.assertEqual(payload["row_counts"]["b1_model_alias_policies"], 1)
        self.assertEqual(payload["row_counts"]["b1_audit_events"], 1)
        jobs = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_jobs")
        self.assertEqual(jobs["row_count"], 1)
        self.assertEqual(jobs["rows"][0]["created_at"], created.isoformat())
        self.assertEqual(jobs["rows"][0]["started_at"], created.isoformat())
        self.assertEqual(jobs["rows"][0]["completed_at"], created.isoformat())
        self.assertEqual(jobs["rows"][0]["load_time_ms"], 1234)
        self.assertEqual(jobs["rows"][0]["run_time_ms"], 5678)
        self.assertEqual(jobs["rows"][0]["peak_vram_mib"], 4096)
        self.assertEqual(jobs["rows"][0]["peak_ram_mib"], 8192)
        self.assertEqual(jobs["rows"][0]["native_prompt_id"], "prompt_native_1")
        self.assertEqual(jobs["rows"][0]["request_params"]["nested"][0], created.isoformat())
        self.assertIn("columns", jobs["schema"])
        self.assertIn("native_prompt_id", {column["name"] for column in jobs["schema"]["columns"]})
        self.assertIn("run_time_ms", {column["name"] for column in jobs["schema"]["columns"]})
        audit_events = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_audit_events")
        self.assertEqual(audit_events["rows"][0]["created_at"], created.isoformat())
        voice_profiles = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_voice_profiles")
        self.assertEqual(voice_profiles["rows"][0]["created_at"], created.isoformat())
        encrypted_secrets = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_encrypted_secrets")
        self.assertEqual(encrypted_secrets["rows"][0]["secret_envelope"]["key_id"], "abc")
        model_downloads = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_model_downloads")
        self.assertEqual(model_downloads["rows"][0]["credential_secret_name"], "model-download:hf")
        self.assertIn("credential_secret_name", {column["name"] for column in model_downloads["schema"]["columns"]})
        resource_policies = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_resource_policies")
        self.assertEqual(resource_policies["rows"][0]["gpu_reserve_vram_gib"], 2.0)
        admission_policies = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_admission_policies")
        self.assertEqual(admission_policies["rows"][0]["artifact_storage_reserve_bytes"], 10 * 1024**3)
        backup_schedules = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_backup_schedules")
        self.assertEqual(backup_schedules["rows"][0]["last_status"], "completed")
        maintenance = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_maintenance_state")
        self.assertTrue(maintenance["rows"][0]["enabled"])
        self.assertEqual(maintenance["rows"][0]["started_at"], created.isoformat())
        update_plans = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_update_plans")
        self.assertEqual(update_plans["rows"][0]["target_version"], "0.2.0")
        self.assertEqual(update_plans["rows"][0]["image_stage"][0]["status"], "dry_run")
        self.assertEqual(update_plans["rows"][0]["compose_override"]["format"], "b1-ai-hub-compose-image-override/v1")
        self.assertEqual(update_plans["rows"][0]["staged_at"], created.isoformat())
        alias_policies = next(table for table in payload["tables"] if table["schema"]["name"] == "b1_model_alias_policies")
        self.assertFalse(alias_policies["rows"][0]["enabled"])
        self.assertEqual(alias_policies["rows"][0]["visibility_roles"], ["admin", "operator"])

    def test_import_plan_reports_upserts_for_all_export_tables(self) -> None:
        payload = database.logical_dump_payload({"b1_jobs": [{"id": "job_1"}]})
        plan = database.logical_dump_import_plan(payload)

        self.assertEqual(plan["status"], "planned")
        self.assertTrue(plan["apply_supported"])
        self.assertEqual(plan["row_counts"]["b1_jobs"], 1)
        self.assertEqual(plan["row_counts"]["b1_models"], 0)
        self.assertTrue(all(operation["mode"] == "upsert" for operation in plan["operations"]))

    def test_validate_logical_dump_rejects_unknown_tables_and_columns(self) -> None:
        payload = database.logical_dump_payload({})
        payload["tables"].append({"schema": {"name": "not_b1"}, "row_count": 0, "rows": []})
        with self.assertRaises(database.LogicalDumpError):
            database.validate_logical_dump(payload)

        payload = database.logical_dump_payload({"b1_jobs": [{"id": "job_1", "unknown": "value"}]})
        with self.assertRaises(database.LogicalDumpError):
            database.validate_logical_dump(payload)

    def test_import_row_coerces_datetime_columns(self) -> None:
        row = database.coerce_import_row(
            database.jobs,
            {"id": "job_1", "created_at": "2026-07-22T12:00:00+00:00", "request_params": {"ok": True}},
        )

        self.assertEqual(row["id"], "job_1")
        self.assertIsInstance(row["created_at"], datetime)
        self.assertEqual(row["request_params"], {"ok": True})

    def test_runtime_reservation_matching_requires_owner_runtime_model_and_active_window(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        reservation = {
            "status": "active",
            "expires_at": datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
            "owner_id": "client_1",
            "runtime": "localai",
            "resolved_model_version": "chat-model@1.0.0",
        }

        self.assertTrue(database.runtime_reservation_matches_request(reservation, "client_1", "localai", "chat-model@1.0.0", now=now))
        self.assertFalse(database.runtime_reservation_matches_request(reservation, "other", "localai", "chat-model@1.0.0", now=now))
        self.assertFalse(database.runtime_reservation_matches_request(reservation, "client_1", "comfyui", "chat-model@1.0.0", now=now))
        self.assertFalse(database.runtime_reservation_matches_request(reservation, "client_1", "localai", "other-model@1.0.0", now=now))
        self.assertFalse(
            database.runtime_reservation_matches_request(
                {**reservation, "expires_at": datetime(2026, 7, 22, 11, 59, tzinfo=UTC)},
                "client_1",
                "localai",
                "chat-model@1.0.0",
                now=now,
            )
        )

    def test_select_claim_candidate_uses_priority_aging_policy(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        rows = [
            {
                "id": "new_chat",
                "priority": "chat",
                "created_at": now,
                "model_alias": "chat-default",
            },
            {
                "id": "old_batch",
                "priority": "batch",
                "created_at": datetime(2026, 7, 22, 7, 0, tzinfo=UTC),
                "model_alias": "video-text",
            },
        ]

        self.assertEqual(database.select_claim_candidate(rows, now=now)["id"], "old_batch")

    def test_unknown_claim_priority_is_treated_as_batch(self) -> None:
        self.assertEqual(database.priority_class_from_value("not-a-priority").value, "batch")

    def test_redis_scheduler_lease_allows_same_owner_and_blocks_other_owner(self) -> None:
        test_case = self

        class FakeRedis:
            def __init__(self) -> None:
                self.value: str | None = None

            async def get(self, key: str) -> str | None:
                test_case.assertEqual(key, database.SCHEDULER_REDIS_LEASE_KEY)
                return self.value

            async def set(self, key: str, value: str, px: int, nx: bool = False) -> bool:
                test_case.assertEqual(key, database.SCHEDULER_REDIS_LEASE_KEY)
                test_case.assertGreater(px, 0)
                if nx and self.value is not None:
                    return False
                self.value = value
                return True

            async def delete(self, key: str) -> int:
                test_case.assertEqual(key, database.SCHEDULER_REDIS_LEASE_KEY)
                self.value = None
                return 1

        fake_redis = FakeRedis()
        database.configure_scheduler_redis(fake_redis)
        self.addCleanup(lambda: database.configure_scheduler_redis(None))

        first = asyncio.run(database.acquire_redis_scheduler_owner("owner-a", 7, 30))
        self.assertTrue(first["acquired"])
        self.assertEqual(database.redis_lease_owner(fake_redis.value), "owner-a")

        blocked = asyncio.run(database.acquire_redis_scheduler_owner("owner-b", 8, 30))
        self.assertFalse(blocked["acquired"])
        self.assertEqual(blocked["current_owner"], "owner-a")

        renewed = asyncio.run(database.acquire_redis_scheduler_owner("owner-a", 7, 30))
        self.assertTrue(renewed["acquired"])
        self.assertTrue(renewed["renewed"])

        release = asyncio.run(database.release_redis_scheduler_owner("owner-a"))
        self.assertTrue(release["released"])
        self.assertIsNone(fake_redis.value)

    def test_redis_lease_value_preserves_owner_strings(self) -> None:
        value = database.redis_lease_value("admin|manual", 12)
        self.assertEqual(database.redis_lease_owner(value), "admin|manual")


if __name__ == "__main__":
    unittest.main()
