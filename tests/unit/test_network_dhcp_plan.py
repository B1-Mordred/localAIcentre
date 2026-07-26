from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
NETWORK_DHCP_PLAN_PATH = ROOT / "deploy" / "scripts" / "network_dhcp_plan.py"
spec = importlib.util.spec_from_file_location("b1_network_dhcp_plan", NETWORK_DHCP_PLAN_PATH)
network_dhcp_plan = importlib.util.module_from_spec(spec)
sys.modules["b1_network_dhcp_plan"] = network_dhcp_plan
assert spec.loader is not None
spec.loader.exec_module(network_dhcp_plan)


def fake_nmcli_runner(*, method: str = "manual", addresses: str = "192.168.2.2/24, 192.168.2.100/24") -> Any:
    def runner(command: list[str]) -> dict[str, Any]:
        if command[:3] == ["nmcli", "-t", "-f"] and command[-3:] == ["con", "show", "lan-dhcp-dns"]:
            return {
                "returncode": 0,
                "stdout": (
                    "connection.id:lan-dhcp-dns\n"
                    "connection.uuid:31c86026-56dd-46c7-81b7-127a53a0f3ed\n"
                    "connection.interface-name:enp176s0\n"
                    f"ipv4.method:{method}\n"
                    f"ipv4.addresses:{addresses}\n"
                    "ipv4.gateway:192.168.2.1\n"
                    "ipv4.dns:192.168.2.2,1.1.1.1,8.8.8.8\n"
                    "ipv4.dns-search:b1.germering\n"
                    "ipv4.ignore-auto-dns:yes\n"
                    "ipv6.method:auto\n"
                ),
                "stderr": "",
            }
        if command[:3] == ["nmcli", "-t", "-f"] and command[-3:] == ["dev", "show", "enp176s0"]:
            return {"returncode": 0, "stdout": "GENERAL.DEVICE:enp176s0\nGENERAL.HWADDR:00:01:2e:a5:cb:c1\n", "stderr": ""}
        return {"returncode": 127, "stdout": "", "stderr": "unexpected command"}

    return runner


