from __future__ import annotations

import importlib.util
import json
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

CUTOVER_PATH = SCRIPTS / "cutover.py"
cutover_spec = importlib.util.spec_from_file_location("b1_cutover", CUTOVER_PATH)
cutover = importlib.util.module_from_spec(cutover_spec)
sys.modules["b1_cutover"] = cutover
assert cutover_spec.loader is not None
cutover_spec.loader.exec_module(cutover)


class CutoverPlanTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def inventory(
        self,
        container: str = "old-open-webui",
        classification: str = "candidate-old-ai-stack-review-required",
        listening_tcp: list[dict[str, Any]] | None = None,
        dns_records: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        records = dns_records if dns_records is not None else {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
        payload = {
            "format": "b1-ai-hub-host-inventory/v1",
            "created_at": "2026-07-23T10:00:00+00:00",
            "classification": {
                "containers": [
                    {"container": container, "classification": classification, "confidence": "medium"},
                    {"container": "b1-ai-hub-control-plane-1", "classification": "b1-ai-hub-current-preserve", "confidence": "high"},
                ]
            },
            "host": {
                "dns": {
                    "intended_hosts": list(cutover.PRODUCTION_HOSTS),
                    "records": records,
                }
            },
        }
        if listening_tcp is not None:
            payload["host"]["listening_tcp"] = listening_tcp
        return payload

    def scope(self, root: Path, container: str = "old-open-webui") -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-old-stack-backup-scope/v1",
            "operator_reviewed": True,
            "reviewed_by": "operator",
            "review_notes": "old Open WebUI container only",
            "include_paths": [{"path": str(root / "old-compose.yaml"), "reason": "old compose file"}],
            "include_docker_volumes": [],
            "include_containers": [{"name": container, "reason": "old AI container metadata and rollback target"}],
        }

    def fake_runner(self, container: str = "old-open-webui"):
        def runner(command: list[str]) -> dict[str, Any]:
            if command[:2] == ["docker", "inspect"] and command[2] == container:
                return {
                    "command": command,
                    "stdout": json.dumps([{"Name": f"/{container}", "Image": "ghcr.io/open-webui/open-webui:v0.6.18"}]),
                    "stderr": "",
                    "returncode": 0,
                }
            raise AssertionError(f"unexpected command: {command}")

        return runner

    def make_verified_backup(self, root: Path, scope_path: Path, container: str = "old-open-webui") -> Path:
        return old_stack_backup.backup_old_stack(
            scope_path=scope_path,
            output_root=root / "backups",
            label="old-stack-unit",
            command_runner=self.fake_runner(container),
            now=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
        )

    def open_webui_plan(
        self,
        inventory_path: Path,
        backup_dir: Path,
        *,
        strategy: str = "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-open-webui-migration-plan/v1",
            "created_at": "2026-07-23T10:30:00+00:00",
            "inputs": {
                "inventory": str(inventory_path.resolve()),
                "old_stack_backup": str(backup_dir.resolve()),
                "old_stack_backup_verification": {"status": "verified", "backup": str(backup_dir.resolve())},
            },
            "safety": {
                "read_only_plan": True,
                "does_not_modify_old_stack": True,
                "does_not_import_automatically": True,
                "does_not_read_database_rows": True,
                "old_stack_deletion_allowed": False,
                "operator_must_review_before_execution": True,
            },
            "open_webui": {
                "readable_database_count": 1,
                "backed_up_database_candidate_count": 1,
                "backup_database_artifacts": [{"archive_path": "docker-volumes/open-webui_data/webui.db"}],
                "recommended_strategy": strategy,
                "version_evidence": {
                    "source_containers": [
                        {
                            "container": "old-open-webui",
                            "image": "ghcr.io/open-webui/open-webui:v0.6.18",
                            "tag": "v0.6.18",
                            "version_hint": "v0.6.18",
                            "floating_or_missing_tag": False,
                        }
                    ],
                    "backed_up_container_metadata": [{"container": "old-open-webui", "archive_path": "docker-inspect/containers/old-open-webui.json"}],
                    "compatibility_status": "source-version-recorded-temporary-validation-required",
                    "direct_database_reuse_approved_by_plan": False,
                    "requires_supported_open_webui_migration_path": True,
                    "requires_temporary_instance_validation": True,
                },
            },
            "warnings": warnings or [],
        }

    def write_open_webui_plan(self, path: Path, inventory_path: Path, backup_dir: Path, **kwargs: Any) -> Path:
        return self.write_json(path, self.open_webui_plan(inventory_path, backup_dir, **kwargs))

    def test_build_plan_verifies_backup_and_renders_cutover_and_rollback_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services:\n  open-webui: {}\n", encoding="utf-8")
            inventory_path = self.write_json(root / "inventory.json", self.inventory())
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)
            open_webui_plan_path = self.write_open_webui_plan(root / "open-webui-plan.json", inventory_path, backup_dir)

            plan = cutover.build_plan(
                inventory_path=inventory_path,
                scope_path=scope_path,
                backup_dir=backup_dir,
                b1_data_root="/srv/b1-ai-hub",
                project_name="b1-ai-hub",
                temporary_http_port=18080,
                temporary_https_port=18443,
                production_http_port=80,
                production_https_port=443,
                open_webui_plan_path=open_webui_plan_path,
                now=datetime(2026, 7, 23, 11, 0, tzinfo=UTC),
            )

        self.assertEqual(plan["format"], "b1-ai-hub-cutover-plan/v1")
        self.assertTrue(plan["safety"]["stops_nothing_automatically"])
        self.assertFalse(plan["safety"]["old_stack_deletion_allowed"])
        self.assertEqual(plan["inputs"]["old_stack_backup_verification"]["status"], "verified")
        self.assertEqual(plan["old_stack_scope"]["containers_to_stop_during_cutover"], ["old-open-webui"])
        cutover_phase = next(phase for phase in plan["phases"] if phase["name"] == "cutover-window")
        rollback_phase = next(phase for phase in plan["phases"] if phase["name"] == "rollback")
        self.assertEqual(cutover_phase["commands"][0]["argv"], ["docker", "stop", "old-open-webui"])
        self.assertEqual(rollback_phase["commands"][0]["argv"], ["docker", "start", "old-open-webui"])
        self.assertIn("B1_HTTPS_PORT=18443", next(phase for phase in plan["phases"] if phase["name"] == "temporary-b1-start")["commands"][1]["shell"])
        self.assertTrue(plan["port_readiness"]["temporary_ports_clear"])
        self.assertTrue(plan["dns_readiness"]["all_hosts_resolve"])
        self.assertEqual(plan["dns_readiness"]["common_addresses"], ["192.168.2.100"])
        self.assertEqual(plan["inputs"]["open_webui_migration_plan"], str(open_webui_plan_path.resolve()))
        self.assertTrue(plan["open_webui_preservation"]["plan_supplied"])
        self.assertEqual(plan["open_webui_preservation"]["recommended_strategy"], "preserve-backed-up-sqlite-and-test-supported-open-webui-import")
        self.assertEqual(plan["open_webui_preservation"]["compatibility_status"], "source-version-recorded-temporary-validation-required")
        self.assertEqual(plan["open_webui_preservation"]["source_version_evidence_count"], 1)
        self.assertEqual(plan["open_webui_preservation"]["backed_up_container_metadata_count"], 1)
        self.assertFalse(plan["open_webui_preservation"]["direct_database_reuse_approved_by_plan"])
        self.assertTrue(plan["open_webui_preservation"]["requires_temporary_instance_validation"])
        self.assertFalse(plan["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertEqual(plan["warnings"], [])

    def test_build_plan_warns_when_open_webui_plan_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(root / "inventory.json", self.inventory())
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)

            plan = cutover.build_plan(
                inventory_path=inventory_path,
                scope_path=scope_path,
                backup_dir=backup_dir,
                b1_data_root="/srv/b1-ai-hub",
                project_name="b1-ai-hub",
                temporary_http_port=18080,
                temporary_https_port=18443,
                production_http_port=80,
                production_https_port=443,
            )

        self.assertFalse(plan["open_webui_preservation"]["plan_supplied"])
        self.assertTrue(plan["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertTrue(any("Open WebUI preservation plan was not supplied" in warning for warning in plan["warnings"]))

    def test_build_plan_refuses_mismatched_open_webui_plan_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(root / "inventory.json", self.inventory())
            other_inventory_path = self.write_json(root / "other-inventory.json", self.inventory())
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)
            open_webui_plan_path = self.write_open_webui_plan(root / "open-webui-plan.json", other_inventory_path, backup_dir)

            with self.assertRaisesRegex(cutover.CutoverPlanError, "different inventory"):
                cutover.build_plan(
                    inventory_path=inventory_path,
                    scope_path=scope_path,
                    backup_dir=backup_dir,
                    b1_data_root="/srv/b1-ai-hub",
                    project_name="b1-ai-hub",
                    temporary_http_port=18080,
                    temporary_https_port=18443,
                    production_http_port=80,
                    production_https_port=443,
                    open_webui_plan_path=open_webui_plan_path,
                )

    def test_build_plan_refuses_occupied_temporary_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(
                    listening_tcp=[
                        {"local_address": "0.0.0.0", "port": 18443, "process": "users:((\"caddy\",pid=10,fd=3))"}
                    ]
                ),
            )
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)

            with self.assertRaisesRegex(cutover.CutoverPlanError, "temporary B1 staging port is already in use"):
                cutover.build_plan(
                    inventory_path=inventory_path,
                    scope_path=scope_path,
                    backup_dir=backup_dir,
                    b1_data_root="/srv/b1-ai-hub",
                    project_name="b1-ai-hub",
                    temporary_http_port=18080,
                    temporary_https_port=18443,
                    production_http_port=80,
                    production_https_port=443,
                )

    def test_build_plan_warns_on_occupied_production_and_legacy_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(
                    listening_tcp=[
                        {"local_address": "0.0.0.0", "port": 80, "process": "users:((\"nginx\",pid=1,fd=3))"},
                        {"local_address": "0.0.0.0", "port": 443, "process": "users:((\"caddy\",pid=2,fd=3))"},
                        {"local_address": "127.0.0.1", "port": 8188, "process": "users:((\"python\",pid=3,fd=3))"},
                    ]
                ),
            )
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)

            plan = cutover.build_plan(
                inventory_path=inventory_path,
                scope_path=scope_path,
                backup_dir=backup_dir,
                b1_data_root="/srv/b1-ai-hub",
                project_name="b1-ai-hub",
                temporary_http_port=18080,
                temporary_https_port=18443,
                production_http_port=80,
                production_https_port=443,
            )

        self.assertTrue(any("production_http port 80 is already listening" in warning for warning in plan["warnings"]))
        self.assertTrue(any("production_https port 443 is already listening" in warning for warning in plan["warnings"]))
        self.assertTrue(any("legacy ComfyUI port 8188 is already listening" in warning for warning in plan["warnings"]))
        self.assertTrue(plan["port_readiness"]["operator_must_resolve_production_conflicts"])
        self.assertTrue(plan["port_readiness"]["operator_must_review_legacy_comfy_conflict"])
        self.assertEqual(plan["port_readiness"]["listeners"]["temporary_http"], [])
        self.assertEqual(plan["port_readiness"]["listeners"]["production_http"], ['0.0.0.0:80 users:(("nginx",pid=1,fd=3))'])

    def test_build_plan_warns_on_missing_dns_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            records = {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
            records.pop("voice.ai.b1.germering")
            inventory_path = self.write_json(root / "inventory.json", self.inventory(dns_records=records))
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)

            plan = cutover.build_plan(
                inventory_path=inventory_path,
                scope_path=scope_path,
                backup_dir=backup_dir,
                b1_data_root="/srv/b1-ai-hub",
                project_name="b1-ai-hub",
                temporary_http_port=18080,
                temporary_https_port=18443,
                production_http_port=80,
                production_https_port=443,
            )

        self.assertFalse(plan["dns_readiness"]["all_hosts_resolve"])
        self.assertEqual(plan["dns_readiness"]["missing_hosts"], ["voice.ai.b1.germering"])
        self.assertTrue(plan["dns_readiness"]["operator_must_review_dns"])
        self.assertTrue(any("DNS inventory did not resolve every B1 virtual host" in warning for warning in plan["warnings"]))

    def test_build_plan_warns_on_divergent_dns_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            records = {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
            records["api.ai.b1.germering"] = ["192.168.2.101"]
            inventory_path = self.write_json(root / "inventory.json", self.inventory(dns_records=records))
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            backup_dir = self.make_verified_backup(root, scope_path)

            plan = cutover.build_plan(
                inventory_path=inventory_path,
                scope_path=scope_path,
                backup_dir=backup_dir,
                b1_data_root="/srv/b1-ai-hub",
                project_name="b1-ai-hub",
                temporary_http_port=18080,
                temporary_https_port=18443,
                production_http_port=80,
                production_https_port=443,
            )

        self.assertTrue(plan["dns_readiness"]["all_hosts_resolve"])
        self.assertFalse(plan["dns_readiness"]["all_hosts_share_gateway_address"])
        self.assertEqual(plan["dns_readiness"]["divergent_hosts"], ["api.ai.b1.germering"])
        self.assertTrue(any("do not share a common gateway address" in warning for warning in plan["warnings"]))
        self.assertTrue(any("DNS inventory records differ from ai.b1.germering" in warning for warning in plan["warnings"]))

    def test_build_plan_refuses_containers_classified_as_unrelated_or_current_b1(self) -> None:
        for container, classification in (
            ("hermes-bot", "preserve-unrelated"),
            ("b1-ai-hub-control-plane-1", "b1-ai-hub-current-preserve"),
        ):
            with self.subTest(classification=classification):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
                    inventory_path = self.write_json(root / "inventory.json", self.inventory(container, classification))
                    scope_path = self.write_json(root / "scope.json", self.scope(root, container))
                    backup_dir = self.make_verified_backup(root, scope_path, container)

                    with self.assertRaisesRegex(cutover.CutoverPlanError, classification):
                        cutover.build_plan(
                            inventory_path=inventory_path,
                            scope_path=scope_path,
                            backup_dir=backup_dir,
                            b1_data_root="/srv/b1-ai-hub",
                            project_name="b1-ai-hub",
                            temporary_http_port=18080,
                            temporary_https_port=18443,
                            production_http_port=80,
                            production_https_port=443,
                        )

    def test_write_plan_uses_private_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = cutover.write_plan({"format": "test"}, root / "cutover-plan.json")

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["format"], "test")


if __name__ == "__main__":
    unittest.main()
