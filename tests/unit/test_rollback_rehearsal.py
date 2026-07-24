from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"

spec = importlib.util.spec_from_file_location("b1_rollback_rehearsal", SCRIPTS / "rollback_rehearsal.py")
rollback_rehearsal = importlib.util.module_from_spec(spec)
sys.modules["b1_rollback_rehearsal"] = rollback_rehearsal
assert spec.loader is not None
spec.loader.exec_module(rollback_rehearsal)


class RollbackRehearsalTests(unittest.TestCase):
    def write_cutover_plan(self, root: Path, *, warnings: list[str] | None = None, resources: dict | None = None) -> Path:
        old_resources = resources or {
            "containers_to_restart_for_rollback": ["old-open-webui"],
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
        self.assertEqual(report["checks"]["old_resources_preserved"]["resource_count"], 3)
        self.assertEqual(report["rollback"]["commands"], ["docker compose -p old-ai up -d old-open-webui"])

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


if __name__ == "__main__":
    unittest.main()
