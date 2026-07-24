from __future__ import annotations

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
from app import backup_migration_rollback as evidence  # noqa: E402
from app import rollback_rehearsal  # noqa: E402
import old_stack_backup  # noqa: E402


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

    def create_old_stack_backup(self, root: Path, output_root: Path | None = None) -> Path:
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
        return old_stack_backup.backup_old_stack(scope_path=scope_path, output_root=output_root or root / "backups", label="old-stack-unit")

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
            root / "open-webui-migration-plan.json",
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

    def test_build_evidence_rejects_stale_rollback_rehearsal_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["phases"][0]["operator_actions"].append("Confirm old-stack services still start on the staging network.")
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover_plan_sha256"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_and_write_evidence_auto_selects_latest_backup_root_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            backup_root = b1_backup.parent
            restore_root = restore_report.parents[1]
            old_stack = self.create_old_stack_backup(root, output_root=backup_root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(backup_root, old_stack)

            result = evidence.build_and_write_evidence(backup_root=backup_root, restore_root=restore_root)

            output = backup_root / "acceptance" / "backup-migration-rollback.json"
            self.assertEqual(result["status"], "ok")
            self.assertEqual(Path(result["output"]), output)
            self.assertTrue(output.is_file())
            self.assertEqual(result["inputs"]["b1_backup"]["path"], str(b1_backup.resolve()))
            self.assertEqual(result["inputs"]["restore_report"]["path"], str(restore_report.resolve()))
            self.assertEqual(result["inputs"]["inventory"]["path"], str(inventory_path.resolve()))
            self.assertEqual(result["inputs"]["old_stack_backup"]["path"], str(old_stack.resolve()))
            self.assertEqual(result["inputs"]["open_webui_plan"]["path"], str(open_webui_plan.resolve()))
            self.assertEqual(result["inputs"]["cutover_plan"]["path"], str(cutover_plan.resolve()))
            self.assertEqual(result["inputs"]["rollback_report"]["path"], str(rollback_report.resolve()))

    def test_status_reports_missing_inputs_without_host_path_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = evidence.status(root / "backups", root / "restore-tests")

        self.assertEqual(report["format"], "b1-ai-hub-backup-migration-rollback-evidence-status/v1")
        self.assertFalse(report["ready"])
        self.assertIn("b1_backup", report["inputs"])
        self.assertGreaterEqual(len(report["blockers"]), 1)


if __name__ == "__main__":
    unittest.main()
