from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> Any:
    path = ROOT / "deploy" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"b1_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"b1_{name}"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


acceptance_env = load_script("acceptance_env")
acceptance_preflight = load_script("acceptance_preflight")


class AcceptancePreflightTests(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "B1_ACCEPTANCE_API_KEY": "b1k_acceptance.secret",
            "B1_RESTART_RECONCILIATION_STARTED_AFTER": "2026-07-24T12:00:00+00:00",
            "B1_MODELHUB_SYNC_MODEL": "tts-fast",
            "B1_MODELHUB_INFERENCE_ONLY_MODEL": "chat-quality",
            "B1_SECURITY_BROWSER_USERNAME": "admin",
            "B1_SECURITY_BROWSER_PASSWORD": "correct horse battery staple",
            "B1_RUNTIME_DEPLOYMENT_MODE": "production",
            "B1_RUNTIME_PRODUCTION_REQUIRED": "localai,comfyui,audio-cpu,voicebox",
            "COMPOSE_FILE": "compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml:compose.production-voicebox.yaml",
            "COMPOSE_PROFILES": "voicebox",
            "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
            "B1_CPU_AUDIO_ENGINE": "piper",
            "B1_CPU_EMBEDDING_ENGINE": "onnx",
            "B1_CPU_STT_ENGINE": "vosk",
        }

    def write_json(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.strip() + "\n", encoding="utf-8")

    def write_operator_files(self, root: Path, *, tiny_prompt: bool = False, placeholder: bool = False) -> None:
        ca = root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        ca.parent.mkdir(parents=True, exist_ok=True)
        ca.write_text("test-ca\n", encoding="utf-8")
        workflows = root / "workflows" / "acceptance"
        if tiny_prompt:
            prompt = """
            {
              "prompt": {
                "1": {
                  "class_type": "B1RuntimeTinyImage",
                  "inputs": { "width": 64, "height": 64 }
                }
              }
            }
            """
        else:
            prompt = """
            {
              "prompt": {
                "4": {
                  "class_type": "CheckpointLoaderSimple",
                  "inputs": { "ckpt_name": "validated-checkpoint.safetensors" }
                }
              }
            }
            """
        self.write_json(workflows / "text-to-image-api-prompt.json", prompt)
        checkpoint = "REPLACE_WITH_INSTALLED_CHECKPOINT.safetensors" if placeholder else "validated-checkpoint.safetensors"
        self.write_json(
            workflows / "image-generation-job.json",
            f"""
            {{
              "modality": "image",
              "operation": "generation",
              "model": "image-default",
              "input": {{
                "parameters": {{
                  "checkpoint_name": "{checkpoint}",
                  "prompt": "acceptance image"
                }},
                "comfyui_prompt": {{ "prompt": {{ "4": {{ "class_type": "CheckpointLoaderSimple", "inputs": {{ "ckpt_name": "{{{{checkpoint_name}}}}" }} }} }} }}
              }}
            }}
            """,
        )
        self.write_json(
            workflows / "image-edit-job.json",
            """
            {
              "modality": "image",
              "operation": "edit",
              "model": "image-edit",
              "input": {
                "parameters": {
                  "checkpoint_name": "validated-checkpoint.safetensors",
                  "source_image_name": "acceptance-input.png",
                  "prompt": "acceptance edit"
                }
              }
            }
            """,
        )
        self.write_json(
            workflows / "short-video-job.json",
            """
            {
              "modality": "video",
              "operation": "generation",
              "model": "video-text",
              "input": {
                "workflow_id": "text-to-video",
                "workflow_version": "0.1.0",
                "parameters": {
                  "prompt": "acceptance clip",
                  "frames": 8,
                  "steps": 12
                }
              }
            }
            """,
        )

    def generate_env_file(self, root: Path, *, license_accepted: bool = True) -> Path:
        output = root / "backups" / "acceptance" / "operator-live-acceptance.env"
        acceptance_env.generate_acceptance_env(
            acceptance_env.AcceptanceEnvConfig(data_root=root, output=output)
        )
        if license_accepted:
            with output.open("a", encoding="utf-8") as handle:
                handle.write('export B1_MODELHUB_ACCEPT_LICENSES="1"\n')
        return output

    def run_report(self, root: Path, env_file: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
        merged = {**self.base_env(), **(env or {})}
        loaded = acceptance_preflight.load_env_exports(env_file, merged)
        context = acceptance_preflight.PreflightContext(data_root=root, env=loaded, env_file=env_file)
        return acceptance_preflight.run_preflight(context)

    def check_by_name(self, report: dict[str, Any], name: str) -> dict[str, Any]:
        for check in report["checks"]:
            if check["name"] == name:
                return check
        raise AssertionError(f"missing preflight check {name!r}")

    def test_safe_env_parser_preserves_preexisting_key_and_loads_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = self.generate_env_file(root)
            loaded = acceptance_preflight.load_env_exports(env_file, {"B1_ACCEPTANCE_API_KEY": "b1k_shared.secret"})

        self.assertEqual(loaded["B1_AI_HUB_API_KEY"], "b1k_shared.secret")
        self.assertEqual(loaded["B1_MODELHUB_TOKEN"], "b1k_shared.secret")
        self.assertEqual(loaded["B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"], "false")
        self.assertEqual(loaded["B1_MODELHUB_ACCEPT_LICENSES"], "1")

    def test_build_context_keeps_process_overrides_after_loading_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = self.generate_env_file(root, license_accepted=False)
            with patch.dict(
                os.environ,
                {
                    "B1_ACCEPTANCE_API_KEY": "b1k_shared.secret",
                    "B1_MODELHUB_ACCEPT_LICENSES": "1",
                    "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP": "true",
                },
                clear=True,
            ):
                context = acceptance_preflight.build_context(Namespace(data_root=str(root), env_file=str(env_file)))

        self.assertEqual(context.env["B1_MODELHUB_ACCEPT_LICENSES"], "1")
        self.assertEqual(context.env["B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"], "true")
        self.assertEqual(context.env["B1_AI_HUB_API_KEY"], "b1k_shared.secret")

    def test_preflight_passes_with_edited_operator_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(self.check_by_name(report, "workflow_inputs")["status"], "ok")
        self.assertEqual(self.check_by_name(report, "production_topology")["status"], "ok")
        text = acceptance_preflight.human_report(report)
        self.assertNotIn("b1k_acceptance.secret", text)
        self.assertNotIn("correct horse battery staple", text)

    def test_json_output_is_private_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "format": acceptance_preflight.PREFLIGHT_FORMAT,
                "generated_at": "2026-07-24T12:00:00+00:00",
                "status": "ok",
                "summary": {"ok": 1, "warning": 0, "fail": 0},
                "checks": [],
            }
            output = root / "backups" / "acceptance" / "operator-preflight.json"
            written = acceptance_preflight.write_json_report(output, report)

            self.assertEqual(written, str(output))
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["format"], acceptance_preflight.PREFLIGHT_FORMAT)

    def test_json_output_refuses_symlink_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink creation is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.json"
            real.write_text("{}\n", encoding="utf-8")
            output = root / "operator-preflight.json"
            try:
                output.symlink_to(real)
            except OSError as exc:
                self.skipTest(f"cannot create symlink: {exc}")

            with self.assertRaises(acceptance_preflight.AcceptancePreflightError):
                acceptance_preflight.write_json_report(output, {"format": acceptance_preflight.PREFLIGHT_FORMAT})

    def test_preflight_fails_for_unedited_acceptance_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root, placeholder=True)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "fail")
        workflow = self.check_by_name(report, "workflow_inputs")
        self.assertEqual(workflow["status"], "fail")
        self.assertIn("unresolved_placeholder_paths", str(workflow))

    def test_preflight_rejects_tiny_comfy_prompt_for_final_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root, tiny_prompt=True)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "fail")
        workflow = self.check_by_name(report, "workflow_inputs")
        self.assertIn("B1RuntimeTinyImage", str(workflow))

    def test_preflight_fails_for_github_pat_used_as_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"B1_ACCEPTANCE_API_KEY": "github_pat_placeholder"})

        self.assertEqual(report["status"], "fail")
        api_keys = self.check_by_name(report, "api_keys")
        self.assertEqual(api_keys["status"], "fail")
        self.assertIn("wrong_type", api_keys["data"])

    def test_preflight_rejects_plain_http_open_webui_smoke_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"B1_SMOKE_OPEN_WEBUI_BASE": "http://ai.b1.germering"})

        self.assertEqual(report["status"], "fail")
        urls = self.check_by_name(report, "urls")
        self.assertEqual(urls["status"], "fail")
        self.assertIn("B1_SMOKE_OPEN_WEBUI_BASE", urls["data"]["insecure"])

    def test_preflight_rejects_development_runtime_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"B1_RUNTIME_DEPLOYMENT_MODE": "development"})

        self.assertEqual(report["status"], "fail")
        topology = self.check_by_name(report, "production_topology")
        self.assertEqual(topology["status"], "fail")
        self.assertTrue(topology["data"]["runtime_deployment_mode_mismatch"])

    def test_preflight_rejects_missing_production_compose_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(
                root,
                env_file,
                {"COMPOSE_FILE": "compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml"},
            )

        self.assertEqual(report["status"], "fail")
        topology = self.check_by_name(report, "production_topology")
        self.assertEqual(topology["status"], "fail")
        self.assertIn("compose.production-voicebox.yaml", topology["data"]["missing_compose_files"])

    def test_preflight_rejects_missing_voicebox_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"COMPOSE_PROFILES": ""})

        self.assertEqual(report["status"], "fail")
        topology = self.check_by_name(report, "production_topology")
        self.assertEqual(topology["status"], "fail")
        self.assertIn("voicebox", topology["data"]["missing_compose_profiles"])

    def test_preflight_rejects_scaffold_cpu_engines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(
                root,
                env_file,
                {
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true",
                    "B1_CPU_AUDIO_ENGINE": "scaffold",
                    "B1_CPU_EMBEDDING_ENGINE": "",
                    "B1_CPU_STT_ENGINE": "",
                },
            )

        self.assertEqual(report["status"], "fail")
        topology = self.check_by_name(report, "production_topology")
        self.assertEqual(topology["status"], "fail")
        failure_keys = {item["key"] for item in topology["data"]["cpu_engine_failures"]}
        self.assertEqual(
            failure_keys,
            {
                "B1_CPU_AUDIO_ENABLE_PLACEHOLDER",
                "B1_CPU_AUDIO_ENGINE",
                "B1_CPU_EMBEDDING_ENGINE",
                "B1_CPU_STT_ENGINE",
            },
        )


if __name__ == "__main__":
    unittest.main()
