from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(ROOT / "services" / "control-plane"))
sys.path.insert(0, str(SCRIPTS))

from app import backup_restore  # noqa: E402
import old_stack_backup  # noqa: E402


spec = importlib.util.spec_from_file_location("b1_backup_migration_rollback_evidence", SCRIPTS / "backup_migration_rollback_evidence.py")
evidence = importlib.util.module_from_spec(spec)
sys.modules["b1_backup_migration_rollback_evidence"] = evidence
assert spec.loader is not None
spec.loader.exec_module(evidence)

rollback_spec = importlib.util.spec_from_file_location("b1_rollback_rehearsal", SCRIPTS / "rollback_rehearsal.py")
rollback_rehearsal = importlib.util.module_from_spec(rollback_spec)
sys.modules["b1_rollback_rehearsal"] = rollback_rehearsal
assert rollback_spec.loader is not None
rollback_spec.loader.exec_module(rollback_rehearsal)


class BackupMigrationRollbackEvidenceTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def create_b1_backup_and_restore(self, root: Path) -> tuple[Path, Path]:
        data_root = root / "b1"
        (data_root / "data" / "open-webui").mkdir(parents=True)
        (data_root / "data" / "open-webui" / "webui.db").write_text("sqlite", encoding="utf-8")
        (data_root / "secrets").mkdir()
        (data_root / "secrets" / "master_encryption_key").write_text("k" * 64, encoding="utf-8")
        dump_path = data_root / "data" / "control-plane" / "postgres-logical-export.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text("{}", encoding="utf-8")
        backup_restore.create_backup(
            data_root,
            label="b1-unit",
            postgres_dump_path=dump_path,
            postgres_dump_format=backup_restore.LOGICAL_POSTGRES_DUMP_FORMAT,
        )
        restore = backup_restore.restore_backup_to_alternate(data_root / "backups", "b1-unit", data_root / "restore-tests")
        return data_root / "backups" / "b1-unit", Path(restore["target"]) / "restore-report.json"

    def create_old_stack_backup(self, root: Path) -> Path:
        old_compose = root / "old-stack" / "docker-compose.yaml"
        old_compose.parent.mkdir(parents=True)
        old_compose.write_text("services: {}\n", encoding="utf-8")
        scope = {
            "format": "b1-ai-hub-old-stack-backup-scope/v1",
            "operator_reviewed": True,
            "reviewed_by": "operator",
            "review_notes": "old AI stack compose file only",
            "include_paths": [{"path": str(old_compose), "reason": "old compose rollback path"}],
            "include_docker_volumes": [],
            "include_containers": [],
        }
        scope_path = self.write_json(root / "old-stack-scope.json", scope)
        return old_stack_backup.backup_old_stack(scope_path=scope_path, output_root=root / "backups", label="old-stack-unit")

    def create_plan_files(self, root: Path, old_stack_backup_dir: Path) -> tuple[Path, Path, Path, Path]:
        inventory_path = self.write_json(
            root / "inventory.json",
            {
                "format": "b1-ai-hub-host-inventory/v1",
                "classification": {"containers": []},
                "migration_readiness": {"hardware_profile": {"accepted": True, "warnings": []}},
            },
        )
        open_webui_plan = self.write_json(
            root / "open-webui-plan.json",
            {
                "format": "b1-ai-hub-open-webui-migration-plan/v1",
                "inputs": {
                    "inventory": str(inventory_path.resolve()),
                    "old_stack_backup": str(old_stack_backup_dir.resolve()),
                    "old_stack_backup_verification": {"status": "verified", "backup": str(old_stack_backup_dir.resolve())},
                },
                "open_webui": {"recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import"},
                "warnings": [],
            },
        )
        cutover_plan = self.write_json(
            root / "cutover-plan.json",
            {
                "format": "b1-ai-hub-cutover-plan/v1",
                "inputs": {
                    "inventory": str(inventory_path.resolve()),
                    "scope": str((root / "old-stack-scope.json").resolve()),
                    "old_stack_backup": str(old_stack_backup_dir.resolve()),
                    "old_stack_backup_verification": {"status": "verified", "backup": str(old_stack_backup_dir.resolve())},
                    "open_webui_migration_plan": str(open_webui_plan.resolve()),
                },
                "safety": {
                    "read_only_plan": True,
                    "stops_nothing_automatically": True,
                    "deletes_nothing": True,
                    "old_stack_deletion_allowed": False,
                },
                "old_stack_scope": {
                    "reviewed_by": "operator",
                    "containers_to_restart_for_rollback": [],
                    "docker_volumes_preserved": [],
                    "host_paths_preserved": [str(root / "old-stack" / "docker-compose.yaml")],
                },
                "phases": [
                    {
                        "name": "rollback",
                        "commands": [],
                        "operator_actions": [
                            "Revert DNS, reverse-proxy routes, or port bindings to the old stack.",
                            "Leave B1 data and old-stack backups intact for diagnosis.",
                        ],
                    },
                ],
                "warnings": [],
            },
        )
        rollback_report = rollback_rehearsal.write_report(
            root / "rollback-rehearsal.json",
            rollback_rehearsal.build_report(
                cutover_plan_path=cutover_plan,
                rehearsed_by="operator",
                rollback_commands_tested=True,
                old_resources_preserved=True,
            ),
        )
        return inventory_path, open_webui_plan, cutover_plan, rollback_report

    def test_build_evidence_verifies_backup_restore_migration_and_rollback_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)

            payload = evidence.build_evidence(
                b1_backup=b1_backup,
                restore_report=restore_report,
                inventory=inventory_path,
                old_stack_backup_path=old_stack,
                open_webui_plan=open_webui_plan,
                cutover_plan=cutover_plan,
                rollback_report=rollback_report,
            )

        self.assertEqual(payload["format"], "b1-ai-hub-backup-migration-rollback-acceptance/v1")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["required_checks"], list(evidence.REQUIRED_CHECKS))
        self.assertTrue(all(payload["checks"][name]["status"] == "ok" for name in evidence.REQUIRED_CHECKS))
        self.assertEqual([sample["label"] for sample in payload["samples"]], ["b1-backup", "restore-test", "old-stack-backup", "rollback-runbook"])

    def test_build_evidence_rejects_rollback_report_without_preservation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(rollback_report.read_text(encoding="utf-8"))
            payload["checks"].pop("old_resources_preserved")
            self.write_json(rollback_report, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "old_resources_preserved"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )


if __name__ == "__main__":
    unittest.main()
