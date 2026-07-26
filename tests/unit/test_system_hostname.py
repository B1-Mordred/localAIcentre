from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_HOSTNAME_PATH = ROOT / "deploy" / "scripts" / "system_hostname.py"
spec = importlib.util.spec_from_file_location("b1_system_hostname", SYSTEM_HOSTNAME_PATH)
system_hostname = importlib.util.module_from_spec(spec)
sys.modules["b1_system_hostname"] = system_hostname
assert spec.loader is not None
spec.loader.exec_module(system_hostname)


class SystemHostnameTests(unittest.TestCase):
    def test_normalize_refuses_network_properties(self) -> None:
        invalid_values = {
            "192.168.2.100": "not an IP address",
            "https://ai.b1.germering": "without scheme",
            "ai.b1.germering:443": "without scheme, path, or port",
            "bad_host": "invalid hostname labels",
        }
        for value, message in invalid_values.items():
            with self.subTest(value=value):
                with self.assertRaisesRegex(system_hostname.SystemHostnameError, message):
                    system_hostname.normalize_appliance_hostname(value)

        self.assertEqual(system_hostname.normalize_appliance_hostname("AI.B1.GERMERING."), "ai.b1.germering")
        self.assertEqual(system_hostname.desired_static_hostname("ai.b1.germering"), "ai")

    def test_status_plans_only_hostname_change_and_leaves_networking_dhcp_owned(self) -> None:
        status = system_hostname.hostname_status(
            "AI.B1.GERMERING.",
            hostnamectl_static="devbox",
            socket_hostname="devbox",
            socket_fqdn="devbox.lan",
            platform_node="devbox",
        )

        self.assertFalse(status["matches"])
        self.assertEqual(status["hostname_authority"], "b1-appliance-config")
        self.assertEqual(status["hostname_source"], "system-hostname")
        self.assertEqual(status["network_property_source"], "host-dhcp-client")
        self.assertFalse(status["b1_manages_host_networking"])
        self.assertFalse(status["b1_static_ip_configures"])
        self.assertEqual(status["network_changes"], [])
        self.assertEqual(status["desired_static_hostname"], "ai")
        self.assertEqual(status["apply_command"], ["hostnamectl", "set-hostname", "ai"])

    def test_status_accepts_short_or_fqdn_system_hostname(self) -> None:
        short = system_hostname.hostname_status(
            "ai.b1.germering",
            hostnamectl_static="ai",
            socket_hostname="ai",
            socket_fqdn="ai.b1.germering",
            platform_node="ai",
        )
        fqdn = system_hostname.hostname_status(
            "ai.b1.germering",
            hostnamectl_static="ai.b1.germering",
            socket_hostname="ai.b1.germering",
            socket_fqdn="ai.b1.germering",
            platform_node="ai.b1.germering",
        )

        self.assertTrue(short["matches"])
        self.assertTrue(fqdn["matches"])
        self.assertIn("hostnamectl_static", short["matching_sources"])
        self.assertIn("socket_fqdn", fqdn["matching_sources"])


if __name__ == "__main__":
    unittest.main()
