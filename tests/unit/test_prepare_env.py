from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE_ENV_PATH = ROOT / "deploy" / "scripts" / "prepare_env.py"
spec = importlib.util.spec_from_file_location("b1_prepare_env", PREPARE_ENV_PATH)
prepare_env = importlib.util.module_from_spec(spec)
sys.modules["b1_prepare_env"] = prepare_env
assert spec.loader is not None
spec.loader.exec_module(prepare_env)


class PrepareEnvTests(unittest.TestCase):
    def unix_socket(self, path: Path) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        return sock

    def test_prepare_creates_production_env_with_detected_docker_gid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            template.write_text(
                "COMPOSE_FILE=compose.yaml\n"
                "B1_APPLIANCE_HOSTNAME=old.example\n"
                "B1_EXPECTED_TARGET_HOST=old.example\n"
                "B1_DOCKER_GID=0\n",
                encoding="utf-8",
            )
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            result = prepare_env.prepare_production_env(
                template=template,
                output=output,
                docker_socket=socket_path,
                expected_target_host="AI.B1.GERMERING.",
            )

            gid = os.stat(socket_path).st_gid
            content = output.read_text(encoding="utf-8")
            self.assertTrue(result["created"])
            self.assertEqual(result["docker_socket_gid"], gid)
            self.assertEqual(result["appliance_hostname"], "ai.b1.germering")
            self.assertEqual(result["expected_target_host"], "ai.b1.germering")
            self.assertEqual(result["hostname_authority"], "b1-appliance-config")
            self.assertEqual(result["network_property_source"], "host-dhcp-client")
            self.assertFalse(result["b1_static_ip_configures"])
            self.assertIn("B1_DOCKER_GID", result["updated_keys"])
            self.assertIn("B1_APPLIANCE_HOSTNAME=ai.b1.germering", content)
            self.assertIn("B1_EXPECTED_TARGET_HOST=ai.b1.germering", content)
            self.assertIn(f"B1_DOCKER_GID={gid}", content)
            self.assertNotIn("B1_STATIC_IP", content)
            self.assertNotIn("B1_GATEWAY", content)
            self.assertNotIn("B1_DNS_SERVERS", content)
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)

    def test_prepare_updates_existing_env_preserving_unmanaged_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            template.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            output.write_text(
                "CUSTOM_VALUE=kept\n"
                "B1_RUNTIME_PRODUCTION_REQUIRED=localai comfyui audio-cpu voicebox\n"
                "B1_DOCKER_GID=0\n",
                encoding="utf-8",
            )
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            result = prepare_env.prepare_production_env(
                template=template,
                output=output,
                docker_socket=socket_path,
                appliance_hostname="ai.b1.germering",
                update_existing=True,
            )

            content = output.read_text(encoding="utf-8")
            self.assertFalse(result["created"])
            self.assertIn("CUSTOM_VALUE=kept", content)
            self.assertIn("B1_RUNTIME_PRODUCTION_REQUIRED=localai,comfyui,audio-cpu,voicebox", content)
            self.assertIn("B1_RUNTIME_PRODUCTION_REQUIRED", result["updated_keys"])
            self.assertIn("B1_APPLIANCE_HOSTNAME=ai.b1.germering", content)
            self.assertIn("B1_EXPECTED_TARGET_HOST=ai.b1.germering", content)
            self.assertIn(f"B1_DOCKER_GID={os.stat(socket_path).st_gid}", content)

    def test_prepare_appends_missing_docker_gid_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            template.write_text("COMPOSE_FILE=compose.yaml\n", encoding="utf-8")
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            prepare_env.prepare_production_env(template=template, output=output, docker_socket=socket_path)

            self.assertTrue(output.read_text(encoding="utf-8").endswith(f"B1_DOCKER_GID={os.stat(socket_path).st_gid}\n"))

    def test_prepare_refuses_existing_env_without_update_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            template.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            output.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            with self.assertRaisesRegex(prepare_env.PrepareEnvError, "already exists"):
                prepare_env.prepare_production_env(template=template, output=output, docker_socket=socket_path)

    def test_prepare_refuses_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            target = root / "target.env"
            template.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            target.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            output.symlink_to(target)
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            with self.assertRaisesRegex(prepare_env.PrepareEnvError, "symlink"):
                prepare_env.prepare_production_env(
                    template=template,
                    output=output,
                    docker_socket=socket_path,
                    update_existing=True,
                )

    def test_prepare_refuses_symlinked_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            socket_path = root / "docker.sock"
            real_dir = root / "real"
            linked_dir = root / "linked"
            real_dir.mkdir()
            linked_dir.symlink_to(real_dir, target_is_directory=True)
            template.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            with self.assertRaisesRegex(prepare_env.PrepareEnvError, "symlinked directory"):
                prepare_env.prepare_production_env(
                    template=template,
                    output=linked_dir / ".env",
                    docker_socket=socket_path,
                )

    def test_prepare_requires_real_unix_socket_for_gid_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular_file = root / "docker.sock"
            regular_file.write_text("not a socket", encoding="utf-8")

            with self.assertRaisesRegex(prepare_env.PrepareEnvError, "not a Unix socket"):
                prepare_env.docker_socket_gid(regular_file)

    def test_expected_target_host_must_be_hostname_not_network_property(self) -> None:
        invalid_values = {
            "192.168.2.100": "not an IP address",
            "https://ai.b1.germering": "without scheme",
            "ai.b1.germering:443": "without scheme, path, or port",
            "bad_host": "invalid hostname labels",
        }
        for value, message in invalid_values.items():
            with self.subTest(value=value):
                with self.assertRaisesRegex(prepare_env.PrepareEnvError, message):
                    prepare_env.normalize_expected_target_host(value)

        self.assertEqual(prepare_env.normalize_expected_target_host("AI.B1.GERMERING."), "ai.b1.germering")
        self.assertEqual(prepare_env.normalize_expected_target_host("ai"), "ai")

    def test_prepare_rejects_conflicting_hostname_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / ".env.production.example"
            output = root / ".env"
            socket_path = root / "docker.sock"
            template.write_text("B1_DOCKER_GID=0\n", encoding="utf-8")
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            with self.assertRaisesRegex(prepare_env.PrepareEnvError, "must match"):
                prepare_env.prepare_production_env(
                    template=template,
                    output=output,
                    docker_socket=socket_path,
                    appliance_hostname="ai.b1.germering",
                    expected_target_host="other.b1.germering",
                )

    def test_prepared_production_template_is_shell_sourceable_for_preflights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / ".env"
            socket_path = root / "docker.sock"
            sock = self.unix_socket(socket_path)
            self.addCleanup(sock.close)

            prepare_env.prepare_production_env(
                template=ROOT / ".env.production.example",
                output=output,
                docker_socket=socket_path,
            )

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -euo pipefail; set -a; . "$1"; set +a; '
                    'test "$B1_DOCKER_GID" = "$2"; '
                    'test "$B1_RUNTIME_PRODUCTION_REQUIRED" = "localai,comfyui,audio-cpu,voicebox"',
                    "bash",
                    str(output),
                    str(os.stat(socket_path).st_gid),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
