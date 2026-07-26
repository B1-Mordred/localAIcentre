from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import evidence


ROOT = Path(__file__).resolve().parents[2]
LIVE_EVIDENCE_HARNESSES = (
    "tests/smoke/test_live_stack.py",
    "tests/integration/test_live_localai_runtime.py",
    "tests/integration/test_live_installed_workflows.py",
    "tests/integration/test_live_cross_runtime_gpu.py",
    "tests/integration/test_live_restart_reconciliation.py",
    "tests/compatibility/test_native_comfyui_compatibility.py",
    "tests/compatibility/test_legacy_comfyui_listener.py",
    "tests/compatibility/test_modelhub_client_sync.py",
    "tests/compatibility/test_remote_nodes_non_comfy.py",
    "tests/compatibility/test_voicebox_remote.py",
    "tests/security/test_live_security_acceptance.py",
)


class LiveEvidenceWriterTests(unittest.TestCase):
    def test_write_private_json_creates_private_atomic_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acceptance" / "live-smoke.json"
            evidence.write_private_json(path, {"format": "test/v1", "status": "ok"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(payload["format"], "test/v1")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(mode, 0o640)

    def test_write_private_json_stamps_source_metadata_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acceptance" / "live-smoke.json"
            env = {
                "B1_SOURCE_COMMIT": "a" * 40,
                "B1_SOURCE_REF": "agent/test",
                "B1_SOURCE_DIRTY": "false",
                "B1_SOURCE_DIRTY_PATH_COUNT": "0",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                evidence.write_private_json(path, {"format": "test/v1", "status": "ok"})
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["source"], "environment")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertEqual(payload["short_commit"], "a" * 12)
        self.assertEqual(payload["source_ref"], "agent/test")
        self.assertEqual(payload["source_branch"], "agent/test")
        self.assertFalse(payload["source_dirty"])
        self.assertEqual(payload["dirty_path_count"], 0)

    def test_write_private_json_keeps_existing_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acceptance" / "live-smoke.json"
            with mock.patch.dict(os.environ, {"B1_SOURCE_COMMIT": "a" * 40}, clear=False):
                evidence.write_private_json(path, {"format": "test/v1", "source_commit": "b" * 40})
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["source_commit"], "b" * 40)

    def test_write_private_json_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "evidence.json"
            self.symlink_or_skip(target, link)

            with self.assertRaises(evidence.EvidenceWriteError):
                evidence.write_private_json(link, {"status": "ok"})

    def test_write_private_json_refuses_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            link = root / "acceptance"
            self.symlink_or_skip(real, link)

            with self.assertRaises(evidence.EvidenceWriteError):
                evidence.write_private_json(link / "live-smoke.json", {"status": "ok"})

    def test_live_harnesses_use_shared_private_writer(self) -> None:
        for relative in LIVE_EVIDENCE_HARNESSES:
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("from tests.support.evidence import write_private_json", source)
                self.assertIn("write_private_json(", source)
                self.assertNotIn("path.write_text(", source)

    def symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
