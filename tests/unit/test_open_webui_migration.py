from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

OLD_STACK_BACKUP_PATH = SCRIPTS / "old_stack_backup.py"
old_stack_spec = importlib.util.spec_from_file_location("old_stack_backup", OLD_STACK_BACKUP_PATH)
old_stack_backup = importlib.util.module_from_spec(old_stack_spec)
sys.modules["old_stack_backup"] = old_stack_backup
assert old_stack_spec.loader is not None
old_stack_spec.loader.exec_module(old_stack_backup)

OPEN_WEBUI_MIGRATION_PATH = SCRIPTS / "open_webui_migration.py"
open_webui_spec = importlib.util.spec_from_file_location("b1_open_webui_migration", OPEN_WEBUI_MIGRATION_PATH)
open_webui_migration = importlib.util.module_from_spec(open_webui_spec)
sys.modules["b1_open_webui_migration"] = open_webui_migration
assert open_webui_spec.loader is not None
open_webui_spec.loader.exec_module(open_webui_migration)


class OpenWebUiMigrationTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def create_webui_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT)")
            connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, title TEXT)")
            connection.execute("INSERT INTO user (id, name) VALUES ('user_1', 'Admin')")
            connection.execute("INSERT INTO chat (id, title) VALUES ('chat_1', 'Private prompt title')")

    def inventory(
        self,
        root: Path,
        database_path: Path,
        *,
        open_webui_image: str = "ghcr.io/open-webui/open-webui:v0.6.18",
    ) -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-host-inventory/v1",
            "created_at": "2026-07-23T12:00:00+00:00",
            "classification": {
                "containers": [
                    {
                        "container": "old-open-webui",
                        "image": open_webui_image,
                        "classification": "candidate-old-ai-stack-review-required",
                    }
                ]
            },
            "paths": {
                "open_webui_data_candidates": [{"path": str(database_path.parent), "exists": True, "type": "directory"}],
                "open_webui_data_roots": [{"path": str(database_path.parent), "exists": True, "type": "directory"}],
                "open_webui_database_candidates": [
                    {
                        "path": str(database_path),
                        "exists": True,
                        "type": "file",
                        "sqlite": {
                            "readable": True,
                            "tables": ["chat", "user"],
                            "table_counts": {"chat": 1, "user": 1},
                            "content_rows_read": False,
                        },
                    }
                ],
            },
            "migration_readiness": {
                "open_webui": {"database_candidate_count": 1, "readable_sqlite_count": 1},
                "open_webui_data_roots": {
                    "candidate_count": 1,
                    "existing_directory_count": 1,
                    "unreadable_or_unscannable": [],
                },
            },
        }

    def scope(self, include_path: Path) -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-old-stack-backup-scope/v1",
            "operator_reviewed": True,
            "reviewed_by": "unit-test",
            "review_notes": "Open WebUI migration preservation test",
            "include_paths": [{"path": str(include_path), "reason": "old Open WebUI data"}],
            "include_docker_volumes": [],
            "include_containers": [],
        }

    def make_backup(self, root: Path, include_path: Path) -> Path:
        scope_path = self.write_json(root / "scope.json", self.scope(include_path))
        return old_stack_backup.backup_old_stack(
            scope_path=scope_path,
            output_root=root / "backups",
            label="open-webui-unit",
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )

    def test_plan_marks_readable_database_covered_by_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            inventory_path = self.write_json(root / "inventory.json", self.inventory(root, database_path))
            backup_dir = self.make_backup(root, database_path.parent)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/srv/b1-ai-hub/restore-tests/open-webui-migration",
                now=datetime(2026, 7, 23, 13, 0, tzinfo=UTC),
            )

        self.assertEqual(plan["format"], "b1-ai-hub-open-webui-migration-plan/v1")
        self.assertTrue(plan["safety"]["does_not_read_database_rows"])
        self.assertTrue(plan["safety"]["does_not_import_automatically"])
        self.assertEqual(plan["open_webui"]["readable_database_count"], 1)
        self.assertEqual(plan["open_webui"]["data_path_candidates"], [str(database_path.parent)])
        self.assertEqual(plan["open_webui"]["unreadable_data_roots"], [])
        self.assertEqual(plan["open_webui"]["backed_up_database_candidate_count"], 1)
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["backup_coverage"], "covered")
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["table_counts"], {"chat": 1, "user": 1})
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["content_rows_read"], False)
        self.assertEqual(plan["open_webui"]["recommended_strategy"], "preserve-backed-up-sqlite-and-test-supported-open-webui-import")
        version_evidence = plan["open_webui"]["version_evidence"]
        self.assertEqual(version_evidence["compatibility_status"], "source-version-recorded-temporary-validation-required")
        self.assertFalse(version_evidence["direct_database_reuse_approved_by_plan"])
        self.assertTrue(version_evidence["requires_supported_open_webui_migration_path"])
        self.assertEqual(version_evidence["source_containers"][0]["container"], "old-open-webui")
        self.assertEqual(version_evidence["source_containers"][0]["tag"], "v0.6.18")
        self.assertEqual(version_evidence["source_containers"][0]["version_hint"], "v0.6.18")
        self.assertTrue(plan["open_webui"]["backup_database_artifacts"][0]["sensitive"])
        self.assertNotIn("Private prompt title", json.dumps(plan, sort_keys=True))
        self.assertEqual(plan["warnings"], [])

    def test_plan_warns_when_open_webui_root_is_discovered_but_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "old-open-webui" / "compose.yaml"
            compose.parent.mkdir(parents=True, exist_ok=True)
            compose.write_text("services: {}\n", encoding="utf-8")
            inventory_payload = self.inventory(root, root / "unreadable" / "webui.db")
            inventory_payload["paths"]["open_webui_database_candidates"] = []
            inventory_payload["paths"]["open_webui_data_roots"] = [
                {"path": "/var/lib/docker/volumes/open-webui/_data", "exists": None, "error": "PermissionError: denied"}
            ]
            inventory_payload["migration_readiness"]["open_webui"] = {"database_candidate_count": 0, "readable_sqlite_count": 0}
            inventory_payload["migration_readiness"]["open_webui_data_roots"] = {
                "candidate_count": 1,
                "existing_directory_count": 0,
                "unreadable_or_unscannable": [
                    {"path": "/var/lib/docker/volumes/open-webui/_data", "reason": "PermissionError: denied"}
                ],
            }
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
            backup_dir = self.make_backup(root, compose)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/restore/open-webui",
            )

        self.assertEqual(
            plan["open_webui"]["unreadable_data_roots"],
            [{"path": "/var/lib/docker/volumes/open-webui/_data", "reason": "PermissionError: denied"}],
        )
        self.assertTrue(any("could not be scanned" in warning for warning in plan["warnings"]))
        self.assertEqual(plan["open_webui"]["recommended_strategy"], "no-readable-open-webui-database-found-preserve-old-stack")

    def test_plan_warns_when_readable_database_is_not_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            unrelated = root / "old-open-webui" / "compose.yaml"
            unrelated.parent.mkdir(parents=True, exist_ok=True)
            unrelated.write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(root / "inventory.json", self.inventory(root, database_path))
            backup_dir = self.make_backup(root, unrelated)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/restore/open-webui",
            )

        self.assertEqual(plan["open_webui"]["backed_up_database_candidate_count"], 0)
        self.assertEqual(plan["open_webui"]["recommended_strategy"], "back-up-open-webui-before-cutover")
        self.assertTrue(any("not covered by the verified old-stack backup" in warning for warning in plan["warnings"]))

    def test_plan_warns_when_open_webui_image_tag_is_floating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(root, database_path, open_webui_image="ghcr.io/open-webui/open-webui:latest"),
            )
            backup_dir = self.make_backup(root, database_path.parent)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/restore/open-webui",
            )

        self.assertEqual(plan["open_webui"]["version_evidence"]["compatibility_status"], "manual-source-version-review-required")
        self.assertTrue(plan["open_webui"]["version_evidence"]["source_containers"][0]["floating_or_missing_tag"])
        self.assertTrue(any("floating or missing tag" in warning for warning in plan["warnings"]))

    def test_digest_pinned_open_webui_image_is_exact_version_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            digest = "sha256:" + "a" * 64
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(root, database_path, open_webui_image=f"ghcr.io/open-webui/open-webui@{digest}"),
            )
            backup_dir = self.make_backup(root, database_path.parent)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/restore/open-webui",
            )

        source = plan["open_webui"]["version_evidence"]["source_containers"][0]
        self.assertTrue(source["digest_present"])
        self.assertFalse(source["floating_or_missing_tag"])
        self.assertEqual(source["version_hint"], digest)
        self.assertEqual(plan["warnings"], [])

    def test_write_plan_uses_private_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = open_webui_migration.write_plan({"format": "test"}, root / "plan.json")

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["format"], "test")


if __name__ == "__main__":
    unittest.main()
