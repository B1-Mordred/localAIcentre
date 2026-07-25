from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import acceptance  # noqa: E402


def load_gpu_acceptance_module() -> Any:
    path = ROOT / "tests" / "integration" / "test_live_cross_runtime_gpu.py"
    spec = importlib.util.spec_from_file_location("b1_live_cross_runtime_gpu_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load GPU acceptance module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GpuAcceptanceHarnessTests(unittest.TestCase):
    def test_required_check_names_match_control_plane_gate(self) -> None:
        harness = load_gpu_acceptance_module()
        self.assertEqual(tuple(harness.GPU_ACCEPTANCE_REQUIRED_CHECKS), tuple(acceptance.GPU_ACCEPTANCE_REQUIRED_CHECKS))

    def test_recovery_probe_records_nested_runtime_agent_ok_status(self) -> None:
        harness = load_gpu_acceptance_module()
        test_case = harness.LiveCrossRuntimeGpuAcceptanceTests(
            methodName="test_predefined_runtime_recovery_action_is_available_when_enabled"
        )
        test_case.checks = {}
        test_case.evidence = []
        test_case.json_request = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "runtime": "localai",
            "action": "recover",
            "runtime_agent": {"status": "ok", "strategy": "bounded_restart"},
        }

        with patch.dict(os.environ, {"B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION": "1"}, clear=False):
            test_case.test_predefined_runtime_recovery_action_is_available_when_enabled()

        check = test_case.checks["bounded_runtime_recovery_action"]
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["result_status"], "ok")
        self.assertEqual(check["strategy"], "bounded_restart")

    def test_recovery_probe_dry_run_rehearsal_does_not_satisfy_handoff_check(self) -> None:
        harness = load_gpu_acceptance_module()
        test_case = harness.LiveCrossRuntimeGpuAcceptanceTests(
            methodName="test_predefined_runtime_recovery_action_is_available_when_enabled"
        )
        test_case.checks = {}
        test_case.evidence = []
        test_case.json_request = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "runtime": "localai",
            "action": "recover",
            "runtime_agent": {"status": "dry_run", "strategy": "bounded_restart"},
        }

        with patch.dict(
            os.environ,
            {"B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION": "1", "B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN": "1"},
            clear=False,
        ):
            test_case.test_predefined_runtime_recovery_action_is_available_when_enabled()

        check = test_case.checks["bounded_runtime_recovery_action"]
        self.assertEqual(check["status"], "dry_run")
        self.assertEqual(check["result_status"], "dry_run")

    def test_recovery_probe_rejects_dry_run_by_default(self) -> None:
        harness = load_gpu_acceptance_module()
        test_case = harness.LiveCrossRuntimeGpuAcceptanceTests(
            methodName="test_predefined_runtime_recovery_action_is_available_when_enabled"
        )
        test_case.checks = {}
        test_case.evidence = []
        test_case.json_request = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "runtime": "localai",
            "action": "recover",
            "runtime_agent": {"status": "dry_run", "strategy": "bounded_restart"},
        }

        with patch.dict(os.environ, {"B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION": "1"}, clear=False):
            os.environ.pop("B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN", None)
            with self.assertRaises(AssertionError):
                test_case.test_predefined_runtime_recovery_action_is_available_when_enabled()


if __name__ == "__main__":
    unittest.main()
