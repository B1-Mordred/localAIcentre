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
        networking: dict[str, Any] | None = None,
        target_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = dns_records if dns_records is not None else {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
        target_identity_payload = target_identity if target_identity is not None else {
            "hostname_authority": "b1-appliance-config",
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
        networking_payload = networking if networking is not None else {
            "hostname_authority": "b1-appliance-config",
            "hostname_source": "system-hostname",
            "network_property_source": "host-dhcp-client",
            "b1_manages_host_networking": False,
            "b1_static_ip_configures": False,
            "expected_operator_networking": "B1-defined system hostname plus host-managed DHCP lease/reservation and LAN DNS records",
            "non_loopback_address_count": 1,
            "default_route_interfaces": ["eno1"],
            "default_route_address_count": 1,
            "default_route_count": 1,
            "default_route_protocols": ["dhcp"],
            "has_dhcp_default_route": True,
            "dns_record_count": sum(len(addresses) for addresses in records.values()),
            "operator_must_review_networking": False,
            "warnings": [],
        }
        payload = {
            "format": "b1-ai-hub-host-inventory/v1",
            "created_at": "2026-07-23T10:00:00+00:00",
            "migration_readiness": {
                "hardware_profile": {
                    "profile": "rtx3060-32gb-initial",
                    "accepted": True,
                    "minimum_gpu_vram_mib": 12288,
                    "minimum_host_ram_mib": 32000,
                    "largest_gpu_vram_mib": 12288,
                    "host_total_ram_mib": 32168,
                    "warnings": [],
                },
                "gpu_container_runtime": {
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
                "runtime_agent_docker_socket": {
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
                    "warnings": [],
                },
                "networking": networking_payload,
                "target_identity": target_identity_payload,
            },
            "classification": {
                "containers": [
                    {"container": container, "classification": classification, "confidence": "medium"},
                    {"container": "b1-ai-hub-control-plane-1", "classification": "b1-ai-hub-current-preserve", "confidence": "high"},
                ],
                "systemd_services": [
                    {"service": "ollama.service", "classification": "candidate-old-ai-stack-review-required", "confidence": "medium"},
                    {"service": "hermes.service", "classification": "preserve-unrelated", "confidence": "high"},
                ],
            },
            "host": {
                "identity": {
                    "hostname": target_identity_payload.get("observed_hostname"),
                    "fqdn": target_identity_payload.get("observed_fqdn"),
                    "platform_node": target_identity_payload.get("observed_platform_node"),
                },
                "dns": {
                    "intended_hosts": list(cutover.PRODUCTION_HOSTS) + list(cutover.OPTIONAL_PRODUCTION_HOSTS),
                    "core_hosts": list(cutover.PRODUCTION_HOSTS),
                    "optional_hosts": list(cutover.OPTIONAL_PRODUCTION_HOSTS),
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
            "preserve_paths": [{"path": str(root / "ollama-models"), "reason": "preserve redownloadable old model cache without archiving"}],
            "include_docker_volumes": [],
            "include_containers": [{"name": container, "reason": "old AI container metadata and rollback target"}],
            "include_systemd_services": [{"name": "ollama.service", "reason": "old Ollama daemon and rollback target"}],
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
            if command[:2] == ["systemctl", "show"] and command[2] == "ollama.service":
                return {
                    "command": command,
                    "stdout": "\n".join(
                        [
                            "Id=ollama.service",
                            "Names=ollama.service",
                            "Description=Ollama Service",
                            "LoadState=loaded",
                            "ActiveState=active",
                            "SubState=running",
                            "FragmentPath=/etc/systemd/system/ollama.service",
                            "DropInPaths=",
                            "ExecStart={ argv[]=/usr/local/bin/ollama serve ; }",
                            "User=ollama",
                            "Group=ollama",
                            "",
                        ]
                    ),
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

    def network_dhcp_plan(self, *, confirmed: bool = True) -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-network-dhcp-plan/v1",
            "status": "ready" if confirmed else "operator-review-required",
            "ready_to_apply": confirmed,
            "reservation_confirmed": confirmed,
            "b1_static_ip_configures": False,
            "safety": {
                "read_only": True,
                "host_networking_changed": False,
                "b1_static_ip_configures": False,
            },
            "dhcp_reserved_appliance_addresses": [
                {"address": "192.168.2.100", "purpose": "b1-ai-hub-gateway"},
            ],
            "host_infrastructure_static_addresses": [
                {"address": "192.168.2.2", "cidr": "192.168.2.2/24", "purpose": "technitium-dhcp-dns"},
            ],
            "blockers": [] if confirmed else ["operator has not confirmed matching DHCP reservations in the LAN DHCP server"],
            "warnings": [],
        }

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
        self.assertEqual(plan["old_stack_scope"]["systemd_services_to_stop_during_cutover"], ["ollama.service"])
        self.assertEqual(
            plan["old_stack_scope"]["host_paths_preserved"],
            [str(root / "old-compose.yaml"), str(root / "ollama-models")],
        )
        cutover_phase = next(phase for phase in plan["phases"] if phase["name"] == "cutover-window")
        rollback_phase = next(phase for phase in plan["phases"] if phase["name"] == "rollback")
        self.assertEqual(cutover_phase["commands"][0]["argv"], ["docker", "stop", "old-open-webui"])
        self.assertEqual(cutover_phase["commands"][1]["argv"], ["systemctl", "stop", "ollama.service"])
        self.assertEqual(rollback_phase["commands"][0]["argv"], ["systemctl", "start", "ollama.service"])
        self.assertEqual(rollback_phase["commands"][1]["argv"], ["docker", "start", "old-open-webui"])
        self.assertIn("B1_HTTPS_PORT=18443", next(phase for phase in plan["phases"] if phase["name"] == "temporary-b1-start")["commands"][1]["shell"])
        self.assertTrue(plan["port_readiness"]["temporary_ports_clear"])
        self.assertTrue(plan["dns_readiness"]["all_hosts_resolve"])
        self.assertEqual(plan["dns_readiness"]["common_addresses"], ["192.168.2.100"])
        self.assertEqual(plan["dns_readiness"]["optional_missing_hosts"], ["monitoring.ai.b1.germering"])
        self.assertEqual(plan["b1_ai_hub"]["optional_hosts"], ["monitoring.ai.b1.germering"])
        self.assertEqual(plan["b1_ai_hub"]["expected_target_host"], "ai.b1.germering")
        self.assertEqual(plan["target_identity_readiness"]["hostname_authority"], "b1-appliance-config")
        self.assertTrue(plan["target_identity_readiness"]["accepted"])
        self.assertTrue(plan["target_identity_readiness"]["fqdn_matches_expected"])
        self.assertFalse(plan["target_identity_readiness"]["operator_must_review_target_identity"])
        self.assertEqual(plan["networking_readiness"]["hostname_authority"], "b1-appliance-config")
        self.assertEqual(plan["networking_readiness"]["hostname_source"], "system-hostname")
        self.assertEqual(plan["networking_readiness"]["network_property_source"], "host-dhcp-client")
        self.assertFalse(plan["networking_readiness"]["b1_static_ip_configures"])
        self.assertEqual(plan["networking_readiness"]["default_route_address_count"], 1)
        self.assertTrue(plan["networking_readiness"]["has_dhcp_default_route"])
        self.assertFalse(plan["networking_readiness"]["operator_must_review_networking"])
        self.assertEqual(plan["inputs"]["open_webui_migration_plan"], str(open_webui_plan_path.resolve()))
        self.assertTrue(plan["open_webui_preservation"]["plan_supplied"])
        self.assertEqual(plan["open_webui_preservation"]["recommended_strategy"], "preserve-backed-up-sqlite-and-test-supported-open-webui-import")
        self.assertEqual(plan["open_webui_preservation"]["compatibility_status"], "source-version-recorded-temporary-validation-required")
        self.assertEqual(plan["open_webui_preservation"]["data_domains"]["backed_up_readable"]["documents_rag"]["known_row_count"], 2)
        self.assertFalse(plan["open_webui_preservation"]["data_domains"]["content_rows_read"])
        self.assertEqual(plan["open_webui_preservation"]["source_version_evidence_count"], 1)
        self.assertEqual(plan["open_webui_preservation"]["backed_up_container_metadata_count"], 1)
        self.assertFalse(plan["open_webui_preservation"]["direct_database_reuse_approved_by_plan"])
        self.assertTrue(plan["open_webui_preservation"]["requires_temporary_instance_validation"])
        self.assertFalse(plan["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertTrue(plan["hardware_readiness"]["accepted"])
        self.assertFalse(plan["hardware_readiness"]["operator_must_review_hardware"])
        self.assertTrue(plan["gpu_runtime_readiness"]["accepted"])
        self.assertFalse(plan["gpu_runtime_readiness"]["operator_must_review_gpu_runtime"])
        self.assertTrue(plan["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"])
        self.assertFalse(plan["runtime_agent_socket_readiness"]["operator_must_review_runtime_agent_socket"])
        self.assertEqual(plan["warnings"], [])

    def test_build_plan_warns_when_networking_lacks_dhcp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(
                    networking={
                        "hostname_authority": "b1-appliance-config",
                        "hostname_source": "system-hostname",
                        "network_property_source": "host-dhcp-client",
                        "b1_manages_host_networking": False,
                        "b1_static_ip_configures": False,
                        "expected_operator_networking": "B1-defined system hostname plus host-managed DHCP lease/reservation and LAN DNS records",
                        "non_loopback_address_count": 1,
                        "default_route_interfaces": ["eno1"],
                        "default_route_address_count": 1,
                        "default_route_count": 1,
                        "default_route_protocols": ["static"],
                        "has_dhcp_default_route": False,
                        "dns_record_count": 7,
                        "operator_must_review_networking": True,
                        "warnings": ["Inventory did not prove a DHCP-owned default route"],
                    }
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

        self.assertFalse(plan["networking_readiness"]["has_dhcp_default_route"])
        self.assertTrue(plan["networking_readiness"]["operator_must_review_networking"])
        self.assertTrue(any("Host DHCP/networking requires operator review" in warning for warning in plan["warnings"]))

    def test_build_plan_accepts_confirmed_dhcp_reservation_plan_for_static_technitium_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            static_networking = {
                "hostname_authority": "b1-appliance-config",
                "hostname_source": "system-hostname",
                "network_property_source": "host-dhcp-client",
                "b1_manages_host_networking": False,
                "b1_static_ip_configures": False,
                "expected_operator_networking": "B1-defined system hostname plus host-managed DHCP lease/reservation and LAN DNS records",
                "non_loopback_address_count": 2,
                "default_route_interfaces": ["eno1"],
                "default_route_address_count": 2,
                "default_route_count": 1,
                "default_route_protocols": ["static"],
                "has_dhcp_default_route": False,
                "dns_record_count": 7,
                "operator_must_review_networking": True,
                "warnings": ["Inventory did not prove a DHCP-owned default route"],
            }
            inventory_path = self.write_json(root / "inventory.json", self.inventory(networking=static_networking))
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            network_plan_path = self.write_json(root / "network-dhcp-plan.json", self.network_dhcp_plan(confirmed=True))
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
                network_dhcp_plan_path=network_plan_path,
            )

        self.assertFalse(plan["networking_readiness"]["has_dhcp_default_route"])
        self.assertTrue(plan["networking_readiness"]["has_dhcp_network_proof"])
        self.assertEqual(plan["networking_readiness"]["network_proof"], "operator-reviewed-dhcp-reservation-plan")
        self.assertFalse(plan["networking_readiness"]["operator_must_review_networking"])
        self.assertEqual(plan["networking_readiness"]["warnings"], [])
        self.assertEqual(
            plan["networking_readiness"]["dhcp_reservation_plan"]["host_infrastructure_static_addresses"][0]["address"],
            "192.168.2.2",
        )
        self.assertFalse(any("Host DHCP/networking requires operator review" in warning for warning in plan["warnings"]))

    def test_build_plan_rejects_unconfirmed_dhcp_reservation_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            static_networking = {
                "hostname_authority": "b1-appliance-config",
                "hostname_source": "system-hostname",
                "network_property_source": "host-dhcp-client",
                "b1_manages_host_networking": False,
                "b1_static_ip_configures": False,
                "non_loopback_address_count": 2,
                "default_route_interfaces": ["eno1"],
                "default_route_address_count": 2,
                "default_route_count": 1,
                "default_route_protocols": ["static"],
                "has_dhcp_default_route": False,
                "dns_record_count": 7,
                "operator_must_review_networking": True,
                "warnings": ["Inventory did not prove a DHCP-owned default route"],
            }
            inventory_path = self.write_json(root / "inventory.json", self.inventory(networking=static_networking))
            scope_path = self.write_json(root / "scope.json", self.scope(root))
            network_plan_path = self.write_json(root / "network-dhcp-plan.json", self.network_dhcp_plan(confirmed=False))
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
                network_dhcp_plan_path=network_plan_path,
            )

        self.assertFalse(plan["networking_readiness"]["has_dhcp_network_proof"])
        self.assertEqual(plan["networking_readiness"]["network_proof"], "missing")
        self.assertTrue(plan["networking_readiness"]["operator_must_review_networking"])

    def test_build_plan_warns_when_target_identity_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_path = self.write_json(
                root / "inventory.json",
                self.inventory(
                    target_identity={
                        "hostname_authority": "b1-appliance-config",
                        "expected_target_host": "ai.b1.germering",
                        "expected_short_hostname": "ai",
                        "observed_hostname": "b1-5",
                        "observed_fqdn": "b1-5",
                        "observed_platform_node": "b1-5",
                        "hostname_matches_expected": False,
                        "fqdn_matches_expected": False,
                        "platform_node_matches_expected": False,
                        "accepted": False,
                        "operator_must_review_target_identity": True,
                        "warnings": ["Inventory host identity does not match expected target ai.b1.germering"],
                    }
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

        self.assertFalse(plan["target_identity_readiness"]["accepted"])
        self.assertTrue(plan["target_identity_readiness"]["operator_must_review_target_identity"])
        self.assertTrue(any("Target host identity requires operator review" in warning for warning in plan["warnings"]))

    def test_build_plan_warns_when_hardware_profile_is_below_initial_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_payload = self.inventory()
            inventory_payload["migration_readiness"]["hardware_profile"] = {
                "profile": "rtx3060-32gb-initial",
                "accepted": False,
                "minimum_gpu_vram_mib": 12288,
                "minimum_host_ram_mib": 32000,
                "largest_gpu_vram_mib": 6144,
                "host_total_ram_mib": 32168,
                "warnings": ["largest detected GPU VRAM is 6144 MiB; required initial profile needs at least 12288 MiB"],
            }
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
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
            )

        self.assertFalse(plan["hardware_readiness"]["accepted"])
        self.assertTrue(plan["hardware_readiness"]["operator_must_review_hardware"])
        self.assertTrue(any("Hardware profile requires operator review" in warning for warning in plan["warnings"]))

    def test_build_plan_warns_when_gpu_container_runtime_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_payload = self.inventory()
            inventory_payload["migration_readiness"]["gpu_container_runtime"] = {
                "available": True,
                "accepted": False,
                "nvidia_smi_available": True,
                "detected_gpu_count": 1,
                "docker_nvidia_runtime_available": False,
                "nvidia_container_toolkit_available": False,
                "operator_must_review_gpu_runtime": True,
                "warnings": ["Docker does not report an nvidia runtime; NVIDIA Container Toolkit is not wired into Docker"],
            }
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
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
            )

        self.assertFalse(plan["gpu_runtime_readiness"]["accepted"])
        self.assertTrue(plan["gpu_runtime_readiness"]["operator_must_review_gpu_runtime"])
        self.assertTrue(any("GPU container runtime requires operator review" in warning for warning in plan["warnings"]))

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

    def test_build_plan_warns_when_runtime_agent_socket_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            inventory_payload = self.inventory()
            inventory_payload["migration_readiness"]["runtime_agent_docker_socket"]["configured_gid"] = "0"
            inventory_payload["migration_readiness"]["runtime_agent_docker_socket"]["configured_gid_matches"] = False
            inventory_payload["migration_readiness"]["runtime_agent_docker_socket"]["runtime_agent_group_access_ready"] = False
            inventory_payload["migration_readiness"]["runtime_agent_docker_socket"]["warnings"] = [
                "B1_DOCKER_GID=0 does not match Docker socket GID 998"
            ]
            inventory_path = self.write_json(root / "inventory.json", inventory_payload)
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
            )

        self.assertFalse(plan["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"])
        self.assertTrue(plan["runtime_agent_socket_readiness"]["operator_must_review_runtime_agent_socket"])
        self.assertTrue(any("Runtime-agent Docker socket requires operator review" in warning for warning in plan["warnings"]))

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

    def test_port_readiness_accepts_current_b1_gateway_production_listeners(self) -> None:
        inventory_payload = self.inventory(
            listening_tcp=[
                {"local_address": "0.0.0.0", "port": 80, "process": 'users:(("docker-proxy",pid=1,fd=3))'},
                {"local_address": "0.0.0.0", "port": 443, "process": 'users:(("docker-proxy",pid=2,fd=3))'},
            ]
        )
        inventory_payload["classification"]["containers"].append(
            {
                "container": "b1-ai-hub-gateway-1",
                "image": "caddy:2.10.2-alpine",
                "classification": "b1-ai-hub-current-preserve",
                "ports": "0.0.0.0:80->80/tcp, [::]:80->80/tcp, 0.0.0.0:443->443/tcp, [::]:443->443/tcp",
            }
        )

        readiness, warnings = cutover.analyze_cutover_ports(
            inventory_payload,
            temporary_http_port=18080,
            temporary_https_port=18443,
            production_http_port=80,
            production_https_port=443,
        )

        self.assertEqual(warnings, [])
        self.assertFalse(readiness["operator_must_resolve_production_conflicts"])
        self.assertEqual(readiness["current_b1_gateway_published_ports"], [80, 443])
        self.assertEqual(
            sorted(readiness["production_ports_owned_by_current_b1_gateway"]),
            ["production_http", "production_https"],
        )

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

    def test_build_plan_tracks_missing_optional_monitoring_dns_without_blocking_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            records = {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
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
        self.assertTrue(plan["dns_readiness"]["all_hosts_share_gateway_address"])
        self.assertFalse(plan["dns_readiness"]["operator_must_review_dns"])
        self.assertEqual(plan["dns_readiness"]["missing_hosts"], [])
        self.assertEqual(plan["dns_readiness"]["optional_missing_hosts"], ["monitoring.ai.b1.germering"])
        self.assertEqual(plan["dns_readiness"]["optional_records"], {"monitoring.ai.b1.germering": []})
        self.assertFalse(any("monitoring.ai.b1.germering" in warning for warning in plan["warnings"]))

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

    def test_build_plan_warns_on_divergent_optional_monitoring_dns_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            records = {host: ["192.168.2.100"] for host in cutover.PRODUCTION_HOSTS}
            records["monitoring.ai.b1.germering"] = ["192.168.2.101"]
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
        self.assertTrue(plan["dns_readiness"]["all_hosts_share_gateway_address"])
        self.assertTrue(plan["dns_readiness"]["operator_must_review_dns"])
        self.assertEqual(plan["dns_readiness"]["optional_divergent_hosts"], ["monitoring.ai.b1.germering"])
        self.assertTrue(any("Optional DNS inventory records differ from ai.b1.germering" in warning for warning in plan["warnings"]))

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

    def test_write_plan_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            output = root / "cutover-plan.json"
            try:
                output.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(cutover.CutoverPlanError, "symlink"):
                cutover.write_plan({"format": "test"}, output)

            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")


if __name__ == "__main__":
    unittest.main()
