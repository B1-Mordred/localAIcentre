from __future__ import annotations

import importlib.util
import json
import os
import socket
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

    def temporary_unix_socket(self, root: Path) -> tuple[socket.socket, Path]:
        sock_path = root / "docker.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        os.chmod(sock_path, 0o660)
        return sock, sock_path

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

        services = inventory.parse_systemd_services(
            "  ollama.service loaded active running Ollama Service\n"
            "● hermes.service loaded active running Hermes worker\n"
            "  cron.service loaded active running Regular background program processing daemon\n"
        )
        self.assertEqual(services[0]["unit"], "ollama.service")
        self.assertEqual(services[0]["active_state"], "active")
        self.assertEqual(services[1]["unit"], "hermes.service")
        service_classes = {item["service"]: item["classification"] for item in map(inventory.classify_systemd_service, services)}
        self.assertEqual(service_classes["ollama.service"], "candidate-old-ai-stack-review-required")
        self.assertEqual(service_classes["hermes.service"], "preserve-unrelated")
        self.assertEqual(service_classes["cron.service"], "unknown-preserve-by-default")

        shown = inventory.parse_systemctl_show(
            "Id=ollama.service\n"
            "Names=ollama.service\n"
            "Description=Ollama Service\n"
            "FragmentPath=/etc/systemd/system/ollama.service\n"
            "DropInPaths=/etc/systemd/system/ollama.service.d/override.conf /etc/systemd/system/ollama.service.d/private.conf\n"
            "ExecStart={ argv[]=/usr/local/bin/ollama serve --token=secret-token ; }\n"
            "User=ollama\n"
        )
        self.assertEqual(shown["id"], "ollama.service")
        self.assertEqual(shown["drop_in_paths"][0], "/etc/systemd/system/ollama.service.d/override.conf")
        self.assertNotIn("secret-token", json.dumps(shown))

        gpu = inventory.parse_nvidia_smi("0, NVIDIA GeForce RTX 3060, 555.42, 12288, 2048, 55, 12\n")
        self.assertEqual(gpu[0]["memory_total_mib"], 12288)
        self.assertEqual(gpu[0]["utilization_percent"], 12)

        memory = inventory.parse_free_mib("              total used free shared buff/cache available\nMem:          32168 1000 2000 0 0 29168\nSwap:          2048 0 2048\n")
        self.assertEqual(memory["mem"]["total_mib"], 32168)
        self.assertEqual(memory["mem"]["available_mib"], 29168)

        disks = inventory.parse_df("Filesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 ext4 1000 250 750 25% /\n")
        self.assertEqual(disks[0]["type"], "ext4")
        self.assertEqual(disks[0]["available_1k"], 750)

        dns = inventory.parse_dns_hosts(
            "192.168.2.100 ai.b1.germering api.ai.b1.germering monitoring.ai.b1.germering\n"
        )
        self.assertEqual(dns["ai.b1.germering"], ["192.168.2.100"])
        self.assertEqual(dns["monitoring.ai.b1.germering"], ["192.168.2.100"])
        ahosts = inventory.parse_dns_ahosts(
            "192.168.2.100 STREAM ai.b1.germering\n"
            "192.168.2.100 DGRAM\n"
            "192.168.2.100 RAW\n"
            "::ffff:192.168.2.100 STREAM api.ai.b1.germering\n"
            "::ffff:192.168.2.100 DGRAM\n"
        )
        self.assertEqual(ahosts["ai.b1.germering"], ["192.168.2.100"])
        self.assertEqual(ahosts["api.ai.b1.germering"], ["192.168.2.100"])
        cutover_records = inventory.cutover_dns_records(
            {"ai.b1.germering": ["fd9b:4afc:bb00:1::100"]},
            {"ai.b1.germering": ["192.168.2.100"]},
        )
        self.assertEqual(cutover_records["ai.b1.germering"], ["192.168.2.100"])
        original_dns_admin_url = os.environ.get("B1_DNS_ADMIN_URL")
        try:
            os.environ["B1_DNS_ADMIN_URL"] = "http://technitium.b1.germering:5380"
            dns_admin = inventory.dns_admin_url_evidence()
            self.assertTrue(dns_admin["configured"])
            self.assertEqual(dns_admin["host"], "technitium.b1.germering")
            self.assertEqual(dns_admin["port"], 5380)
            os.environ["B1_DNS_ADMIN_URL"] = "http://technitium.b1.germering:bad"
            invalid_dns_admin = inventory.dns_admin_url_evidence()
            self.assertFalse(invalid_dns_admin["configured"])
            self.assertIn("invalid port", " ".join(invalid_dns_admin["warnings"]))
        finally:
            if original_dns_admin_url is None:
                os.environ.pop("B1_DNS_ADMIN_URL", None)
            else:
                os.environ["B1_DNS_ADMIN_URL"] = original_dns_admin_url
        interfaces = inventory.summarize_ip_interfaces(
            [
                {
                    "ifname": "eno1",
                    "operstate": "UP",
                    "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
                    "address": "00:11:22:33:44:55",
                    "addr_info": [
                        {
                            "family": "inet",
                            "local": "192.168.2.100",
                            "prefixlen": 24,
                            "scope": "global",
                            "dynamic": True,
                        }
                    ],
                },
                {"ifname": "lo", "flags": ["LOOPBACK"], "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}]},
            ]
        )
        routes = inventory.summarize_default_routes(
            [{"dst": "default", "gateway": "192.168.2.1", "dev": "eno1", "protocol": "dhcp", "metric": 100}],
            family="inet",
        )
        network = inventory.summarize_host_network(interfaces, routes, dns)
        self.assertEqual(network["hostname_authority"], "b1-appliance-config")
        self.assertEqual(network["hostname_source"], "system-hostname")
        self.assertEqual(
            network["expected_operator_networking"],
            "B1-defined system hostname plus host-managed DHCP lease/reservation and LAN DNS records",
        )
        self.assertFalse(network["b1_static_ip_configures"])
        self.assertTrue(network["has_dhcp_default_route"])
        self.assertEqual(network["non_loopback_address_count"], 1)
        self.assertEqual(network["default_route_interfaces"], ["eno1"])
        self.assertEqual(network["default_route_address_count"], 1)
        self.assertFalse(network["operator_must_review_networking"])
        target_identity = inventory.summarize_target_identity(
            {"hostname": "ai", "fqdn": "ai.b1.germering", "platform_node": "ai"},
            "ai.b1.germering",
        )
        self.assertEqual(target_identity["hostname_authority"], "b1-appliance-config")
        self.assertTrue(target_identity["accepted"])
        self.assertTrue(target_identity["hostname_matches_expected"])
        self.assertTrue(target_identity["fqdn_matches_expected"])
        mismatch_identity = inventory.summarize_target_identity(
            {"hostname": "b1-5", "fqdn": "b1-5", "platform_node": "b1-5"},
            "ai.b1.germering",
        )
        self.assertFalse(mismatch_identity["accepted"])
        self.assertTrue(mismatch_identity["operator_must_review_target_identity"])

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

        gpu_runtime = inventory.summarize_gpu_container_runtime(
            gpu_devices=[],
            nvidia_smi_available=False,
            docker_info={"nvidia_runtime_available": False, "runtimes": ["runc"], "default_runtime": "runc"},
            nvidia_toolkit={"available": True, "returncode": 127, "version": ""},
        )
        self.assertFalse(gpu_runtime["accepted"])
        self.assertTrue(gpu_runtime["operator_must_review_gpu_runtime"])
        self.assertIn("runc", gpu_runtime["docker_runtimes"])
        self.assertTrue(any("Docker does not report an nvidia runtime" in warning for warning in gpu_runtime["warnings"]))

    def test_docker_socket_readiness_records_group_gid_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sock, sock_path = self.temporary_unix_socket(root)
            self.addCleanup(sock.close)
            gid = sock_path.stat().st_gid

            ready = inventory.inspect_docker_socket(sock_path, configured_gid=str(gid))
            mismatch = inventory.inspect_docker_socket(sock_path, configured_gid=str(gid + 1))
            missing = inventory.inspect_docker_socket(root / "missing.sock", configured_gid=None)

        self.assertTrue(ready["exists"])
        self.assertTrue(ready["is_socket"])
        self.assertEqual(ready["mode_octal"], "0660")
        self.assertEqual(ready["gid"], gid)
        self.assertTrue(ready["configured_gid_matches"])
        self.assertTrue(ready["runtime_agent_group_access_ready"])
        self.assertEqual(ready["warnings"], [])
        self.assertFalse(mismatch["runtime_agent_group_access_ready"])
        self.assertTrue(any("does not match Docker socket GID" in warning for warning in mismatch["warnings"]))
        self.assertFalse(missing["exists"])
        self.assertTrue(any("is missing" in warning for warning in missing["warnings"]))

    def test_build_inventory_classifies_old_ai_candidates_and_preserves_unrelated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sock, sock_path = self.temporary_unix_socket(root)
            self.addCleanup(sock.close)
            docker_gid = sock_path.stat().st_gid
            b1_root = root / "b1"
            model_dir = b1_root / "models"
            (model_dir / "llm").mkdir(parents=True)
            (model_dir / "llm" / "chat-model.gguf").write_bytes(b"model-bytes")
            open_webui = b1_root / "data" / "open-webui"
            open_webui.mkdir(parents=True)
            with sqlite3.connect(open_webui / "webui.db") as connection:
                connection.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE document (id TEXT PRIMARY KEY, content TEXT)")
                connection.execute("CREATE TABLE file (id TEXT PRIMARY KEY, filename TEXT)")
                connection.execute("CREATE TABLE config (id TEXT PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO user (id) VALUES ('user_1')")
                connection.execute("INSERT INTO chat (id) VALUES ('chat_1')")
                connection.execute("INSERT INTO chat (id) VALUES ('chat_2')")
                connection.execute("INSERT INTO document (id, content) VALUES ('doc_1', 'private RAG document text')")
                connection.execute("INSERT INTO file (id, filename) VALUES ('file_1', 'secret-upload.pdf')")
                connection.execute("INSERT INTO config (id, value) VALUES ('cfg_1', 'private setting value')")
            (open_webui / "alternate.db").write_text("sqlite", encoding="utf-8")
            volume_webui = root / "docker" / "volumes" / "open-webui" / "_data"
            volume_webui.mkdir(parents=True)
            with sqlite3.connect(volume_webui / "webui.db") as connection:
                connection.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE knowledge (id TEXT PRIMARY KEY, name TEXT)")
                connection.execute("INSERT INTO user (id) VALUES ('volume_user')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat_2')")
                connection.execute("INSERT INTO chat (id) VALUES ('volume_chat_3')")
                connection.execute("INSERT INTO knowledge (id, name) VALUES ('knowledge_1', 'private knowledge base')")
            model_volume = root / "docker" / "volumes" / "ollama-models" / "_data"
            model_volume.mkdir(parents=True)
            (model_volume / "volume-model.gguf").write_bytes(b"volume-model")
            project = root / "old-stack"
            project.mkdir()
            (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

            dns_ahostsv4 = "\n".join(
                f"192.168.2.100 STREAM {host}" for host in inventory.CORE_INTENDED_HOSTS
            )
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
                "systemd_services": (
                    "  ollama.service loaded active running Ollama Service\n"
                    "  hermes.service loaded active running Hermes worker\n"
                    "  b1-ai-hub.service loaded inactive dead B1 AI Hub maintenance unit\n"
                    "  cron.service loaded active running Regular background program processing daemon\n"
                ),
                "listening_tcp": "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0 4096 0.0.0.0:11434 0.0.0.0:* users:((\"ollama\",pid=1,fd=3))\n",
                "nvidia_smi": "0, NVIDIA GeForce RTX 3060, 555.42, 12288, 2048, 55, 12\n",
                "nvidia_container_toolkit": "NVIDIA Container Toolkit CLI version 1.17.8\n",
                "free": "              total used free shared buff/cache available\nMem:          32168 1000 2000 0 0 29168\nSwap:          2048 0 2048\n",
                "df": "Filesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 ext4 1000 250 750 25% /\n",
                "mounts": json.dumps({"filesystems": [{"target": "/", "source": "/dev/sda1"}]}),
                "dns_hosts": "fd9b:4afc:bb00:1::100 ai.b1.germering\n",
                "dns_ahostsv4": dns_ahostsv4,
                "dns_ahostsv6": "fd9b:4afc:bb00:1::100 STREAM ai.b1.germering\n",
                "ip_addresses": json.dumps(
                    [
                        {
                            "ifname": "eno1",
                            "operstate": "UP",
                            "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
                            "address": "00:11:22:33:44:55",
                            "addr_info": [
                                {
                                    "family": "inet",
                                    "local": "192.168.2.100",
                                    "prefixlen": 24,
                                    "scope": "global",
                                    "dynamic": True,
                                }
                            ],
                        }
                    ]
                ),
                "ip_default_routes_v4": json.dumps(
                    [{"dst": "default", "gateway": "192.168.2.1", "dev": "eno1", "protocol": "dhcp", "prefsrc": "192.168.2.100", "metric": 100}]
                ),
                "ip_default_routes_v6": "[]",
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
            systemd_shows = {
                "ollama.service": (
                    "Id=ollama.service\n"
                    "Names=ollama.service\n"
                    "Description=Ollama Service\n"
                    "LoadState=loaded\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    f"FragmentPath={root / 'systemd' / 'ollama.service'}\n"
                    f"DropInPaths={root / 'systemd' / 'ollama.service.d' / 'override.conf'}\n"
                    "ExecStart={ argv[]=/usr/local/bin/ollama serve --token=should-not-be-in-report ; }\n"
                    "User=ollama\n"
                    "Group=ollama\n"
                ),
                "hermes.service": "Id=hermes.service\nDescription=Hermes worker\nActiveState=active\n",
                "b1-ai-hub.service": "Id=b1-ai-hub.service\nDescription=B1 AI Hub maintenance unit\nActiveState=inactive\n",
            }

            def runner(command: list[str]) -> dict[str, Any]:
                if command[:3] == ["docker", "container", "inspect"]:
                    payload = container_inspects.get(command[3], [])
                    return {"available": True, "command": command, "stdout": json.dumps(payload), "stderr": "", "returncode": 0}
                if command[:3] == ["docker", "volume", "inspect"]:
                    payload = volume_inspects.get(command[3], [])
                    return {"available": True, "command": command, "stdout": json.dumps(payload), "stderr": "", "returncode": 0}
                if command[:2] == ["systemctl", "show"]:
                    return {"available": True, "command": command, "stdout": systemd_shows.get(command[2], ""), "stderr": "", "returncode": 0}
                name = next(key for key, value in inventory.COMMANDS.items() if value == command)
                return {"available": True, "command": command, "stdout": outputs[name], "stderr": "", "returncode": 0}

            self.patch_attr("read_resolv_conf", lambda: {"path": "/etc/resolv.conf", "exists": True, "lines": ["nameserver 192.168.2.1"]})
            self.patch_attr("default_model_path_candidates", lambda b1_root: [b1_root / "models"])
            original_dns_admin_url = os.environ.get("B1_DNS_ADMIN_URL")
            os.environ["B1_DNS_ADMIN_URL"] = "http://technitium.b1.germering:5380"
            self.addCleanup(
                lambda: os.environ.pop("B1_DNS_ADMIN_URL", None)
                if original_dns_admin_url is None
                else os.environ.__setitem__("B1_DNS_ADMIN_URL", original_dns_admin_url)
            )
            self.patch_attr(
                "inspect_host_identity",
                lambda: {
                    "hostname": "ai",
                    "fqdn": "ai.b1.germering",
                    "platform_node": "ai",
                    "system": "Linux",
                    "release": "6.14.0",
                    "machine": "x86_64",
                    "python_version": "3.12.11",
                    "uid": 1000,
                    "gid": 1000,
                    "username": "mordred",
                },
            )

            report = inventory.build_inventory(
                b1_root=b1_root,
                scan_roots=[root],
                docker_socket_path=sock_path,
                configured_docker_gid=str(docker_gid),
                command_runner=runner,
                now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )

        self.assertEqual(report["format"], "b1-ai-hub-host-inventory/v1")
        self.assertTrue(report["safety"]["read_only"])
        self.assertEqual(report["host"]["identity"]["hostname"], "ai")
        self.assertEqual(report["host"]["identity"]["fqdn"], "ai.b1.germering")
        self.assertEqual(report["host"]["identity"]["machine"], "x86_64")
        self.assertEqual(report["migration_readiness"]["target_identity"]["expected_target_host"], "ai.b1.germering")
        self.assertTrue(report["migration_readiness"]["target_identity"]["accepted"])
        self.assertTrue(report["migration_readiness"]["target_identity"]["fqdn_matches_expected"])
        classifications = {item["container"]: item["classification"] for item in report["classification"]["containers"]}
        self.assertEqual(classifications["old-open-webui"], "candidate-old-ai-stack-review-required")
        self.assertEqual(classifications["hermes-bot"], "preserve-unrelated")
        self.assertEqual(classifications["b1-ai-hub-control-plane-1"], "b1-ai-hub-current-preserve")
        systemd_classifications = {item["service"]: item["classification"] for item in report["classification"]["systemd_services"]}
        self.assertEqual(systemd_classifications["ollama.service"], "candidate-old-ai-stack-review-required")
        self.assertEqual(systemd_classifications["hermes.service"], "preserve-unrelated")
        self.assertEqual(systemd_classifications["b1-ai-hub.service"], "b1-ai-hub-current-preserve")
        self.assertEqual(report["classification"]["systemd_old_ai_stack_candidates"][0]["service"], "ollama.service")
        self.assertEqual(report["docker"]["info"]["nvidia_runtime_available"], True)
        self.assertTrue(report["migration_readiness"]["gpu_container_runtime"]["accepted"])
        self.assertTrue(report["migration_readiness"]["gpu_container_runtime"]["docker_nvidia_runtime_available"])
        self.assertTrue(report["migration_readiness"]["gpu_container_runtime"]["nvidia_container_toolkit_available"])
        self.assertTrue(report["docker"]["socket"]["runtime_agent_group_access_ready"])
        self.assertEqual(report["migration_readiness"]["runtime_agent_docker_socket"]["gid"], docker_gid)
        report_json = json.dumps(report, sort_keys=True)
        self.assertNotIn("should-not-be-in-report", report_json)
        self.assertEqual(report["host"]["systemd_service_inspects"][0]["service"], "ollama.service")
        self.assertEqual(report["host"]["systemd_service_inspects"][0]["fragment_path"], str(root / "systemd" / "ollama.service"))
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
        self.assertEqual(report["host"]["dns"]["core_hosts"], list(inventory.CORE_INTENDED_HOSTS))
        self.assertEqual(report["host"]["dns"]["optional_hosts"], list(inventory.OPTIONAL_INTENDED_HOSTS))
        self.assertIn("monitoring.ai.b1.germering", report["host"]["dns"]["intended_hosts"])
        self.assertEqual(report["host"]["dns"]["records"]["ai.b1.germering"], ["192.168.2.100"])
        self.assertEqual(report["host"]["dns"]["records"]["api.ai.b1.germering"], ["192.168.2.100"])
        self.assertIn("fd9b:4afc:bb00:1::100", report["host"]["dns"]["records_all_sources"]["ai.b1.germering"])
        self.assertEqual(report["host"]["dns"]["records_by_source"]["getent_hosts"]["ai.b1.germering"], ["fd9b:4afc:bb00:1::100"])
        self.assertEqual(report["host"]["dns"]["dns_admin"]["host"], "technitium.b1.germering")
        self.assertEqual(report["host"]["dns"]["dns_admin"]["port"], 5380)
        self.assertEqual(report["host"]["network"]["interfaces"][0]["ifname"], "eno1")
        self.assertEqual(report["host"]["network"]["default_routes"][0]["protocol"], "dhcp")
        self.assertEqual(report["migration_readiness"]["target_identity"]["hostname_authority"], "b1-appliance-config")
        self.assertEqual(report["migration_readiness"]["networking"]["hostname_authority"], "b1-appliance-config")
        self.assertEqual(report["migration_readiness"]["networking"]["hostname_source"], "system-hostname")
        self.assertEqual(report["migration_readiness"]["networking"]["network_property_source"], "host-dhcp-client")
        self.assertFalse(report["migration_readiness"]["networking"]["b1_manages_host_networking"])
        self.assertFalse(report["migration_readiness"]["networking"]["b1_static_ip_configures"])
        self.assertEqual(report["migration_readiness"]["networking"]["default_route_interfaces"], ["eno1"])
        self.assertEqual(report["migration_readiness"]["networking"]["default_route_address_count"], 1)
        self.assertTrue(report["migration_readiness"]["networking"]["has_dhcp_default_route"])
        self.assertFalse(report["migration_readiness"]["networking"]["operator_must_review_networking"])
        self.assertTrue(any(item["path"].endswith("compose.yaml") for item in report["paths"]["compose_file_candidates"]))
        self.assertTrue(any(item["path"].endswith("webui.db") for item in report["paths"]["open_webui_database_candidates"]))
        self.assertTrue(any(item["path"].endswith("alternate.db") for item in report["paths"]["open_webui_database_candidates"]))
        self.assertTrue(any(item["path"] == str(volume_webui / "webui.db") for item in report["paths"]["open_webui_database_candidates"]))
        webui_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"].endswith("webui.db"))
        self.assertTrue(webui_db["sqlite"]["readable"])
        self.assertEqual(webui_db["sqlite"]["table_counts"]["user"], 1)
        self.assertEqual(webui_db["sqlite"]["table_counts"]["chat"], 2)
        self.assertEqual(webui_db["sqlite"]["table_counts"]["document"], 1)
        self.assertEqual(webui_db["sqlite"]["data_domains"]["documents_rag"]["known_row_count"], 2)
        self.assertEqual(webui_db["sqlite"]["data_domains"]["settings"]["known_row_count"], 1)
        volume_db = next(item for item in report["paths"]["open_webui_database_candidates"] if item["path"] == str(volume_webui / "webui.db"))
        self.assertEqual(volume_db["sqlite"]["table_counts"]["chat"], 3)
        self.assertEqual(volume_db["sqlite"]["data_domains"]["documents_rag"]["row_counts"]["knowledge"], 1)
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
        self.assertEqual(
            report["migration_readiness"]["open_webui"]["known_data_domains_by_path"][b1_webui_path]["documents_rag"]["known_row_count"],
            2,
        )
        self.assertEqual(report["migration_readiness"]["open_webui"]["data_domain_totals"]["documents_rag"]["database_count"], 2)
        self.assertEqual(report["migration_readiness"]["open_webui"]["data_domain_totals"]["documents_rag"]["known_row_count"], 3)
        self.assertNotIn("private RAG document text", report_json)
        self.assertNotIn("secret-upload.pdf", report_json)
        self.assertNotIn("private setting value", report_json)
        self.assertNotIn("private knowledge base", report_json)
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

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "O_NOFOLLOW"), "symlink output refusal is POSIX-specific")
    def test_main_refuses_symlink_output_without_resolving_to_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            link = root / "inventory.json"
            target.write_text("old", encoding="utf-8")
            link.symlink_to(target)
            self.assertEqual(inventory.absolute_path_without_symlink_resolution(str(link)), Path(os.path.abspath(str(link))))

            self.patch_attr("build_inventory", lambda **kwargs: {"format": "b1-ai-hub-host-inventory/v1"})
            original_argv = sys.argv
            sys.argv = ["inventory.py", "--output", str(link), "--b1-root", str(root)]
            self.addCleanup(lambda: setattr(sys, "argv", original_argv))

            with self.assertRaises(OSError):
                inventory.main()

            self.assertEqual(target.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
