from __future__ import annotations

import json
import os
import re
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
REMOTE_NODES_REQUIRED_CHECKS = (
    "server_side_comfyui_stopped",
    "remote_models_listed",
    "model_alias_selected",
    "credentials_externalized",
    "non_comfy_tts_completed",
    "artifact_downloaded",
)
EXAMPLES_ROOT = ROOT / "integrations" / "comfyui-b1-remote-nodes" / "examples"
SECRET_VALUE_PATTERN = re.compile(r"\b(?:b1k_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|b1adm_[A-Za-z0-9_-]+|github_pat_[A-Za-z0-9_]+)\b")
FORBIDDEN_WORKFLOW_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
}


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


def workflow_secret_findings(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            text_key = str(key)
            normalized = text_key.lower().replace("-", "_")
            if any(fragment in normalized for fragment in FORBIDDEN_WORKFLOW_SECRET_KEYS):
                findings.append(f"{path}.{text_key}")
            findings.extend(workflow_secret_findings(child, f"{path}.{text_key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(workflow_secret_findings(child, f"{path}[{index}]"))
    elif isinstance(value, str) and SECRET_VALUE_PATTERN.search(value):
        findings.append(path)
    return findings


def configured_credential_source() -> str:
    if os.getenv(nodes.API_KEY_ENV):
        return "environment"
    if os.getenv(nodes.API_KEY_FILE_ENV):
        return "environment_file"
    config, _path = nodes.local_config_with_path()
    if config.get("api_key_file") or config.get(nodes.API_KEY_FILE_ENV):
        return "config_file_key_file"
    if config.get("api_key") or config.get(nodes.API_KEY_ENV):
        return "config_file_inline_private"
    return "none"


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
        self.assertTrue(nodes.api_key(), "configure a B1 API key through environment or private config")
        output_dir = Path(os.getenv("B1_AI_HUB_DOWNLOAD_DIR", "b1-artifacts")).resolve()
        model = os.getenv("B1_REMOTE_NODES_TTS_MODEL", "tts-fast")
        runtime_policy = "non_comfy_only"
        self.verify_credentials_externalized()
        with server_side_comfyui_stopped_context():
            self.record_check(
                "server_side_comfyui_stopped",
                stop_mode=os.getenv("B1_REMOTE_NODES_COMFYUI_STOP_MODE", "").strip().lower(),
            )
            aliases_json, raw_models_json = nodes.B1ListModels().run()
            aliases = json.loads(aliases_json)
            raw_models = json.loads(raw_models_json)
            self.assertIsInstance(aliases, list)
            self.assertTrue(aliases, "B1 remote nodes could not list any model aliases")
            self.assertIn(model, aliases, f"{model} was not visible in /v1/models")
            self.record_check(
                "remote_models_listed",
                alias_count=len(aliases),
                selected_model_visible=True,
                model=model,
                raw_object=raw_models.get("object"),
            )
            self.samples.append({"label": "remote-node-model-list", "alias_count": len(aliases), "selected_model_visible": True})
            selected_model = nodes.B1SelectModelAlias().run(model)[0]
            self.assertEqual(selected_model, model)
            self.record_check("model_alias_selected", model=selected_model)
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

    def verify_credentials_externalized(self) -> None:
        source = configured_credential_source()
        self.assertNotEqual(source, "none", "remote-node credentials must come from environment or private config")
        inspected_files: list[str] = []
        findings: list[str] = []
        for path in sorted(EXAMPLES_ROOT.glob("*.json")):
            inspected_files.append(path.name)
            payload = json.loads(path.read_text(encoding="utf-8"))
            findings.extend(f"{path.name}:{finding}" for finding in workflow_secret_findings(payload))
        self.assertFalse(findings, f"remote-node example workflows contain credential material: {findings}")
        self.record_check(
            "credentials_externalized",
            credential_source=source,
            inspected_workflow_count=len(inspected_files),
            inspected_workflows=inspected_files,
            workflow_secret_findings=[],
        )


if __name__ == "__main__":
    unittest.main()
