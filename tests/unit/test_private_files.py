from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import private_files  # noqa: E402


CRITICAL_HANDOFF_WRITERS = (
    "services/control-plane/app/backup_migration_rollback.py",
    "services/control-plane/app/backup_restore.py",
    "services/control-plane/app/open_webui_migration.py",
    "services/control-plane/app/rollback_rehearsal.py",
    "deploy/scripts/cutover.py",
    "deploy/scripts/old_stack_backup.py",
    "deploy/scripts/restore.py",
)


class PrivateFileTests(unittest.TestCase):
    def test_write_private_json_is_atomic_private_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports" / "evidence.json"
            written = private_files.write_private_json(path, {"format": "test/v1", "status": "ok"}, mode=0o600)

            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(written, path)
        self.assertEqual(payload["format"], "test/v1")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(mode, 0o600)

    def test_write_private_json_refuses_symlink_target_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text('{"status":"old"}\n', encoding="utf-8")
            link = root / "evidence.json"
            self.symlink_or_skip(target, link)

            with self.assertRaisesRegex(private_files.PrivateFileError, "symlink"):
                private_files.write_private_json(link, {"status": "new"}, mode=0o600)

            self.assertEqual(target.read_text(encoding="utf-8"), '{"status":"old"}\n')
            self.assertTrue(link.is_symlink())

    def test_write_private_json_refuses_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            linked = root / "acceptance"
            self.symlink_or_skip(real, linked, target_is_directory=True)

            with self.assertRaisesRegex(private_files.PrivateFileError, "symlinked directory"):
                private_files.write_private_json(linked / "evidence.json", {"status": "ok"}, mode=0o600)

    def test_write_private_json_refuses_non_regular_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "evidence.json"
            target.mkdir()

            with self.assertRaisesRegex(private_files.PrivateFileError, "non-regular"):
                private_files.write_private_json(target, {"status": "ok"}, mode=0o600)

    def test_critical_handoff_writers_use_private_file_helper(self) -> None:
        for relative in CRITICAL_HANDOFF_WRITERS:
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("write_private_json", source)
                self.assertNotIn("write_text(json.dumps", source)

    def symlink_or_skip(self, target: Path, link: Path, *, target_is_directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