class NetworkDhcpPlanTests(unittest.TestCase):
    def test_static_technitium_address_is_preserved_and_dhcp_reservation_is_required(self) -> None:
        plan = network_dhcp_plan.build_plan(
            connection="lan-dhcp-dns",
            appliance_hostname="ai.b1.germering",
            dns_admin_url="http://technitium.b1.germering:5380",
            required_addresses=[{"address": "192.168.2.100", "purpose": "b1-ai-hub-gateway"}],
            static_infrastructure_addresses=[{"address": "192.168.2.2", "purpose": "technitium-dhcp-dns"}],
            reservation_confirmed=False,
            command_runner=fake_nmcli_runner(),
            now=datetime(2026, 7, 26, 18, 30, tzinfo=UTC),
        )

        self.assertEqual(plan["format"], "b1-ai-hub-network-dhcp-plan/v1")
        self.assertEqual(plan["status"], "operator-review-required")
        self.assertFalse(plan["ready_to_apply"])
        self.assertTrue(plan["safety"]["read_only"])
        self.assertFalse(plan["safety"]["host_networking_changed"])
        self.assertEqual(plan["dns_admin"]["host"], "technitium.b1.germering")
        self.assertEqual(plan["dns_admin"]["port"], 5380)
        self.assertEqual(plan["connection"]["ipv4_method"], "manual")
        self.assertEqual(plan["connection"]["ipv4_addresses"], ["192.168.2.2", "192.168.2.100"])
        self.assertEqual(plan["ordinary_dhcp_lease_count"], 1)
        self.assertFalse(plan["b1_static_ip_configures"])
        self.assertEqual(plan["dhcp_reserved_appliance_addresses"][0]["address"], "192.168.2.100")
        self.assertEqual(plan["host_infrastructure_static_addresses"][0]["cidr"], "192.168.2.2/24")
        self.assertFalse(any("ipv4.method=manual" in blocker for blocker in plan["blockers"]))
        self.assertFalse(any("multiple required IPv4 addresses" in blocker for blocker in plan["blockers"]))
        self.assertTrue(any("DHCP reservations" in blocker for blocker in plan["blockers"]))
        stage_commands = " ".join(plan["commands"]["stage_candidate_profile"])
        self.assertIn("ipv4.method auto", stage_commands)
        self.assertIn("ipv4.addresses '192.168.2.2/24'", stage_commands)
        self.assertNotIn("192.168.2.100/24", stage_commands)

    def test_manual_profile_with_static_infra_and_confirmed_reservation_is_ready(self) -> None:
        plan = network_dhcp_plan.build_plan(
            connection="lan-dhcp-dns",
            appliance_hostname="ai.b1.germering",
            dns_admin_url="http://technitium.b1.germering:5380",
            required_addresses=[{"address": "192.168.2.100", "purpose": "b1-ai-hub-gateway"}],
            static_infrastructure_addresses=[{"address": "192.168.2.2", "purpose": "technitium-dhcp-dns"}],
            reservation_confirmed=True,
            command_runner=fake_nmcli_runner(),
            now=datetime(2026, 7, 26, 18, 30, tzinfo=UTC),
        )

        self.assertEqual(plan["status"], "ready")
        self.assertTrue(plan["ready_to_apply"])
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["candidate_connection"], "lan-dhcp-dns-dhcp-candidate")
        self.assertIn("nmcli connection up 'lan-dhcp-dns-dhcp-candidate'", plan["commands"]["activate_after_reservations_are_confirmed"])
        self.assertIn("nmcli connection up 'lan-dhcp-dns'", plan["commands"]["rollback"])
        self.assertTrue(any("manual IPv4" in warning for warning in plan["warnings"]))

    def test_single_confirmed_auto_profile_is_ready(self) -> None:
        plan = network_dhcp_plan.build_plan(
            connection="lan-dhcp-dns",
            appliance_hostname="ai.b1.germering",
            dns_admin_url="http://technitium.b1.germering:5380",
            required_addresses=[{"address": "192.168.2.100", "purpose": "b1-ai-hub-gateway"}],
            reservation_confirmed=True,
            command_runner=fake_nmcli_runner(method="auto", addresses="192.168.2.100/24"),
            now=datetime(2026, 7, 26, 18, 30, tzinfo=UTC),
        )

        self.assertEqual(plan["status"], "ready")
        self.assertTrue(plan["ready_to_apply"])
        self.assertEqual(plan["blockers"], [])
        stage_commands = " ".join(plan["commands"]["stage_candidate_profile"])
        self.assertIn("ipv4.addresses ''", stage_commands)

    def test_required_address_validation_rejects_non_ipv4(self) -> None:
        with self.assertRaisesRegex(network_dhcp_plan.NetworkDhcpPlanError, "address must be IPv4"):
            network_dhcp_plan.parse_required_addresses(["fd9b:4afc:bb00:1::100=ipv6"])
        with self.assertRaisesRegex(network_dhcp_plan.NetworkDhcpPlanError, "invalid IPv4 address"):
            network_dhcp_plan.parse_required_addresses(["not-an-ip=gateway"])

    def test_missing_static_infrastructure_address_blocks_candidate(self) -> None:
        plan = network_dhcp_plan.build_plan(
            connection="lan-dhcp-dns",
            appliance_hostname="ai.b1.germering",
            dns_admin_url="http://technitium.b1.germering:5380",
            required_addresses=[{"address": "192.168.2.100", "purpose": "b1-ai-hub-gateway"}],
            static_infrastructure_addresses=[{"address": "192.168.2.2", "purpose": "technitium-dhcp-dns"}],
            reservation_confirmed=True,
            command_runner=fake_nmcli_runner(method="auto", addresses="192.168.2.100/24"),
            now=datetime(2026, 7, 26, 18, 30, tzinfo=UTC),
        )

        self.assertEqual(plan["status"], "operator-review-required")
        self.assertTrue(any("static host-infrastructure address is not present" in blocker for blocker in plan["blockers"]))

    def test_write_private_json_refuses_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            symlink = root / "plan.json"
            symlink.symlink_to(target)

            with self.assertRaisesRegex(network_dhcp_plan.NetworkDhcpPlanError, "regular file"):
                network_dhcp_plan.write_private_json(symlink, {"format": "test"})


if __name__ == "__main__":
    unittest.main()
