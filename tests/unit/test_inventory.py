from __future__ import annotations

import importlib.util
import json
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
                "docker_volume_ls": json.dumps({"Name": "open-webui_data"}),
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

            def runner(command: list[str]) -> dict[str, Any]:
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
        self.assertEqual(report["host"]["listening_tcp"][0]["port"], 11434)
        self.assertEqual(report["host"]["gpu"]["devices"][0]["memory_total_mib"], 12288)
        self.assertTrue(any(item["path"].endswith("compose.yaml") for item in report["paths"]["compose_file_candidates"]))
        self.assertTrue(any(item["path"].endswith("webui.db") for item in report["paths"]["open_webui_database_candidates"]))
        self.assertTrue(any(item["path"].endswith("alternate.db") for item in report["paths"]["open_webui_database_candidates"]))
        webui_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"].endswith("webui.db"))
        self.assertTrue(webui_db["sqlite"]["readable"])
        self.assertEqual(webui_db["sqlite"]["table_counts"]["user"], 1)
        self.assertEqual(webui_db["sqlite"]["table_counts"]["chat"], 2)
        alternate_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"].endswith("alternate.db"))
        self.assertFalse(alternate_db["sqlite"]["readable"])
        b1_models = next(item for item in report["paths"]["model_directories"] if item["path"].endswith("/b1/models"))
        self.assertEqual(b1_models["scan"]["model_file_count"], 1)
        self.assertEqual(b1_models["scan"]["model_size_bytes"], len(b"model-bytes"))
        self.assertEqual(report["migration_readiness"]["model_storage"]["model_file_count"], 1)
        self.assertEqual(report["migration_readiness"]["open_webui"]["readable_sqlite_count"], 1)
        self.assertEqual(report["migration_readiness"]["open_webui"]["known_table_counts"]["webui.db"]["chat"], 2)
        self.assertIn(11434, report["migration_readiness"]["port_review"]["ports_requiring_review"])
        self.assertEqual(report["classification"]["volumes_with_ai_hints"][0]["Name"], "open-webui_data")
        self.assertEqual(report["classification"]["networks_with_ai_hints"][0]["Name"], "comfy_default")


if __name__ == "__main__":
    unittest.main()
