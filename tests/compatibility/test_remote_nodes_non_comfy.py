from __future__ import annotations

import os
import subprocess
import sys
import unittest
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import TracebackType


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "comfyui-b1-remote-nodes"))

from comfyui_b1_remote_nodes import nodes  # noqa: E402


class DockerComposeComfyUiStopper(AbstractContextManager["DockerComposeComfyUiStopper"]):
    service_name = "comfyui"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.was_running = False

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", *args],
            cwd=self.root,
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )

    def running_services(self) -> set[str]:
        completed = self.compose("ps", "--status", "running", "--services")
        return {line.strip() for line in completed.stdout.splitlines() if line.strip()}

    def assert_stopped(self) -> None:
        running = self.running_services()
        if self.service_name in running:
            raise AssertionError("server-side B1 ComfyUI service is still running")

    def __enter__(self) -> "DockerComposeComfyUiStopper":
        self.was_running = self.service_name in self.running_services()
        if self.was_running:
            self.compose("stop", self.service_name)
        self.assert_stopped()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        if self.was_running:
            self.compose("up", "-d", self.service_name)


def server_side_comfyui_stopped_context() -> AbstractContextManager[object]:
    mode = os.getenv("B1_REMOTE_NODES_COMFYUI_STOP_MODE", "").strip().lower()
    if mode in {"docker-compose", "compose"}:
        return DockerComposeComfyUiStopper(ROOT)
    if mode == "manual":
        return nullcontext()
    raise unittest.SkipTest(
        "set B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose to let the test stop/restore the B1 comfyui service, "
        "or manually stop it first and set B1_REMOTE_NODES_COMFYUI_STOP_MODE=manual"
    )


@unittest.skipUnless(os.getenv("B1_REMOTE_NODES_LIVE_TEST") == "1", "set B1_REMOTE_NODES_LIVE_TEST=1 to run live remote-node compatibility tests")
class RemoteNodesNonComfyCompatibilityTests(unittest.TestCase):
    def test_tts_fast_uses_unified_api_without_server_side_comfyui(self) -> None:
        self.assertTrue(os.getenv("B1_AI_HUB_API_KEY"), "B1_AI_HUB_API_KEY is required")
        output_dir = Path(os.getenv("B1_AI_HUB_DOWNLOAD_DIR", "b1-artifacts")).resolve()
        with server_side_comfyui_stopped_context():
            file_path, byte_count, digest = nodes.B1TextToSpeech().run(
                "tts-fast",
                "B1 remote-node non-Comfy compatibility test.",
                "default",
                runtime_policy="non_comfy_only",
                filename="b1-remote-node-non-comfy.wav",
            )
        self.assertEqual(Path(file_path).parent.resolve(), output_dir)
        self.assertGreater(byte_count, 0)
        self.assertEqual(len(digest), 64)
        self.assertTrue(Path(file_path).is_file())


if __name__ == "__main__":
    unittest.main()
