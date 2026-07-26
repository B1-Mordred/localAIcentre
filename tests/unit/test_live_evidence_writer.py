from __future__ import annotations

import json
import stat
import tempfile
import unittest
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
