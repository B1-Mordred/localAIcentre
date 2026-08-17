from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import backup_restore  # noqa: E402


class ControlPlaneBackupTests(unittest.TestCase):
    def make_root(self, root: Path) -> None:
        (root / "data" / "open-webui").mkdir(parents=True)
        (root / "data" / "open-webui" / "db.sqlite").write_text("webui", encoding="utf-8")
        (root / "models" / "tts").mkdir(parents=True)
        (root / "models" / "tts" / "custom.bin").write_bytes(b"custom")
        (root / "models" / "blobs").mkdir(parents=True)
        (root / "models" / "blobs" / "redownloadable").write_text("skip", encoding="utf-8")
        (root / "models" / "runtime-views" / "localai" / "m" / "1").mkdir(parents=True)
        (root / "models" / "runtime-views" / "localai" / "m" / "1" / "model.gguf").write_text("skip-view", encoding="utf-8")
        (root / "secrets").mkdir()
        (root / "secrets" / "master_encryption_key").write_text("secret", encoding="utf-8")
        (root / "restore-tests" / "old").mkdir(parents=True)
        (root / "restore-tests" / "old" / "report.json").write_text("skip", encoding="utf-8")

    def set_backup_created_at(self, backup_dir: Path, created_at: datetime) -> None:
        manifest_path = backup_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = created_at.isoformat()
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    def test_create_list_verify_and_restore_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)

            summary = backup_restore.create_backup(root, label="unit")
            self.assertEqual(summary["name"], "unit")
            self.assertTrue(summary["contains_sensitive_data"])

            listed = backup_restore.list_backups(root / "backups")
            self.assertEqual([item["name"] for item in listed], ["unit"])

            verified = backup_restore.verify_backup(root / "backups", "unit")
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(verified["file_count"], 3)

            manifest = backup_restore.load_manifest(root / "backups" / "unit")
            paths = {item["path"] for item in manifest["files"]}
            self.assertIn("data/open-webui/db.sqlite", paths)
            self.assertIn("models/tts/custom.bin", paths)
            self.assertIn("secrets/master_encryption_key", paths)
            self.assertNotIn("models/blobs/redownloadable", paths)
            self.assertNotIn("models/runtime-views/localai/m/1/model.gguf", paths)
            self.assertNotIn("restore-tests/old/report.json", paths)

            report = backup_restore.restore_backup_to_alternate(root / "backups", "unit", root / "restore-tests")
            self.assertEqual(report["status"], "restored")
            self.assertEqual((root / "restore-tests" / "unit" / "models" / "tts" / "custom.bin").read_bytes(), b"custom")
            self.assertTrue((root / "restore-tests" / "unit" / "restore-report.json").is_file())

    def test_manifest_marks_included_postgres_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            dump_path = root / "data" / "control-plane" / "postgres-logical-export.json"
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text('{"format":"b1-ai-hub-postgres-logical-export/v1"}', encoding="utf-8")

            summary = backup_restore.create_backup(
                root,
                label="with-db",
                postgres_dump_path=dump_path,
                postgres_dump_format="b1-ai-hub-postgres-logical-export/v1",
            )
            manifest = backup_restore.load_manifest(root / "backups" / "with-db")

            self.assertTrue(summary["postgres_dump_included"])
            self.assertEqual(summary["postgres_dump"]["path"], "data/control-plane/postgres-logical-export.json")
            self.assertEqual(manifest["postgres_dump"]["format"], "b1-ai-hub-postgres-logical-export/v1")
            self.assertTrue(manifest["postgres_dump"]["sensitive"])
            self.assertEqual(backup_restore.verify_backup(root / "backups", "with-db")["postgres_dump"]["path"], "data/control-plane/postgres-logical-export.json")

    def test_manifest_records_logical_and_native_postgres_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            logical_path = root / "data" / "control-plane" / "postgres-logical-export.json"
            native_path = root / "data" / "control-plane" / "postgres-native.dump"
            logical_path.parent.mkdir(parents=True, exist_ok=True)
            logical_path.write_text('{"format":"b1-ai-hub-postgres-logical-export/v1"}', encoding="utf-8")
            native_path.write_bytes(b"pg-custom-dump")

            summary = backup_restore.create_backup(
                root,
                label="with-native-db",
                postgres_dump_path=logical_path,
                postgres_dump_format=backup_restore.LOGICAL_POSTGRES_DUMP_FORMAT,
                postgres_extra_dumps=[
                    {
                        "path": native_path,
                        "format": backup_restore.NATIVE_POSTGRES_DUMP_FORMAT,
                        "kind": "native",
                        "tool": "pg_dump",
                        "verified": True,
                    }
                ],
            )
            manifest = backup_restore.load_manifest(root / "backups" / "with-native-db")
            verified = backup_restore.verify_backup(root / "backups", "with-native-db")

            self.assertTrue(summary["postgres_dump_included"])
            self.assertEqual(summary["postgres_dump"]["kind"], "logical")
            self.assertEqual(summary["postgres_native_dump"]["kind"], "native")
            self.assertEqual([item["kind"] for item in manifest["postgres_dumps"]], ["logical", "native"])
            self.assertEqual(verified["postgres_native_dump_verification"]["status"], "archive-member-verified")

            original_verify = backup_restore.verify_native_postgres_dump
            self.addCleanup(lambda: setattr(backup_restore, "verify_native_postgres_dump", original_verify))
            backup_restore.verify_native_postgres_dump = lambda path: {"status": "verified", "path": str(path)}  # type: ignore[assignment]
            report = backup_restore.restore_backup_to_alternate(root / "backups", "with-native-db", root / "restore-tests")
            self.assertEqual(report["postgres_native_dump_verification"]["status"], "verified")
            self.assertTrue((root / "restore-tests" / "with-native-db" / "data" / "control-plane" / "postgres-native.dump").is_file())

    def test_native_postgres_dump_uses_env_password_and_verifies_custom_dump(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
            if argv[0] == "pg_dump":
                self.assertNotIn("secret-pass", " ".join(argv))
                self.assertEqual((kwargs.get("env") or {})["PGPASSWORD"], "secret-pass")  # type: ignore[index]
                Path(argv[argv.index("--file") + 1]).write_bytes(b"custom dump")
                return backup_restore.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[0] == "pg_restore":
                return backup_restore.subprocess.CompletedProcess(argv, 0, stdout="1; TABLE public.jobs\n2; TABLE DATA public.jobs\n", stderr="")
            raise AssertionError(f"unexpected command: {argv}")

        original_run = backup_restore.subprocess.run
        backup_restore.subprocess.run = fake_run  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(backup_restore.subprocess, "run", original_run))

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "postgres-native.dump"
            result = backup_restore.export_native_postgres_dump(
                "postgresql+asyncpg://b1:secret-pass@postgres:5432/b1_ai_hub?sslmode=disable",
                output_path,
            )

            self.assertEqual(result["format"], backup_restore.NATIVE_POSTGRES_DUMP_FORMAT)
            self.assertEqual(result["verification"]["object_count"], 2)
            self.assertEqual(calls[0]["argv"][0], "pg_dump")
            self.assertEqual(calls[1]["argv"][0], "pg_restore")
            self.assertEqual(output_path.read_bytes(), b"custom dump")

    @unittest.skipIf(backup_restore.AESGCM is None, "cryptography is not installed in this lightweight test environment")
    def test_encrypted_backup_copy_and_encrypted_only_restore(self) -> None:
        key = "backup-key-" + ("k" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)

            copy_summary = backup_restore.create_backup(
                root,
                label="encrypted-copy",
                backup_encryption_key=key,
                backup_encryption_mode="copy",
            )
            copy_manifest = backup_restore.load_manifest(root / "backups" / "encrypted-copy")

            self.assertEqual(copy_summary["archive_encryption"]["mode"], "copy")
            self.assertTrue((root / "backups" / "encrypted-copy" / "payload.tar.gz").is_file())
            self.assertTrue((root / "backups" / "encrypted-copy" / "payload.tar.gz.aesgcm").is_file())
            self.assertEqual(copy_manifest["archive_encryption"]["plaintext_sha256"], copy_manifest["archive"]["sha256"])
            copy_verified = backup_restore.verify_backup(root / "backups", "encrypted-copy")
            self.assertEqual(copy_verified["status"], "verified")
            self.assertEqual(copy_verified["archive_encryption_verification"]["status"], "encrypted-archive-verified")

            only_summary = backup_restore.create_backup(
                root,
                label="encrypted-only",
                backup_encryption_key=key,
                backup_encryption_mode="encrypted-only",
            )
            self.assertEqual(only_summary["archive_encryption"]["mode"], "encrypted-only")
            self.assertFalse((root / "backups" / "encrypted-only" / "payload.tar.gz").exists())
            self.assertTrue((root / "backups" / "encrypted-only" / "payload.tar.gz.aesgcm").is_file())
            with self.assertRaises(backup_restore.BackupEncryptionError):
                backup_restore.verify_backup(root / "backups", "encrypted-only")
            with self.assertRaises(backup_restore.BackupEncryptionError):
                backup_restore.verify_backup(root / "backups", "encrypted-only", "wrong-key-" + ("x" * 64))

            verified = backup_restore.verify_backup(root / "backups", "encrypted-only", key)
            self.assertEqual(verified["archive_encryption"]["mode"], "encrypted-only")
            self.assertEqual(verified["archive_encryption_verification"]["status"], "encrypted-archive-verified")
            report = backup_restore.restore_backup_to_alternate(root / "backups", "encrypted-only", root / "restore-tests", backup_encryption_key=key)
            self.assertEqual(report["status"], "restored")
            self.assertEqual(report["archive_encryption_verification"]["status"], "encrypted-archive-verified")
            self.assertEqual((root / "restore-tests" / "encrypted-only" / "models" / "tts" / "custom.bin").read_bytes(), b"custom")

    def test_rejects_unsafe_backup_name_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.create_backup(root, label="../escape")

            (root / "models" / "tts" / "linked").symlink_to(root / "secrets" / "master_encryption_key")
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.create_backup(root, label="symlink")

    def test_restore_rejects_path_traversal_archive_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups" / "evil"
            backup_dir.mkdir(parents=True)
            archive_path = backup_dir / "payload.tar.gz"
            payload = b"escape"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            manifest = {
                "format": backup_restore.BACKUP_FORMAT,
                "name": "evil",
                "created_at": "2026-07-22T00:00:00+00:00",
                "archive": {
                    "file": "payload.tar.gz",
                    "size_bytes": archive_path.stat().st_size,
                    "sha256": backup_restore.sha256_file(archive_path),
                },
                "files": [{"path": "../escape", "size_bytes": len(payload), "sha256": "0" * 64}],
            }
            (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(backup_restore.RestoreError):
                backup_restore.restore_backup_to_alternate(root / "backups", "evil", root / "restore-tests")

    def test_restore_rejects_archive_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups" / "evil"
            backup_dir.mkdir(parents=True)
            archive_path = backup_dir / "payload.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("data/open-webui/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../secrets/master_encryption_key"
                archive.addfile(info)
            manifest = {
                "format": backup_restore.BACKUP_FORMAT,
                "name": "evil",
                "created_at": "2026-07-22T00:00:00+00:00",
                "archive": {
                    "file": "payload.tar.gz",
                    "size_bytes": archive_path.stat().st_size,
                    "sha256": backup_restore.sha256_file(archive_path),
                },
                "files": [],
            }
            (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(backup_restore.RestoreError, "unsupported archive member type"):
                backup_restore.restore_backup_to_alternate(root / "backups", "evil", root / "restore-tests")

    def test_restore_rejects_archive_file_parent_conflicts_before_extracting(self) -> None:
        cases = [
            ("parent-first", [("data", b"parent"), ("data/open-webui/db.sqlite", b"child")], "below a file member"),
            ("child-first", [("data/open-webui/db.sqlite", b"child"), ("data", b"parent")], "conflicts with existing child member"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for backup_name, members, expected in cases:
                with self.subTest(backup_name=backup_name):
                    backup_dir = root / "backups" / backup_name
                    backup_dir.mkdir(parents=True)
                    archive_path = backup_dir / "payload.tar.gz"
                    files = []
                    with tarfile.open(archive_path, "w:gz") as archive:
                        for member_name, payload in members:
                            info = tarfile.TarInfo(member_name)
                            info.size = len(payload)
                            archive.addfile(info, io.BytesIO(payload))
                            files.append(
                                {
                                    "path": member_name,
                                    "size_bytes": len(payload),
                                    "sha256": hashlib.sha256(payload).hexdigest(),
                                }
                            )
                    manifest = {
                        "format": backup_restore.BACKUP_FORMAT,
                        "name": backup_name,
                        "created_at": "2026-07-22T00:00:00+00:00",
                        "archive": {
                            "file": "payload.tar.gz",
                            "size_bytes": archive_path.stat().st_size,
                            "sha256": backup_restore.sha256_file(archive_path),
                        },
                        "files": files,
                    }
                    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

                    with self.assertRaisesRegex(backup_restore.RestoreError, expected):
                        backup_restore.restore_backup_to_alternate(root / "backups", backup_name, root / "restore-tests")
                    self.assertFalse((root / "restore-tests" / backup_name / "data").exists())

    def test_restore_rejects_preexisting_symlink_in_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            backup_restore.create_backup(root, label="unit")
            target = root / "restore-tests" / "unit"
            outside = root / "outside"
            outside.mkdir()
            (target / "data").mkdir(parents=True)
            (target / "data" / "open-webui").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(backup_restore.RestoreError, "symlink"):
                backup_restore.restore_backup_to_alternate(root / "backups", "unit", root / "restore-tests", force=True)

    def test_retention_plan_keeps_newest_and_applies_confirmed_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            for label, created_at in [
                ("old", datetime(2026, 7, 1, tzinfo=UTC)),
                ("middle", datetime(2026, 7, 10, tzinfo=UTC)),
                ("new", datetime(2026, 7, 20, tzinfo=UTC)),
            ]:
                backup_restore.create_backup(root, label=label)
                self.set_backup_created_at(root / "backups" / label, created_at)

            plan = backup_restore.build_backup_retention_plan(root / "backups", keep_last=1, now=datetime(2026, 7, 22, tzinfo=UTC))

            self.assertEqual(plan["status"], "planned")
            self.assertEqual([item["name"] for item in plan["candidates"]], ["middle", "old"])
            self.assertEqual([item["name"] for item in plan["kept"]], ["new"])
            self.assertGreater(plan["total_reclaimable_bytes"], 0)
            with self.assertRaisesRegex(backup_restore.BackupError, "explicit confirmation"):
                backup_restore.apply_backup_retention_plan(root / "backups", keep_last=1, now=datetime(2026, 7, 22, tzinfo=UTC))

            applied = backup_restore.apply_backup_retention_plan(
                root / "backups",
                keep_last=1,
                confirmed=True,
                now=datetime(2026, 7, 22, tzinfo=UTC),
            )

            self.assertEqual(applied["status"], "applied")
            self.assertEqual([item["name"] for item in applied["deleted"]], ["middle", "old"])
            self.assertTrue((root / "backups" / "new").is_dir())
            self.assertFalse((root / "backups" / "middle").exists())
            self.assertFalse((root / "backups" / "old").exists())

    def test_retention_plan_preserves_recent_age_threshold_and_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            for label, created_at in [
                ("old", datetime(2026, 7, 1, tzinfo=UTC)),
                ("recent", datetime(2026, 7, 18, tzinfo=UTC)),
                ("new", datetime(2026, 7, 21, tzinfo=UTC)),
            ]:
                backup_restore.create_backup(root, label=label)
                self.set_backup_created_at(root / "backups" / label, created_at)
            (root / "backups" / "linked").symlink_to(root / "secrets", target_is_directory=True)

            plan = backup_restore.build_backup_retention_plan(
                root / "backups",
                keep_last=1,
                delete_older_than_days=10,
                now=datetime(2026, 7, 22, tzinfo=UTC),
            )

            self.assertEqual([item["name"] for item in plan["candidates"]], ["old"])
            self.assertEqual([item["name"] for item in plan["kept"]], ["new", "recent"])
            self.assertEqual(plan["invalid_preserved_count"], 1)
            self.assertEqual(plan["invalid_preserved"][0]["name"], "linked")
            applied = backup_restore.apply_backup_retention_plan(
                root / "backups",
                keep_last=1,
                delete_older_than_days=10,
                confirmed=True,
                now=datetime(2026, 7, 22, tzinfo=UTC),
            )
            self.assertFalse((root / "backups" / "old").exists())
            self.assertTrue((root / "backups" / "recent").is_dir())
            self.assertTrue((root / "backups" / "new").is_dir())
            self.assertTrue((root / "backups" / "linked").is_symlink())


if __name__ == "__main__":
    unittest.main()
