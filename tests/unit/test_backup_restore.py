from __future__ import annotations

import importlib.util
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


backup_mod = load_module("b1_backup", ROOT / "deploy" / "scripts" / "backup.py")
restore_mod = load_module("b1_restore", ROOT / "deploy" / "scripts" / "restore.py")
sys.path.insert(0, str(ROOT / "services" / "control-plane"))
from app import backup_restore as control_plane_backup_restore  # noqa: E402


class BackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_env(
            {
                "B1_BACKUP_ENCRYPTION_MODE": "none",
                "B1_BACKUP_ENCRYPTION_KEY": None,
                "B1_BACKUP_ENCRYPTION_KEY_FILE": None,
                "B1_MASTER_KEY": None,
                "B1_MASTER_KEY_FILE": None,
            }
        )

    def patch_env(self, values: dict[str, str | None]) -> None:
        original = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        def restore() -> None:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def make_root(self, root: Path) -> None:
        (root / "data" / "open-webui").mkdir(parents=True)
        (root / "data" / "open-webui" / "db.sqlite").write_text("webui", encoding="utf-8")
        (root / "secrets").mkdir()
        (root / "secrets" / "admin_bootstrap_key").write_text("secret", encoding="utf-8")
        (root / "models" / "blobs").mkdir(parents=True)
        (root / "models" / "blobs" / "downloadable").write_text("skip", encoding="utf-8")
        (root / "models" / "runtime-views" / "localai" / "m" / "1").mkdir(parents=True)
        (root / "models" / "runtime-views" / "localai" / "m" / "1" / "model.gguf").write_text("skip-view", encoding="utf-8")
        (root / "models" / "tts").mkdir(parents=True)
        (root / "models" / "tts" / "custom.bin").write_bytes(b"custom")
        (root / "cache").mkdir()
        (root / "cache" / "temp").write_text("skip", encoding="utf-8")

    def test_backup_creates_archive_manifest_and_excludes_redownloadable_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            backup_dir = backup_mod.backup(root, label="unit")
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
            paths = {item["path"] for item in manifest["files"]}
            self.assertEqual(manifest["format"], "b1-ai-hub-backup/v1")
            self.assertTrue(manifest["contains_sensitive_data"])
            self.assertIn("data/open-webui/db.sqlite", paths)
            self.assertIn("secrets/admin_bootstrap_key", paths)
            self.assertIn("models/tts/custom.bin", paths)
            self.assertNotIn("models/blobs/downloadable", paths)
            self.assertNotIn("models/runtime-views/localai/m/1/model.gguf", paths)
            self.assertNotIn("cache/temp", paths)
            self.assertEqual(backup_mod.sha256_file(backup_dir / "payload.tar.gz"), manifest["archive"]["sha256"])

    def test_backup_refuses_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            (root / "models" / "tts" / "linked").symlink_to(root / "secrets" / "admin_bootstrap_key")
            with self.assertRaises(control_plane_backup_restore.BackupError):
                backup_mod.backup(root, label="symlink")

    def test_backup_marks_optional_postgres_dump_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            dump_path = root / "data" / "control-plane" / "postgres-logical-export.json"
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text("{}", encoding="utf-8")

            backup_dir = backup_mod.backup(
                root,
                label="with-db",
                postgres_dump_path=dump_path,
                postgres_dump_format="b1-ai-hub-postgres-logical-export/v1",
            )
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue(manifest["postgres_dump_included"])
            self.assertEqual(manifest["postgres_dump"]["path"], "data/control-plane/postgres-logical-export.json")
            self.assertTrue(manifest["postgres_dump"]["sensitive"])

    def test_backup_wrapper_marks_native_dump_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            logical_path = root / "data" / "control-plane" / "postgres-logical-export.json"
            native_path = root / "data" / "control-plane" / "postgres-native.dump"
            logical_path.parent.mkdir(parents=True, exist_ok=True)
            logical_path.write_text("{}", encoding="utf-8")
            native_path.write_bytes(b"native")

            backup_dir = backup_mod.backup(
                root,
                label="with-native",
                postgres_dump_path=logical_path,
                postgres_dump_format=control_plane_backup_restore.LOGICAL_POSTGRES_DUMP_FORMAT,
                postgres_extra_dumps=[
                    {
                        "path": str(native_path),
                        "format": control_plane_backup_restore.NATIVE_POSTGRES_DUMP_FORMAT,
                        "kind": "native",
                        "tool": "pg_dump",
                        "verified": False,
                    }
                ],
            )
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual([item["kind"] for item in manifest["postgres_dumps"]], ["logical", "native"])
            self.assertEqual(manifest["postgres_native_dump"]["path"], "data/control-plane/postgres-native.dump")

    def test_restore_verifies_files_into_alternate_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            restore_root = Path(tmp) / "restore"
            root.mkdir()
            self.make_root(root)
            backup_dir = backup_mod.backup(root, label="unit")
            report = restore_mod.restore(backup_dir, restore_root)
            self.assertEqual(report["status"], "restored")
            self.assertEqual((restore_root / "data" / "open-webui" / "db.sqlite").read_text(encoding="utf-8"), "webui")
            self.assertEqual((restore_root / "models" / "tts" / "custom.bin").read_bytes(), b"custom")
            self.assertTrue((restore_root / "restore-report.json").is_file())

    def test_restore_refuses_non_empty_target_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            restore_root = Path(tmp) / "restore"
            root.mkdir()
            restore_root.mkdir()
            (restore_root / "existing").write_text("data", encoding="utf-8")
            self.make_root(root)
            backup_dir = backup_mod.backup(root, label="unit")
            with self.assertRaises(restore_mod.RestoreError):
                restore_mod.restore(backup_dir, restore_root)

    def test_restore_rejects_tampered_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            self.make_root(root)
            backup_dir = backup_mod.backup(root, label="unit")
            with (backup_dir / "payload.tar.gz").open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaises(restore_mod.RestoreError):
                restore_mod.restore(backup_dir, Path(tmp) / "restore")

    def test_restore_rejects_archive_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            backup_dir.mkdir()
            archive_path = backup_dir / "payload.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("data/open-webui/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../secrets/admin_bootstrap_key"
                archive.addfile(info)
            manifest = {
                "format": "b1-ai-hub-backup/v1",
                "archive": {"file": "payload.tar.gz", "sha256": restore_mod.sha256_file(archive_path)},
                "files": [],
            }
            (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(restore_mod.RestoreError, "unsupported archive member type"):
                restore_mod.restore(backup_dir, Path(tmp) / "restore")

    def test_restore_rejects_preexisting_symlink_in_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            restore_root = Path(tmp) / "restore"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            self.make_root(root)
            backup_dir = backup_mod.backup(root, label="unit")
            (restore_root / "data").mkdir(parents=True)
            (restore_root / "data" / "open-webui").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(restore_mod.RestoreError, "symlink"):
                restore_mod.restore(backup_dir, restore_root, force=True)

    @unittest.skipIf(control_plane_backup_restore.AESGCM is None or restore_mod.AESGCM is None, "cryptography is not installed in this lightweight test environment")
    def test_restore_accepts_encrypted_only_archive_with_key(self) -> None:
        key = "backup-key-" + ("k" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            self.make_root(root)
            control_plane_backup_restore.create_backup(
                root,
                label="encrypted-only",
                backup_encryption_key=key,
                backup_encryption_mode="encrypted-only",
            )
            backup_dir = root / "backups" / "encrypted-only"
            self.assertFalse((backup_dir / "payload.tar.gz").exists())

            with self.assertRaisesRegex(restore_mod.RestoreError, "requires"):
                restore_mod.restore(backup_dir, Path(tmp) / "restore-missing-key")

            report = restore_mod.restore(backup_dir, Path(tmp) / "restore", backup_encryption_key=key)
            self.assertEqual(report["status"], "restored")
            self.assertEqual(report["archive_encryption"]["mode"], "encrypted-only")
            self.assertEqual((Path(tmp) / "restore" / "models" / "tts" / "custom.bin").read_bytes(), b"custom")

    @unittest.skipIf(control_plane_backup_restore.AESGCM is None, "cryptography is not installed in this lightweight test environment")
    def test_backup_wrapper_supports_encrypted_archive_copy(self) -> None:
        key = "backup-key-" + ("k" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)

            backup_dir = backup_mod.backup(root, label="encrypted-copy", backup_encryption_key=key, backup_encryption_mode="copy")
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((backup_dir / "payload.tar.gz").is_file())
            self.assertTrue((backup_dir / "payload.tar.gz.aesgcm").is_file())
            self.assertEqual(manifest["archive_encryption"]["mode"], "copy")
            self.assertEqual(manifest["archive_encryption"]["plaintext_sha256"], manifest["archive"]["sha256"])


if __name__ == "__main__":
    unittest.main()
