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
            connection.execute("CREATE TABLE document (id TEXT PRIMARY KEY, content TEXT)")
            connection.execute("CREATE TABLE file (id TEXT PRIMARY KEY, filename TEXT)")
            connection.execute("CREATE TABLE config (id TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO user (id, name) VALUES ('user_1', 'Admin')")
            connection.execute("INSERT INTO chat (id, title) VALUES ('chat_1', 'Private prompt title')")
            connection.execute("INSERT INTO document (id, content) VALUES ('doc_1', 'Private RAG document text')")
            connection.execute("INSERT INTO file (id, filename) VALUES ('file_1', 'private-source.pdf')")
            connection.execute("INSERT INTO config (id, value) VALUES ('cfg_1', 'private setting')")

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
                "b1_root": {"path": str(root / "b1-ai-hub"), "exists": True, "type": "directory"},
                "open_webui_data_candidates": [{"path": str(database_path.parent), "exists": True, "type": "directory"}],
                "open_webui_data_roots": [{"path": str(database_path.parent), "exists": True, "type": "directory"}],
                "open_webui_database_candidates": [
                    {
                        "path": str(database_path),
                        "exists": True,
                        "type": "file",
                        "sqlite": {
                            "readable": True,
                            "tables": ["chat", "config", "document", "file", "user"],
                            "table_counts": {"chat": 1, "config": 1, "document": 1, "file": 1, "user": 1},
                            "data_domains": {
                                "accounts": {
                                    "tables": ["user"],
                                    "table_count": 1,
                                    "row_counts": {"user": 1},
                                    "known_row_count": 1,
                                    "content_rows_read": False,
                                },
                                "chats": {
                                    "tables": ["chat"],
                                    "table_count": 1,
                                    "row_counts": {"chat": 1},
                                    "known_row_count": 1,
                                    "content_rows_read": False,
                                },
                                "settings": {
                                    "tables": ["config"],
                                    "table_count": 1,
                                    "row_counts": {"config": 1},
                                    "known_row_count": 1,
                                    "content_rows_read": False,
                                },
                                "documents_rag": {
                                    "tables": ["document", "file"],
                                    "table_count": 2,
                                    "row_counts": {"document": 1, "file": 1},
                                    "known_row_count": 2,
                                    "content_rows_read": False,
                                },
                            },
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
        self.assertEqual(len(plan["open_webui"]["old_stack_database_candidates"]), 1)
        self.assertEqual(plan["open_webui"]["current_b1_database_candidates"], [])
        self.assertEqual(plan["open_webui"]["data_path_candidates"], [str(database_path.parent)])
        self.assertEqual(plan["open_webui"]["unreadable_data_roots"], [])
        self.assertEqual(plan["open_webui"]["backed_up_database_candidate_count"], 1)
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["backup_coverage"], "covered")
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["table_counts"]["chat"], 1)
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["table_counts"]["document"], 1)
        self.assertEqual(plan["open_webui"]["database_candidates"][0]["data_domains"]["documents_rag"]["known_row_count"], 2)
        self.assertEqual(plan["open_webui"]["data_domains"]["all_readable"]["documents_rag"]["known_row_count"], 2)
        self.assertEqual(plan["open_webui"]["data_domains"]["backed_up_readable"]["documents_rag"]["known_row_count"], 2)
        self.assertEqual(plan["open_webui"]["data_domains"]["backed_up_readable"]["settings"]["known_row_count"], 1)
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
        self.assertNotIn("Private RAG document text", json.dumps(plan, sort_keys=True))
        self.assertNotIn("private-source.pdf", json.dumps(plan, sort_keys=True))
        self.assertNotIn("private setting", json.dumps(plan, sort_keys=True))
        self.assertEqual(plan["warnings"], [])

    def test_plan_ignores_current_b1_open_webui_database_when_checking_old_backup_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_database_path = root / "old-open-webui" / "data" / "webui.db"
            current_database_path = root / "b1-ai-hub" / "data" / "open-webui" / "webui.db"
            self.create_webui_db(old_database_path)
            self.create_webui_db(current_database_path)
            inventory_payload = self.inventory(root, old_database_path)
            current_candidate = json.loads(json.dumps(inventory_payload["paths"]["open_webui_database_candidates"][0]))
            current_candidate["path"] = str(current_database_path)
            inventory_payload["paths"]["open_webui_database_candidates"].append(current_candidate)
            inventory_payload["paths"]["open_webui_data_candidates"].append(
                {"path": str(current_database_path.parent), "exists": True, "type": "directory"}
            )
            inventory_payload["paths"]["open_webui_data_roots"].append(
                {"path": str(current_database_path.parent), "exists": True, "type": "directory"}
            )
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
            backup_dir = self.make_backup(root, old_database_path.parent)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/srv/b1-ai-hub/restore-tests/open-webui-migration",
                now=datetime(2026, 7, 23, 13, 0, tzinfo=UTC),
            )

        self.assertEqual(plan["open_webui"]["readable_database_count"], 1)
        self.assertEqual(plan["open_webui"]["backed_up_database_candidate_count"], 1)
        self.assertEqual([item["path"] for item in plan["open_webui"]["old_stack_database_candidates"]], [str(old_database_path)])
        self.assertEqual([item["path"] for item in plan["open_webui"]["current_b1_database_candidates"]], [str(current_database_path)])
        self.assertEqual(plan["open_webui"]["database_candidates"][1]["source_role"], "current_b1_open_webui")
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
            [
                {
                    "path": "/var/lib/docker/volumes/open-webui/_data",
                    "reason": "PermissionError: denied",
                    "backup_coverage": "not_covered",
                    "covering_sources": [],
                }
            ],
        )
        self.assertTrue(any("could not be scanned" in warning for warning in plan["warnings"]))
        self.assertEqual(plan["open_webui"]["recommended_strategy"], "no-readable-open-webui-database-found-preserve-old-stack")

    def test_plan_derives_data_domains_from_older_inventory_table_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            inventory_payload = self.inventory(root, database_path)
            sqlite = inventory_payload["paths"]["open_webui_database_candidates"][0]["sqlite"]
            sqlite.pop("data_domains")
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
            backup_dir = self.make_backup(root, database_path.parent)

            plan = open_webui_migration.build_plan(
                inventory_path=inventory_path,
                backup_dir=backup_dir,
                restore_target="/restore/open-webui",
            )

        domains = plan["open_webui"]["database_candidates"][0]["data_domains"]
        self.assertEqual(domains["documents_rag"]["known_row_count"], 2)
        self.assertEqual(plan["open_webui"]["data_domains"]["backed_up_readable"]["documents_rag"]["database_count"], 1)

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
        self.assertTrue(any("data domains" in warning and "documents_rag" in warning for warning in plan["warnings"]))

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

    def test_floating_open_webui_tag_is_recorded_when_backup_has_exact_image_id(self) -> None:
        version_evidence = [
            {
                **open_webui_migration.image_reference_metadata("ghcr.io/open-webui/open-webui:main"),
                "container": "open-webui",
            }
        ]
        backed_metadata = [
            {
                "container": "open-webui",
                "image_evidence": {
                    "config_image": "ghcr.io/open-webui/open-webui:main",
                    "image_id": "sha256:a26effeb220e132482bf7e0560b3404843e7bc40d23051144e062960df8df6b0",
                    "repo_digests": [],
                },
            }
        ]

        self.assertEqual(
            open_webui_migration.version_compatibility_status(version_evidence, backed_metadata),
            "source-image-id-recorded-temporary-validation-required",
        )

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

    def test_write_plan_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            output = root / "plan.json"
            try:
                output.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(open_webui_migration.OpenWebUiMigrationError, "symlink"):
                open_webui_migration.write_plan({"format": "test"}, output)

            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")

    def test_status_and_web_build_use_latest_backup_root_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "old-open-webui" / "data" / "webui.db"
            self.create_webui_db(database_path)
            inventory_payload = self.inventory(root, database_path)
            backup_dir = self.make_backup(root, database_path.parent)
            backup_root = backup_dir.parent
            inventory_path = self.write_json(backup_root / "inventory-20260723.json", inventory_payload)
            restore_root = root / "restore-tests"

            status_before = open_webui_migration.status(backup_root, restore_root)
            result = open_webui_migration.build_and_write_plan(
                backup_root,
                restore_root,
                now=datetime(2026, 7, 23, 13, 30, tzinfo=UTC),
            )
            status_after = open_webui_migration.status(backup_root, restore_root)
            generated_mode = Path(result["path"]).stat().st_mode & 0o777

        self.assertTrue(status_before["ready_to_generate"])
        self.assertEqual(status_before["inputs"]["inventory"]["path"], str(inventory_path.resolve()))
        self.assertEqual(status_before["inputs"]["old_stack_backup"]["path"], str(backup_dir.resolve()))
        self.assertEqual(status_before["inputs"]["restore_target"]["path"], str((restore_root / "open-webui-migration").resolve()))
        self.assertEqual(result["name"], "open-webui-migration-plan-20260723-133000.json")
        self.assertEqual(result["summary"]["status"], "ready")
        self.assertEqual(generated_mode, 0o600)
        self.assertTrue(status_after["inputs"]["current_plan"]["available"])
        self.assertEqual(
            status_after["inputs"]["current_plan"]["recommended_strategy"],
            "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
        )


if __name__ == "__main__":
    unittest.main()
