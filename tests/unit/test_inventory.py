from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = ROOT / "deploy" / "scripts" / "inventory.py"
spec = importlib.util.spec_from_file_location("b1_inventory", INVENTORY_PATH)
inventory = importlib.util.module_from_spec(spec)
sys.modules["b1_inventory"] = inventory
assert spec.loader is not None
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(inventory, name)
        setattr(inventory, name, value)
        self.addCleanup(lambda: setattr(inventory, name, original))

    def test_parsers_redact_and_normalize_host_outputs(self) -> None:
        self.assertEqual(inventory.redact_text("Authorization: Bearer b1rt_secret"), "Authorization: Bearer <redacted>")
        self.assertEqual(inventory.redact_text("token=abc123"), "token=<redacted>")
        self.assertEqual(json.loads(inventory.redact_text('{"Env":["OPENAI_API_KEY=abc123"]}'))["Env"][0], "OPENAI_API_KEY=<redacted>")

        listening = inventory.parse_listening_tcp(
            "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            "LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:* users:((\"ollama\",pid=1,fd=3))\n"
            "LISTEN 0 4096 [fd00::1]:8188 [::]:* users:((\"python\",pid=2,fd=3))\n"
        )
        self.assertEqual(listening[0]["port"], 11434)
        self.assertEqual(listening[1]["local_address"], "fd00::1")
        self.assertEqual(listening[1]["port"], 8188)
        review = inventory.analyze_listening_tcp(listening)
        self.assertIn(11434, review["ports_requiring_review"])
        self.assertEqual(review["legacy_comfy_listeners"][0]["port"], 8188)

        gpu = inventory.parse_nvidia_smi("0, NVIDIA GeForce RTX 3060, 555.42, 12288, 2048, 55, 12\n")
        self.assertEqual(gpu[0]["memory_total_mib"], 12288)
        self.assertEqual(gpu[0]["utilization_percent"], 12)

        memory = inventory.parse_free_mib("              total used free shared buff/cache available\nMem:          32168 1000 2000 0 0 29168\nSwap:          2048 0 2048\n")
        self.assertEqual(memory["mem"]["total_mib"], 32168)
        self.assertEqual(memory["mem"]["available_mib"], 29168)

        disks = inventory.parse_df("Filesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 ext4 1000 250 750 25% /\n")
        self.assertEqual(disks[0]["type"], "ext4")
        self.assertEqual(disks[0]["available_1k"], 750)

        dns = inventory.parse_dns_hosts("192.168.2.100 ai.b1.germering api.ai.b1.germering\n")
        self.assertEqual(dns["ai.b1.germering"], ["192.168.2.100"])

        roots = inventory.summarize_open_webui_data_roots(
            [
                {"path": "/var/lib/docker/volumes/open-webui/_data", "exists": None, "error": "PermissionError: denied"},
                {"path": "/srv/open-webui", "exists": True, "type": "directory"},
            ]
        )
        self.assertEqual(roots["candidate_count"], 2)
        self.assertEqual(roots["existing_directory_count"], 1)
        self.assertEqual(roots["unreadable_or_unscannable"][0]["path"], "/var/lib/docker/volumes/open-webui/_data")

        hardware = inventory.summarize_hardware_profile(
            [{"memory_total_mib": 6144, "name": "NVIDIA GeForce RTX 3060 Laptop GPU"}],
            {"mem": {"total_mib": 32168, "available_mib": 28000}},
        )
        self.assertFalse(hardware["accepted"])
        self.assertEqual(hardware["largest_gpu_vram_mib"], 6144)
        self.assertTrue(any("12288 MiB" in warning for warning in hardware["warnings"]))

    def test_build_inventory_classifies_old_ai_candidates_and_preserves_unrelated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b1_root = root / "b1"
            model_dir = b1_root / "models"
            (model_dir / "llm").mkdir(parents=True)
            (model_dir / "llm" / "chat-model.gguf").write_bytes(b"model-bytes")
            open_webui = b1_root / "data" / "open-webui"
            open_webui.mkdir(parents=True)
            with sqlite3.connect(open_webui / "webui.db") as connection:
                connection.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO user (id) VALUES ('user_1')")
                connection.execute("INSERT INTO chat (id) VALUES ('chat_1')")
                connection.execute("INSERT INTO chat (id) VALUES ('chat_2')")
            (open_webui / "alternate.db").write_text("sqlite", encoding="utf-8")
            volume_webui = root / "docker" / "volumes" / "open-webui" / "_data"
            volume_webui.mkdir(parents=True)
            with sqlite3.connect(volume_webui / "webui.db") as connection:
                connection.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO user (id) VALUES ('volume_user')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat_2')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat_3')")
            model_volume = root / "docker" / "volumes" / "ollama-models" / "_data"
            model_volume.mkdir(parents=True)
            (model_volume / "volume-model.gguf").write_bytes(b"volume-model")
            project = root / "old-stack"
            project.mkdir()
            (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

            outputs = {
                "docker_ps_all": "\n".join(
                    [
                        json.dumps({"ID": "1", "Names": "old-open-webui", "Image": "ghcr.io/open-webui/open-webui:v1", "Ports": "0.0.0.0:3000->8080/tcp"}),
                        json.dumps({"ID": "2", "Names": "hermes-bot", "Image": "example/hermes:1"}),
                        json.dumps({"ID": "3", "Names": "b1-ai-hub-control-plane-1", "Image": "b1/control-plane:test"}),
                    ]
                ),
                "docker_compose_ls": json.dumps({"Name": "openwebui", "Status": "running(1)", "ConfigFiles": str(project / "compose.yaml")}),
                "docker_volume_ls": "\n".join(
                    [
                        json.dumps({"Name": "open-webui"}),
                        json.dumps({"Name": "ollama-models"}),
                    ]
                ),
                "docker_network_ls": json.dumps({"Name": "comfy_default"}),
                "docker_info": json.dumps({"ServerVersion": "27.0", "Runtimes": {"runc": {}, "nvidia": {}}, "DefaultRuntime": "runc"}),
                "docker_version": json.dumps({"Client": {"Version": "27.0"}, "Server": {"Version": "27.0"}}),
                "listening_tcp": "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0 4096 0.0.0.0:11434 0.0.0.0:* users:((\"ollama\",pid=1,fd=3))\n",
                "nvidia_smi": "0, NVIDIA GeForce RTX 3060, 555.42, 12288, 2048, 55, 12\n",
                "nvidia_container_toolkit": "NVIDIA Container Toolkit CLI version 1.17.8\n",
                "free": "              total used free shared buff/cache available\nMem:          32168 1000 2000 0 0 29168\nSwap:          2048 0 2048\n",
                "df": "Filesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 ext4 1000 250 750 25% /\n",
                "mounts": json.dumps({"filesystems": [{"target": "/", "source": "/dev/sda1"}]}),
                "dns_hosts": "192.168.2.100 ai.b1.germering api.ai.b1.germering\n",
            }
            container_inspects = {
                "1": [
                    {
                        "Id": "1",
                        "Name": "/old-open-webui",
                        "Config": {
                            "Image": "ghcr.io/open-webui/open-webui:v1",
                            "Env": ["OPENAI_API_KEY=should-not-be-in-report"],
                            "Labels": {
                                "com.docker.compose.project": "openwebui",
                                "unsafe.secret.label": "should-not-be-in-report",
                            },
                        },
                        "State": {"Status": "running", "Running": True, "StartedAt": "2026-07-22T12:00:00Z"},
                        "Mounts": [
                            {
                                "Type": "volume",
                                "Name": "open-webui",
                                "Source": str(volume_webui),
                                "Destination": "/app/backend/data",
                                "Driver": "local",
                                "Mode": "z",
                                "RW": True,
                                "Propagation": "",
                            }
                        ],
                        "NetworkSettings": {"Networks": {"old-stack_default": {}}},
                    }
                ],
                "2": [
                    {
                        "Id": "2",
                        "Name": "/hermes-bot",
                        "Config": {"Image": "example/hermes:1", "Env": ["BOT_TOKEN=should-not-be-in-report"], "Labels": {}},
                        "State": {"Status": "running", "Running": True},
                        "Mounts": [],
                        "NetworkSettings": {"Networks": {}},
                    }
                ],
                "3": [
                    {
                        "Id": "3",
                        "Name": "/b1-ai-hub-control-plane-1",
                        "Config": {"Image": "b1/control-plane:test", "Env": ["B1_ADMIN_KEY=should-not-be-in-report"], "Labels": {}},
                        "State": {"Status": "running", "Running": True},
                        "Mounts": [],
                        "NetworkSettings": {"Networks": {}},
                    }
                ],
            }
            volume_inspects = {
                "open-webui": [{"Name": "open-webui", "Driver": "local", "Mountpoint": str(volume_webui), "Scope": "local"}],
                "ollama-models": [{"Name": "ollama-models", "Driver": "local", "Mountpoint": str(model_volume), "Scope": "local"}],
            }

            def runner(command: list[str]) -> dict[str, Any]:
                if command[:3] == ["docker", "container", "inspect"]:
                    payload = container_inspects.get(command[3], [])
                    return {"available": True, "command": command, "stdout": json.dumps(payload), "stderr": "", "returncode": 0}
                if command[:3] == ["docker", "volume", "inspect"]:
                    payload = volume_inspects.get(command[3], [])
                    return {"available": True, "command": command, "stdout": json.dumps(payload), "stderr": "", "returncode": 0}
                name = next(key for key, value in inventory.COMMANDS.items() if value == command)
                return {"available": True, "command": command, "stdout": outputs[name], "stderr": "", "returncode": 0}

            self.patch_attr("read_resolv_conf", lambda: {"path": "/etc/resolv.conf", "exists": True, "lines": ["nameserver 192.168.2.1"]})
            self.patch_attr("default_model_path_candidates", lambda b1_root: [b1_root / "models"])

            report = inventory.build_inventory(
                b1_root=b1_root,
                scan_roots=[root],
                command_runner=runner,
                now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )

        self.assertEqual(report["format"], "b1-ai-hub-host-inventory/v1")
        self.assertTrue(report["safety"]["read_only"])
        classifications = {item["container"]: item["classification"] for item in report["classification"]["containers"]}
        self.assertEqual(classifications["old-open-webui"], "candidate-old-ai-stack-review-required")
        self.assertEqual(classifications["hermes-bot"], "preserve-unrelated")
        self.assertEqual(classifications["b1-ai-hub-control-plane-1"], "b1-ai-hub-current-preserve")
        self.assertEqual(report["docker"]["info"]["nvidia_runtime_available"], True)
        report_json = json.dumps(report, sort_keys=True)
        self.assertNotIn("should-not-be-in-report", report_json)
        self.assertEqual(report["docker"]["container_inspects"][0]["mounts"][0]["source"], str(volume_webui))
        self.assertEqual(report["docker"]["volume_inspects"][0]["mountpoint"], str(volume_webui))
        data_roots = {item["path"]: item for item in report["paths"]["open_webui_data_roots"]}
        self.assertIn(str(volume_webui), data_roots)
        self.assertEqual(data_roots[str(volume_webui)]["type"], "directory")
        self.assertEqual(report["migration_readiness"]["open_webui_data_roots"]["existing_directory_count"], 2)
        self.assertEqual(report["host"]["listening_tcp"][0]["port"], 11434)
        self.assertEqual(report["host"]["gpu"]["devices"][0]["memory_total_mib"], 12288)
        self.assertTrue(report["migration_readiness"]["hardware_profile"]["accepted"])
        self.assertEqual(report["migration_readiness"]["hardware_profile"]["largest_gpu_vram_mib"], 12288)
        self.assertEqual(report["migration_readiness"]["hardware_profile"]["host_total_ram_mib"], 32168)
        self.assertTrue(any(item["path"].endswith("compose.yaml") for item in report["paths"]["compose_file_candidates"]))
        self.assertTrue(any(item["path"].endswith("webui.db") for item in report["paths"]["open_webui_database_candidates"]))
        self.assertTrue(any(item["path"].endswith("alternate.db") for item in report["paths"]["open_webui_database_candidates"]))
        self.assertTrue(any(item["path"] == str(volume_webui / "webui.db") for item in report["paths"]["open_webui_database_candidates"]))
        webui_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"].endswith("webui.db"))
        self.assertTrue(webui_db["sqlite"]["readable"])
        self.assertEqual(webui_db["sqlite"]["table_counts"]["user"], 1)
        self.assertEqual(webui_db["sqlite"]["table_counts"]["chat"], 2)
        volume_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"] == str(volume_webui / "webui.db"))
        self.assertEqual(volume_db["sqlite"]["table_counts"]["chat"], 3)
        alternate_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"].endswith("alternate.db"))
        self.assertFalse(alternate_db["sqlite"]["readable"])
        b1_models = next(item for item in report["paths"]["model_directories"] if item["path"].endswith("/b1/models"))
        self.assertEqual(b1_models["scan"]["model_file_count"], 1)
        self.assertEqual(b1_models["scan"]["model_size_bytes"], len(b"model-bytes"))
        docker_models = next(item for item in report["paths"]["model_directories"] if item["path"] == str(model_volume))
        self.assertEqual(docker_models["scan"]["model_file_count"], 1)
        self.assertEqual(report["migration_readiness"]["model_storage"]["model_file_count"], 2)
        self.assertEqual(report["migration_readiness"]["open_webui"]["readable_sqlite_count"], 2)
        b1_webui_path = str(open_webui / "webui.db")
        volume_webui_path = str(volume_webui / "webui.db")
        self.assertEqual(report["migration_readiness"]["open_webui"]["known_table_counts"][b1_webui_path]["chat"], 2)
        self.assertEqual(report["migration_readiness"]["open_webui"]["known_table_counts"][volume_webui_path]["chat"], 3)
        self.assertEqual(report["migration_readiness"]["open_webui"]["known_table_counts_by_path"][b1_webui_path]["chat"], 2)
        self.assertEqual(report["migration_readiness"]["open_webui"]["known_table_counts_by_path"][volume_webui_path]["chat"], 3)
        self.assertIn(11434, report["migration_readiness"]["port_review"]["ports_requiring_review"])
        hinted_volumes = {item["Name"] for item in report["classification"]["volumes_with_ai_hints"]}
        self.assertIn("open-webui", hinted_volumes)
        self.assertIn("ollama-models", hinted_volumes)
        self.assertEqual(report["classification"]["networks_with_ai_hints"][0]["Name"], "comfy_default")

    def test_write_private_json_protects_inventory_output_and_new_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "inventory.json"

            inventory.write_private_json(output, {"format": "b1-ai-hub-host-inventory/v1", "ok": True})

            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["format"], "b1-ai-hub-host-inventory/v1")
            if os.name != "nt":
                self.assertEqual((Path(tmp) / "nested").stat().st_mode & 0o777, 0o700)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "O_NOFOLLOW"), "symlink output refusal is POSIX-specific")
    def test_write_private_json_refuses_existing_output_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            link = root / "inventory.json"
            target.write_text("old", encoding="utf-8")
            link.symlink_to(target)

            with self.assertRaises(OSError):
                inventory.write_private_json(link, {"format": "b1-ai-hub-host-inventory/v1"})

            self.assertEqual(target.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
