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
        target_identity = {
            "expected_target_host": "ai.b1.germering",
            "expected_short_hostname": "ai",
            "observed_hostname": "ai",
            "observed_fqdn": "ai.b1.germering",
            "observed_platform_node": "ai",
            "hostname_matches_expected": True,
            "fqdn_matches_expected": True,
            "platform_node_matches_expected": True,
            "accepted": True,
            "operator_must_review_target_identity": False,
            "warnings": [],
        }
        inventory_path = self.write_json(
            root / "inventory.json",
            {
                "format": "b1-ai-hub-host-inventory/v1",
                "classification": {"containers": []},
                "host": {
                    "identity": {
                        "hostname": "ai",
                        "fqdn": "ai.b1.germering",
                        "platform_node": "ai",
                    },
                    "target_identity": target_identity,
                },
                "migration_readiness": {
                    "hardware_profile": {"accepted": True, "warnings": []},
                    "target_identity": target_identity,
                },
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
                "open_webui": {
                    "recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
                    "readable_database_count": 1,
                    "data_domains": {
                        "all_readable": {
                            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
                            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
                            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
                        },
                        "backed_up_readable": {
                            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
                            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
                            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
                        },
                        "content_rows_read": False,
                    },
                },
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
                    "systemd_services_to_restart_for_rollback": ["ollama.service"],
                    "docker_volumes_preserved": [],
                    "host_paths_preserved": [str(root / "old-stack" / "docker-compose.yaml")],
                },
                "dns_readiness": {
                    "all_hosts_resolve": True,
                    "all_hosts_share_gateway_address": True,
                    "operator_must_review_dns": False,
                    "reference_host": "ai.b1.germering",
                    "common_addresses": ["192.168.2.100"],
                    "missing_hosts": [],
                    "divergent_hosts": [],
                    "optional_missing_hosts": ["monitoring.ai.b1.germering"],
                    "optional_divergent_hosts": [],
                },
                "target_identity_readiness": {"available": True, **target_identity},
                "networking_readiness": {
                    "available": True,
                    "hostname_source": "system-hostname",
                    "network_property_source": "host-dhcp-client",
                    "b1_manages_host_networking": False,
                    "b1_static_ip_configures": False,
                    "non_loopback_address_count": 1,
                    "default_route_interfaces": ["eno1"],
                    "default_route_address_count": 1,
                    "default_route_count": 1,
                    "default_route_protocols": ["dhcp"],
                    "has_dhcp_default_route": True,
                    "dns_record_count": 7,
                    "operator_must_review_networking": False,
                    "warnings": [],
                },
                "hardware_readiness": {
                    "available": True,
                    "profile": "rtx3060-32gb-initial",
                    "accepted": True,
                    "largest_gpu_vram_mib": 12288,
                    "minimum_gpu_vram_mib": 12288,
                    "host_total_ram_mib": 32168,
                    "minimum_host_ram_mib": 32000,
                    "operator_must_review_hardware": False,
                    "warnings": [],
                },
                "gpu_runtime_readiness": {
                    "available": True,
                    "accepted": True,
                    "nvidia_smi_available": True,
                    "detected_gpu_count": 1,
                    "docker_nvidia_runtime_available": True,
                    "docker_runtimes": ["nvidia", "runc"],
                    "docker_default_runtime": "runc",
                    "nvidia_container_toolkit_available": True,
                    "nvidia_container_toolkit_returncode": 0,
                    "nvidia_container_toolkit_version": "NVIDIA Container Toolkit CLI version 1.17.8",
                    "operator_must_review_gpu_runtime": False,
                    "warnings": [],
                },
                "runtime_agent_socket_readiness": {
                    "available": True,
                    "path": "/var/run/docker.sock",
                    "exists": True,
                    "is_socket": True,
                    "uid": 0,
                    "gid": 998,
                    "mode_octal": "0660",
                    "group_readable": True,
                    "group_writable": True,
                    "configured_gid": "998",
                    "configured_gid_valid": True,
                    "configured_gid_matches": True,
                    "runtime_agent_group_access_ready": True,
                    "operator_must_review_runtime_agent_socket": False,
                    "warnings": [],
                },
                "open_webui_preservation": {
                    "plan_supplied": True,
                    "operator_must_review_open_webui": False,
                    "recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
                    "compatibility_status": "source-version-recorded-temporary-validation-required",
                    "requires_temporary_instance_validation": True,
                    "data_domains": {
                        "all_readable": {
                            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
                            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
                            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
                        },
                        "backed_up_readable": {
                            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
                            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
                            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
                        },
                        "content_rows_read": False,
                    },
                    "plan_warnings": [],
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
        self.assertEqual(len(payload["checks"]["b1_backup_verified"]["archive_sha256"]), 64)
        self.assertEqual(len(payload["checks"]["old_stack_backup_verified"]["archive_sha256"]), 64)
        self.assertEqual(len(payload["checks"]["rollback_rehearsed"]["cutover_plan_sha256"]), 64)
        self.assertEqual(len(payload["checks"]["rollback_rehearsed"]["rollback_actions_sha256"]), 64)
        self.assertEqual(payload["checks"]["rollback_rehearsed"]["operator_action_count"], 2)
        self.assertEqual(payload["checks"]["old_resources_preserved"]["rehearsal_resource_count"], 2)
        self.assertEqual(payload["checks"]["old_resources_preserved"]["resource_counts_by_type"]["systemd_services_to_restart_for_rollback"], 1)
        self.assertEqual(len(payload["checks"]["old_resources_preserved"]["resources_sha256"]), 64)
        self.assertEqual(
            payload["checks"]["old_resources_preserved"]["resources"]["systemd_services_to_restart_for_rollback"],
            ["ollama.service"],
        )
        self.assertEqual(
            payload["checks"]["cutover_plan_reviewed"]["dns_readiness"]["optional_missing_hosts"],
            ["monitoring.ai.b1.germering"],
        )
        self.assertTrue(payload["checks"]["old_stack_inventory_reviewed"]["target_identity"]["accepted"])
        self.assertTrue(payload["checks"]["cutover_plan_reviewed"]["target_identity_readiness"]["accepted"])
        self.assertTrue(payload["checks"]["cutover_plan_reviewed"]["networking_readiness"]["has_dhcp_default_route"])
        self.assertTrue(payload["checks"]["cutover_plan_reviewed"]["hardware_readiness"]["accepted"])
        self.assertTrue(payload["checks"]["cutover_plan_reviewed"]["gpu_runtime_readiness"]["accepted"])
        self.assertTrue(payload["checks"]["cutover_plan_reviewed"]["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"])
        self.assertFalse(payload["checks"]["cutover_plan_reviewed"]["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertEqual(
            payload["checks"]["open_webui_migration_plan_reviewed"]["data_domains"]["backed_up_readable"]["documents_rag"]["known_row_count"],
            2,
        )
        self.assertEqual(
            payload["checks"]["cutover_plan_reviewed"]["open_webui_preservation"]["data_domains"]["backed_up_readable"]["documents_rag"]["known_row_count"],
            2,
        )
        self.assertEqual([sample["label"] for sample in payload["samples"]], ["b1-backup", "restore-test", "old-stack-backup", "rollback-runbook"])

    def test_build_evidence_rejects_cutover_dns_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["dns_readiness"]["all_hosts_resolve"] = False
            payload["dns_readiness"]["operator_must_review_dns"] = True
            payload["dns_readiness"]["missing_hosts"] = ["voice.ai.b1.germering"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover DNS readiness is missing core host records"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_divergent_optional_monitoring_dns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["dns_readiness"]["operator_must_review_dns"] = True
            payload["dns_readiness"]["optional_missing_hosts"] = []
            payload["dns_readiness"]["optional_divergent_hosts"] = ["monitoring.ai.b1.germering"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover DNS readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_hardware_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["hardware_readiness"]["accepted"] = False
            payload["hardware_readiness"]["operator_must_review_hardware"] = True
            payload["hardware_readiness"]["warnings"] = ["largest detected GPU VRAM is below the initial profile"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover hardware readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_open_webui_preservation_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["open_webui_preservation"]["operator_must_review_open_webui"] = True
            payload["open_webui_preservation"]["plan_warnings"] = ["readable Open WebUI database was not preserved"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover Open WebUI preservation requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_networking_without_dhcp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["networking_readiness"]["has_dhcp_default_route"] = False
            payload["networking_readiness"]["operator_must_review_networking"] = True
            payload["networking_readiness"]["warnings"] = ["Inventory did not prove a DHCP-owned default route"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover host DHCP/networking readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_inventory_target_identity_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(inventory_path.read_text(encoding="utf-8"))
            payload["migration_readiness"]["target_identity"]["accepted"] = False
            payload["migration_readiness"]["target_identity"]["operator_must_review_target_identity"] = True
            payload["migration_readiness"]["target_identity"]["warnings"] = [
                "Inventory host identity does not match expected target ai.b1.germering"
            ]
            self.write_json(inventory_path, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "inventory target host identity requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_target_identity_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["target_identity_readiness"]["accepted"] = False
            payload["target_identity_readiness"]["operator_must_review_target_identity"] = True
            payload["target_identity_readiness"]["warnings"] = [
                "Inventory host identity does not match expected target ai.b1.germering"
            ]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover target host identity readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_gpu_runtime_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["gpu_runtime_readiness"]["accepted"] = False
            payload["gpu_runtime_readiness"]["operator_must_review_gpu_runtime"] = True
            payload["gpu_runtime_readiness"]["warnings"] = ["Docker does not report an nvidia runtime"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover GPU container runtime readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_cutover_runtime_agent_socket_requiring_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(cutover_plan.read_text(encoding="utf-8"))
            payload["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"] = False
            payload["runtime_agent_socket_readiness"]["operator_must_review_runtime_agent_socket"] = True
            payload["runtime_agent_socket_readiness"]["warnings"] = ["B1_DOCKER_GID=0 does not match Docker socket GID 998"]
            self.write_json(cutover_plan, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "cutover runtime-agent Docker socket readiness requires operator review"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

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

    def test_build_evidence_rejects_rollback_report_resource_set_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(rollback_report.read_text(encoding="utf-8"))
            preserved = payload["checks"]["old_resources_preserved"]
            preserved["resources"]["systemd_services_to_restart_for_rollback"] = []
            preserved["resources"]["host_paths_preserved"].append(str(root / "different-old-stack" / "docker-compose.yaml"))
            self.write_json(rollback_report, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "resources_sha256|resources do not match"):
                evidence.build_evidence(
                    b1_backup=b1_backup,
                    restore_report=restore_report,
                    inventory=inventory_path,
                    old_stack_backup_path=old_stack,
                    open_webui_plan=open_webui_plan,
                    cutover_plan=cutover_plan,
                    rollback_report=rollback_report,
                )

    def test_build_evidence_rejects_rollback_report_action_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_backup, restore_report = self.create_b1_backup_and_restore(root)
            old_stack = self.create_old_stack_backup(root)
            inventory_path, open_webui_plan, cutover_plan, rollback_report = self.create_plan_files(root, old_stack)
            payload = json.loads(rollback_report.read_text(encoding="utf-8"))
            payload["rollback"]["operator_actions"] = ["Do something else"]
            self.write_json(rollback_report, payload)

            with self.assertRaisesRegex(evidence.EvidenceError, "operator actions do not match"):
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
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_write_evidence_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            output = root / "backup-migration-rollback.json"
            try:
                output.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(evidence.EvidenceError, "symlink"):
                evidence.write_evidence(output, {"format": evidence.EVIDENCE_FORMAT})

            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")

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
