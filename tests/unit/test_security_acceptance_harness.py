from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import acceptance  # noqa: E402


def load_security_acceptance_module() -> Any:
    path = ROOT / "tests" / "security" / "test_live_security_acceptance.py"
    spec = importlib.util.spec_from_file_location("b1_live_security_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load security acceptance module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecurityAcceptanceHarnessTests(unittest.TestCase):
    def test_required_check_names_match_control_plane_gate(self) -> None:
        harness = load_security_acceptance_module()
        self.assertEqual(tuple(harness.SECURITY_REQUIRED_CHECKS), tuple(acceptance.SECURITY_REQUIRED_CHECKS))

    def test_import_url_policy_checks_are_individually_required(self) -> None:
        harness = load_security_acceptance_module()
        for name in (
            "import_ssrf_blocked",
            "import_metadata_ssrf_blocked",
            "import_private_network_blocked",
            "import_plain_http_blocked",
        ):
            self.assertIn(name, harness.SECURITY_REQUIRED_CHECKS)

    def test_runtime_agent_arbitrary_operation_checks_are_required(self) -> None:
        harness = load_security_acceptance_module()
        self.assertIn("runtime_agent_arbitrary_runtime_rejected", harness.SECURITY_REQUIRED_CHECKS)
        self.assertIn("runtime_agent_arbitrary_logs_rejected", harness.SECURITY_REQUIRED_CHECKS)


if __name__ == "__main__":
    unittest.main()
