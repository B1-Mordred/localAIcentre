from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "comfyui-b1-remote-nodes"))

from comfyui_b1_remote_nodes import nodes  # noqa: E402


REMOTE_NODES_EVIDENCE_FORMAT = "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1"
REMOTE_NODES_REQUIRED_CHECKS = ("server_side_comfyui_stopped", "non_comfy_tts_completed", "artifact_downloaded")


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
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_REMOTE_NODES_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in REMOTE_NODES_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": REMOTE_NODES_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": nodes.api_base(),
                    "status": status,
                    "required_checks": list(REMOTE_NODES_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def test_tts_fast_uses_unified_api_without_server_side_comfyui(self) -> None:
        self.assertTrue(os.getenv("B1_AI_HUB_API_KEY"), "B1_AI_HUB_API_KEY is required")
        output_dir = Path(os.getenv("B1_AI_HUB_DOWNLOAD_DIR", "b1-artifacts")).resolve()
        model = os.getenv("B1_REMOTE_NODES_TTS_MODEL", "tts-fast")
        runtime_policy = "non_comfy_only"
        with server_side_comfyui_stopped_context():
            self.record_check(
                "server_side_comfyui_stopped",
                stop_mode=os.getenv("B1_REMOTE_NODES_COMFYUI_STOP_MODE", "").strip().lower(),
            )
            file_path, byte_count, digest = nodes.B1TextToSpeech().run(
                model,
                "B1 remote-node non-Comfy compatibility test.",
                "default",
                runtime_policy=runtime_policy,
                filename="b1-remote-node-non-comfy.wav",
            )
        self.record_check("non_comfy_tts_completed", model=model, runtime_policy=runtime_policy, byte_count=byte_count)
        self.assertEqual(Path(file_path).parent.resolve(), output_dir)
        self.assertGreater(byte_count, 0)
        self.assertEqual(len(digest), 64)
        self.assertTrue(Path(file_path).is_file())
        self.record_check("artifact_downloaded", filename=Path(file_path).name, byte_count=byte_count, sha256=digest)
        self.samples.append(
            {
                "label": "tts-fast-non-comfy",
                "model": model,
                "runtime_policy": runtime_policy,
                "output_filename": Path(file_path).name,
                "byte_count": byte_count,
                "sha256": digest,
            }
        )


if __name__ == "__main__":
    unittest.main()
