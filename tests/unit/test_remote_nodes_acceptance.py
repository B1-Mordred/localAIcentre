from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.compatibility import test_remote_nodes_non_comfy as remote_nodes


class RemoteNodesAcceptanceHarnessTests(unittest.TestCase):
    def test_stop_verification_accepts_absent_or_exited_comfyui_containers(self) -> None:
        snapshot = remote_nodes.comfyui_stop_verification_from_runtime_inventory(
            {
                "runtime_agent_services": {
                    "services": [
                        {
                            "name": "comfyui",
                            "containers": [
                                {
                                    "short_id": "abc123",
                                    "name": "b1-ai-hub-comfyui-1",
                                    "state": "exited",
                                    "status": "Exited (0) 2 minutes ago",
                                    "image": "local/comfyui:test",
                                }
                            ],
                        }
                    ]
                }
            }
        )

        self.assertEqual(snapshot["service"], "comfyui")
        self.assertEqual(snapshot["container_count"], 1)
        self.assertEqual(snapshot["running_container_count"], 0)
        self.assertEqual(snapshot["verified_by"], "admin_runtimes_runtime_agent_services")

    def test_stop_verification_rejects_running_comfyui_container(self) -> None:
        with self.assertRaisesRegex(AssertionError, "still has running containers"):
            remote_nodes.comfyui_stop_verification_from_runtime_inventory(
                {
                    "runtime_agent_services": {
                        "services": [
                            {
                                "name": "comfyui",
                                "containers": [
                                    {
                                        "short_id": "abc123",
                                        "name": "b1-ai-hub-comfyui-1",
                                        "state": "running",
                                        "status": "Up 3 minutes",
                                        "image": "local/comfyui:test",
                                    }
                                ],
                            }
                        ]
                    }
                }
            )

    def test_stop_verification_rejects_missing_inventory_or_docker_error(self) -> None:
        with self.assertRaisesRegex(AssertionError, "runtime_agent_services"):
            remote_nodes.comfyui_stop_verification_from_runtime_inventory({})
        with self.assertRaisesRegex(AssertionError, "could not inspect"):
            remote_nodes.comfyui_stop_verification_from_runtime_inventory(
                {
                    "runtime_agent_services": {
                        "services": [
                            {
                                "name": "comfyui",
                                "containers": [],
                                "docker_error": "Docker socket is not mounted",
                            }
                        ]
                    }
                }
            )

    def test_downloaded_artifact_file_proof_requires_private_file_inside_output_dir(self) -> None:
        test_case = remote_nodes.RemoteNodesNonComfyCompatibilityTests(methodName="test_tts_fast_uses_unified_api_without_server_side_comfyui")
        content = b"RIFF....WAVEremote-node"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            artifact = output_dir / "speech.wav"
            artifact.write_bytes(content)
            artifact.chmod(0o600)

            proof = test_case.verify_downloaded_artifact_file(str(artifact), output_dir, len(content), digest)

        self.assertEqual(proof["filename"], "speech.wav")
        self.assertEqual(proof["relative_path"], "speech.wav")
        self.assertEqual(proof["byte_count"], len(content))
        self.assertEqual(proof["stat_size"], len(content))
        self.assertEqual(proof["sha256"], digest)
        self.assertEqual(proof["file_sha256"], digest)
        self.assertTrue(proof["path_within_download_dir"])
        self.assertFalse(proof["symlink"])
        self.assertTrue(proof["private_file_mode"])


if __name__ == "__main__":
    unittest.main()
