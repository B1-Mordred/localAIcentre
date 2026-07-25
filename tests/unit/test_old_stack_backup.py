from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OLD_STACK_BACKUP_PATH = ROOT / "deploy" / "scripts" / "old_stack_backup.py"
spec = importlib.util.spec_from_file_location("b1_old_stack_backup", OLD_STACK_BACKUP_PATH)
old_stack_backup = importlib.util.module_from_spec(spec)
sys.modules["b1_old_stack_backup"] = old_stack_backup
assert spec.loader is not None
spec.loader.exec_module(old_stack_backup)


class OldStackBackupTests(unittest.TestCase):
    def inventory_report(self, root: Path) -> dict[str, Any]:
        compose_file = root / "old-open-webui" / "compose.yaml"
        open_webui_data = root / "old-open-webui" / "data"
        model_dir = root / "models"
        ollama_unit = root / "systemd" / "ollama.service"
        ollama_drop_in = root / "systemd" / "ollama.service.d" / "override.conf"
        return {
            "format": "b1-ai-hub-host-inventory/v1",
            "created_at": "2026-07-22T12:00:00+00:00",
            "classification": {
                "old_ai_stack_candidates": [
                    {"container": "old-open-webui", "classification": "candidate-old-ai-stack-review-required"}
                ],
                "systemd_old_ai_stack_candidates": [
                    {"service": "ollama.service", "classification": "candidate-old-ai-stack-review-required"}
                ],
                "compose_projects": [
                    {"project": "old-ai", "classification": "candidate-old-ai-stack-review-required"},
                    {"project": "hermes", "classification": "preserve-unrelated"},
                ],
                "volumes_with_ai_hints": [{"Name": "open-webui_data"}],
            },
            "host": {
                "systemd_service_inspects": [
                    {
                        "service": "ollama.service",
                        "classification": "candidate-old-ai-stack-review-required",
                        "fragment_path": str(ollama_unit),
                        "drop_in_paths": [str(ollama_drop_in)],
                    }
                ]
            },
            "paths": {
                "compose_file_candidates": [{"path": str(compose_file), "exists": True, "type": "file"}],
                "open_webui_data_candidates": [{"path": str(open_webui_data), "exists": True, "type": "directory"}],
                "open_webui_data_roots": [
                    {"path": str(open_webui_data), "exists": True, "type": "directory"},
                    {"path": "/var/lib/docker/volumes/open-webui/_data", "exists": None, "error": "PermissionError: denied"},
                ],
                "open_webui_database_candidates": [{"path": str(open_webui_data / "webui.db"), "exists": True, "type": "file"}],
                "model_directories": [{"path": str(model_dir), "exists": True, "type": "directory"}],
            },
            "migration_readiness": {
                "hardware_profile": {
                    "profile": "rtx3060-32gb-initial",
                    "accepted": False,
                    "largest_gpu_vram_mib": 6144,
                    "host_total_ram_mib": 32168,
                    "warnings": ["largest detected GPU VRAM is 6144 MiB; required initial profile needs at least 12288 MiB"],
                },
                "port_review": {"ports_requiring_review": [80, 443, 11434]},
                "model_storage": {"model_file_count": 3, "model_size_bytes": 4096},
                "open_webui": {"database_candidate_count": 1, "readable_sqlite_count": 1},
                "open_webui_data_roots": {
                    "candidate_count": 2,
                    "existing_directory_count": 1,
                    "unreadable_or_unscannable": [
                        {"path": "/var/lib/docker/volumes/open-webui/_data", "reason": "PermissionError: denied"}
                    ],
                },
            },
        }

    def write_scope(self, path: Path, payload: dict[str, Any]) -> Path:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def reviewed_scope(self, root: Path, volume_data: Path) -> dict[str, Any]:
        return {
            "format": "b1-ai-hub-old-stack-backup-scope/v1",
            "operator_reviewed": True,
            "reviewed_by": "unit-test",
            "review_notes": "old Open WebUI only",
            "include_paths": [
                {"path": str(root / "old-open-webui" / "compose.yaml"), "reason": "old compose file"},
                {"path": str(root / "models"), "reason": "non-redownloadable old model metadata"},
            ],
            "include_docker_volumes": [{"name": "open-webui_data", "reason": "old Open WebUI data volume"}],
            "include_containers": [{"name": "old-open-webui", "reason": "container metadata for rollback"}],
            "include_systemd_services": [{"name": "ollama.service", "reason": "host Ollama service for rollback"}],
            "test_volume_mountpoint": str(volume_data),
        }

    def fake_runner(self, volume_data: Path):
        systemd_root = volume_data.parent / "systemd"

        def runner(command: list[str]) -> dict[str, Any]:
            if command[:3] == ["docker", "volume", "inspect"]:
                return {
                    "command": command,
                    "stdout": json.dumps([{"Name": command[3], "Mountpoint": str(volume_data)}]),
                    "stderr": "",
                    "returncode": 0,
                }
            if command[:2] == ["docker", "inspect"]:
                return {
                    "command": command,
                    "stdout": json.dumps(
                        [
                            {
                                "Name": f"/{command[2]}",
                                "Image": "ghcr.io/open-webui/open-webui:v0.6.30",
                                "Config": {
                                    "Env": ["OPEN_WEBUI_SECRET_KEY=unit-key", "PUID=1000"],
                                    "Labels": {"public": "kept", "api_key": "unit-api"},
                                    "Cmd": ["serve", "--token=unit-token"],
                                },
                            }
                        ]
                    ),
                    "stderr": "",
                    "returncode": 0,
                }
            if command[:2] == ["systemctl", "show"]:
                service_name = command[2]
                return {
                    "command": command,
                    "stdout": (
                        f"Id={service_name}\n"
                        f"Names={service_name}\n"
                        "Description=Ollama Service\n"
                        "LoadState=loaded\n"
                        "ActiveState=active\n"
                        "SubState=running\n"
                        f"FragmentPath={systemd_root / 'ollama.service'}\n"
                        f"DropInPaths={systemd_root / 'ollama.service.d' / 'override.conf'}\n"
                        "ExecStart={ argv[]=/usr/local/bin/ollama serve --token=unit-token ; }\n"
                        "User=ollama\n"
                        "Group=ollama\n"
                    ),
                    "stderr": "",
                    "returncode": 0,
                }
            raise AssertionError(f"unexpected command: {command}")

        return runner

    def make_sources(self, root: Path) -> Path:
        old_stack = root / "old-open-webui"
        old_stack.mkdir()
        (old_stack / "compose.yaml").write_text("services:\n  open-webui: {}\n", encoding="utf-8")
        (root / "models").mkdir()
        (root / "models" / "custom.gguf").write_bytes(b"model")
        volume_data = root / "docker-volume-open-webui"
        volume_data.mkdir()
        (volume_data / "webui.db").write_bytes(b"sqlite")
        systemd = root / "systemd"
        (systemd / "ollama.service.d").mkdir(parents=True)
        (systemd / "ollama.service").write_text(
            "[Service]\nExecStart=/usr/local/bin/ollama serve --token=unit-file-token\n",
            encoding="utf-8",
        )
        (systemd / "ollama.service.d" / "override.conf").write_text(
            "[Service]\nEnvironment=OLLAMA_HOST=0.0.0.0:11434\n",
            encoding="utf-8",
        )
        return volume_data

    def test_scope_template_lists_candidates_but_selects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = old_stack_backup.build_scope_template(
                self.inventory_report(root),
                now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )

        self.assertEqual(template["format"], "b1-ai-hub-old-stack-backup-scope/v1")
        self.assertFalse(template["operator_reviewed"])
        self.assertEqual(template["include_paths"], [])
        self.assertEqual(template["include_docker_volumes"], [])
        self.assertEqual(template["include_containers"], [])
        self.assertEqual(template["include_systemd_services"], [])
        self.assertEqual(template["candidates"]["old_ai_stack_container_names"], ["old-open-webui"])
        self.assertEqual(template["candidates"]["old_ai_stack_systemd_service_names"], ["ollama.service"])
        self.assertEqual(
            template["candidates"]["systemd_unit_paths"],
            [str(root / "systemd" / "ollama.service"), str(root / "systemd" / "ollama.service.d" / "override.conf")],
        )
        self.assertEqual(template["candidates"]["old_ai_stack_compose_projects"], ["old-ai"])
        self.assertEqual(template["candidates"]["ai_hint_volume_names"], ["open-webui_data"])
        self.assertEqual(template["candidates"]["open_webui_data_paths"], [str(root / "old-open-webui" / "data")])
        self.assertEqual(template["candidates"]["open_webui_unreadable_data_roots"], ["/var/lib/docker/volumes/open-webui/_data"])
        self.assertFalse(template["inventory_review"]["hardware_profile"]["accepted"])
        self.assertEqual(template["inventory_review"]["hardware_profile"]["largest_gpu_vram_mib"], 6144)
        self.assertEqual(template["inventory_review"]["port_review"]["ports_requiring_review"], [80, 443, 11434])
        self.assertEqual(template["inventory_review"]["model_storage"]["model_file_count"], 3)
        self.assertEqual(template["inventory_review"]["open_webui"]["readable_sqlite_count"], 1)
        self.assertEqual(template["inventory_review"]["open_webui_data_roots"]["candidate_count"], 2)

    def test_backup_requires_operator_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = {
                "format": "b1-ai-hub-old-stack-backup-scope/v1",
                "operator_reviewed": False,
                "reviewed_by": "",
                "include_paths": [],
                "include_docker_volumes": [],
                "include_containers": [],
            }
            scope_path = self.write_scope(root / "scope.json", scope)

            with self.assertRaisesRegex(old_stack_backup.OldStackBackupError, "operator_reviewed"):
                old_stack_backup.backup_old_stack(scope_path=scope_path, output_root=root / "backups", label="unit")

    def test_backup_rejects_unsafe_systemd_service_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = {
                "format": "b1-ai-hub-old-stack-backup-scope/v1",
                "operator_reviewed": True,
                "reviewed_by": "unit-test",
                "include_paths": [],
                "include_docker_volumes": [],
                "include_containers": [],
                "include_systemd_services": [{"name": "ollama.service;rm", "reason": "bad"}],
            }
            scope_path = self.write_scope(root / "scope.json", scope)

            with self.assertRaisesRegex(old_stack_backup.OldStackBackupError, "unsafe systemd service name"):
                old_stack_backup.backup_old_stack(scope_path=scope_path, output_root=root / "backups", label="unit")

    def test_backup_copies_explicit_paths_volumes_and_container_metadata_then_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volume_data = self.make_sources(root)
            scope_path = self.write_scope(root / "scope.json", self.reviewed_scope(root, volume_data))

            backup_dir = old_stack_backup.backup_old_stack(
                scope_path=scope_path,
                output_root=root / "backups",
                label="unit",
                command_runner=self.fake_runner(volume_data),
                now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )
            report = old_stack_backup.verify_backup(backup_dir)
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(report["status"], "verified")
            self.assertTrue(manifest["contains_sensitive_data"])
            self.assertFalse(manifest["safety"]["old_stack_deletion_allowed"])
            archive_names = {item["archive_path"] for item in manifest["files"]}
            self.assertIn("docker-volumes/open-webui_data/webui.db", archive_names)
            self.assertIn("docker-inspect/containers/old-open-webui.json", archive_names)
            self.assertIn("docker-inspect-redacted/containers/old-open-webui.json", archive_names)
            self.assertIn("systemd/services/ollama.service.show", archive_names)
            self.assertIn("systemd/services-redacted/ollama.service.show", archive_names)
            self.assertTrue(any(name.startswith("systemd/units/ollama.service/") and name.endswith("-ollama.service") for name in archive_names))
            self.assertTrue(any(name.startswith("systemd/units/ollama.service/") and name.endswith("-override.conf") for name in archive_names))
            self.assertTrue(any(name.endswith("/compose.yaml") for name in archive_names))
            self.assertTrue(any(name.endswith("/custom.gguf") for name in archive_names))
            file_records = {item["archive_path"]: item for item in manifest["files"]}
            self.assertTrue(file_records["docker-inspect/containers/old-open-webui.json"]["sensitive"])
            self.assertFalse(file_records["docker-inspect-redacted/containers/old-open-webui.json"]["sensitive"])
            self.assertTrue(file_records["systemd/services/ollama.service.show"]["sensitive"])
            self.assertFalse(file_records["systemd/services-redacted/ollama.service.show"]["sensitive"])
            self.assertTrue(
                all(
                    file_records[name]["sensitive"]
                    for name in archive_names
                    if name.startswith("systemd/units/ollama.service/")
                )
            )
            sources = {item["type"]: item for item in manifest["sources"]}
            self.assertEqual(
                sources["docker_container_metadata"]["redacted_review_archive_path"],
                "docker-inspect-redacted/containers/old-open-webui.json",
            )
            self.assertEqual(sources["systemd_service_metadata"]["redacted_review_archive_path"], "systemd/services-redacted/ollama.service.show")
            self.assertEqual(len(sources["systemd_service_metadata"]["unit_file_archive_paths"]), 2)
            with tarfile.open(backup_dir / "payload.tar.gz", "r:gz") as archive:
                self.assertIn("docker-volumes/open-webui_data/webui.db", archive.getnames())
                redacted_member = archive.extractfile("docker-inspect-redacted/containers/old-open-webui.json")
                self.assertIsNotNone(redacted_member)
                assert redacted_member is not None
                redacted_payload = json.loads(redacted_member.read().decode("utf-8"))
                self.assertEqual(redacted_payload[0]["Config"]["Env"], ["OPEN_WEBUI_SECRET_KEY=<redacted>", "PUID=<redacted>"])
                self.assertEqual(redacted_payload[0]["Config"]["Labels"]["api_key"], "<redacted>")
                self.assertEqual(redacted_payload[0]["Config"]["Labels"]["public"], "kept")
                self.assertEqual(redacted_payload[0]["Config"]["Cmd"], ["serve", "--token=<redacted>"])
                service_show = archive.extractfile("systemd/services-redacted/ollama.service.show")
                self.assertIsNotNone(service_show)
                assert service_show is not None
                self.assertNotIn("unit-token", service_show.read().decode("utf-8"))

    def test_backup_rejects_symlinked_old_stack_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volume_data = self.make_sources(root)
            (root / "models" / "secret-link").symlink_to(root / "old-open-webui" / "compose.yaml")
            scope_path = self.write_scope(root / "scope.json", self.reviewed_scope(root, volume_data))

            with self.assertRaisesRegex(old_stack_backup.OldStackBackupError, "symlink"):
                old_stack_backup.backup_old_stack(
                    scope_path=scope_path,
                    output_root=root / "backups",
                    label="unit",
                    command_runner=self.fake_runner(volume_data),
                )

    def test_verify_rejects_tampered_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volume_data = self.make_sources(root)
            scope_path = self.write_scope(root / "scope.json", self.reviewed_scope(root, volume_data))
            backup_dir = old_stack_backup.backup_old_stack(
                scope_path=scope_path,
                output_root=root / "backups",
                label="unit",
                command_runner=self.fake_runner(volume_data),
            )
            with (backup_dir / "payload.tar.gz").open("ab") as handle:
                handle.write(b"tamper")

            with self.assertRaisesRegex(old_stack_backup.OldStackBackupError, "checksum"):
                old_stack_backup.verify_backup(backup_dir)


if __name__ == "__main__":
    unittest.main()
