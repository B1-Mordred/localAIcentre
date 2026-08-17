from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import rollback_rehearsal  # noqa: E402


class RollbackRehearsalTests(unittest.TestCase):
    def write_cutover_plan(self, root: Path, *, warnings: list[str] | None = None, resources: dict | None = None) -> Path:
        old_resources = resources or {
            "containers_to_restart_for_rollback": ["old-open-webui"],
            "systemd_services_to_restart_for_rollback": ["ollama.service"],
            "docker_volumes_preserved": ["open-webui-data"],
            "host_paths_preserved": [str(root / "old-stack" / "docker-compose.yaml")],
        }
        plan = {
            "format": "b1-ai-hub-cutover-plan/v1",
            "warnings": warnings or [],
            "safety": {
                "read_only_plan": True,
                "stops_nothing_automatically": True,
                "deletes_nothing": True,
                "old_stack_deletion_allowed": False,
            },
            "old_stack_scope": old_resources,
            "phases": [
                {
                    "name": "rollback",
                    "commands": ["docker compose -p old-ai up -d old-open-webui"],
                    "operator_actions": [
                        "Revert DNS, reverse-proxy routes, or port bindings to the old stack.",
                        "Restart only the old-stack containers explicitly scoped in this plan.",
                    ],
                },
            ],
        }
        path = root / "cutover-plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def test_build_report_records_rehearsal_and_preserved_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cutover_plan = self.write_cutover_plan(root)

            report = rollback_rehearsal.build_report(
                cutover_plan_path=cutover_plan,
                rehearsed_by="operator",
                rollback_commands_tested=True,
                old_resources_preserved=True,
            )

        self.assertEqual(report["format"], "b1-ai-hub-rollback-rehearsal/v1")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["rehearsed_by"], "operator")
        self.assertEqual(report["checks"]["rollback_commands_tested"]["status"], "ok")
        self.assertEqual(report["checks"]["rollback_commands_tested"]["command_count"], 1)
        self.assertEqual(len(report["checks"]["rollback_commands_tested"]["rollback_actions_sha256"]), 64)
        self.assertEqual(report["checks"]["old_resources_preserved"]["resource_count"], 4)
        self.assertEqual(report["checks"]["old_resources_preserved"]["resource_counts_by_type"]["systemd_services_to_restart_for_rollback"], 1)
        self.assertEqual(len(report["checks"]["old_resources_preserved"]["resources_sha256"]), 64)
        self.assertEqual(report["checks"]["old_resources_preserved"]["resources"]["systemd_services_to_restart_for_rollback"], ["ollama.service"])
        self.assertEqual(report["rollback"]["commands"], ["docker compose -p old-ai up -d old-open-webui"])
        self.assertEqual(len(report["rollback"]["actions_sha256"]), 64)

    def test_build_report_requires_explicit_operator_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cutover_plan = self.write_cutover_plan(Path(tmp))

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "rollback_commands_tested"):
                rollback_rehearsal.build_report(
                    cutover_plan_path=cutover_plan,
                    rehearsed_by="operator",
                    rollback_commands_tested=False,
                    old_resources_preserved=True,
                )

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "old_resources_preserved"):
                rollback_rehearsal.build_report(
                    cutover_plan_path=cutover_plan,
                    rehearsed_by="operator",
                    rollback_commands_tested=True,
                    old_resources_preserved=False,
                )

    def test_build_report_rejects_cutover_plan_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cutover_plan = self.write_cutover_plan(Path(tmp), warnings=["DNS does not resolve yet"])

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "unresolved warnings"):
                rollback_rehearsal.build_report(
                    cutover_plan_path=cutover_plan,
                    rehearsed_by="operator",
                    rollback_commands_tested=True,
                    old_resources_preserved=True,
                )

    def test_build_report_rejects_cutover_plan_without_preserved_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cutover_plan = self.write_cutover_plan(
                Path(tmp),
                resources={
                    "containers_to_restart_for_rollback": [],
                    "systemd_services_to_restart_for_rollback": [],
                    "docker_volumes_preserved": [],
                    "host_paths_preserved": [],
                },
            )

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "no preserved rollback resources"):
                rollback_rehearsal.build_report(
                    cutover_plan_path=cutover_plan,
                    rehearsed_by="operator",
                    rollback_commands_tested=True,
                    old_resources_preserved=True,
                )

    def test_build_and_write_report_selects_latest_generated_cutover_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = self.write_cutover_plan(root)
            older.rename(root / "cutover-plan-20260723-120000.json")
            latest = self.write_cutover_plan(root)
            latest.rename(root / "cutover-plan-20260724-120000.json")

            result = rollback_rehearsal.build_and_write_report(
                backup_root=root,
                cutover_plan_name=None,
                rehearsed_by="operator",
                rollback_commands_tested=True,
                old_resources_preserved=True,
            )

            report_path = root / "rollback-rehearsal.json"
            self.assertEqual(result["status"], "ok")
            self.assertTrue(report_path.is_file())
            self.assertEqual(result["report"]["cutover_plan_name"], "cutover-plan-20260724-120000.json")
            status = rollback_rehearsal.status(root)
            self.assertTrue(status["cutover_plan"]["available"])
            self.assertEqual(status["cutover_plan"]["resource_counts_by_type"]["systemd_services_to_restart_for_rollback"], 1)
            self.assertEqual(len(status["cutover_plan"]["resources_sha256"]), 64)
            self.assertEqual(len(status["cutover_plan"]["rollback_actions_sha256"]), 64)
            self.assertTrue(status["report"]["available"])
            self.assertEqual(status["report"]["cutover_plan_name"], "cutover-plan-20260724-120000.json")
            self.assertEqual(len(status["report"]["resources_sha256"]), 64)

    def test_write_report_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            output = root / "rollback-rehearsal.json"
            try:
                output.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "symlink"):
                rollback_rehearsal.write_report(output, {"format": rollback_rehearsal.ROLLBACK_REHEARSAL_FORMAT})

            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")

    def test_resolve_cutover_plan_rejects_names_outside_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_cutover_plan(root)

            with self.assertRaisesRegex(rollback_rehearsal.RollbackRehearsalError, "generated cutover-plan"):
                rollback_rehearsal.resolve_cutover_plan(root, "../cutover-plan.json")


if __name__ == "__main__":
    unittest.main()
