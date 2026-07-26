from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_PATH = ROOT / "deploy" / "scripts" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("b1_bootstrap", BOOTSTRAP_PATH)
bootstrap = importlib.util.module_from_spec(spec)
sys.modules["b1_bootstrap"] = bootstrap
assert spec.loader is not None
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    REQUIRED_PLAN_DIRS = {
        "data/postgres",
        "data/redis",
        "data/open-webui",
        "data/control-plane",
        "data/prometheus",
        "data/grafana",
        "data/voicebox",
        "models/llm",
        "models/vision",
        "models/embeddings",
        "models/diffusion/checkpoints",
        "models/diffusion/diffusion_models",
        "models/diffusion/text_encoders",
        "models/diffusion/vae",
        "models/diffusion/loras",
        "models/diffusion/controlnet",
        "models/diffusion/upscale_models",
        "models/video",
        "models/tts",
        "models/stt",
        "models/blobs",
        "workflows",
        "workflows/acceptance",
        "artifacts/images",
        "artifacts/audio",
        "artifacts/video",
        "artifacts/voicebox",
        "artifacts/temporary",
        "cache",
        "secrets",
        "logs",
        "backups",
    }
    REQUIRED_B1_MANAGED_DIRS = {
        "models/blobs/.partial",
        "models/runtime-views/localai",
        "models/runtime-views/comfyui",
        "models/runtime-views/voicebox",
        "models/runtime-views/audio-cpu",
        "models/quarantine/runtime-views",
        "models/quarantine/blobs",
        "restore-tests",
    }

    def test_bootstrap_generates_runtime_agent_token_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = bootstrap.bootstrap(root)
            second = bootstrap.bootstrap(root)
            token_path = root / "secrets" / "runtime_agent_token"
            runtime_control_token_path = root / "secrets" / "runtime_control_token"
            artifact_token_path = root / "secrets" / "artifact_server_token"
            open_webui_key_path = root / "secrets" / "open_webui_api_key"
            open_webui_secret_key_path = root / "secrets" / "open_webui_secret_key"
            self.assertIn("runtime_agent_token", first["created_secrets"])
            self.assertIn("runtime_control_token", first["created_secrets"])
            self.assertIn("artifact_server_token", first["created_secrets"])
            self.assertIn("open_webui_api_key", first["created_secrets"])
            self.assertIn("open_webui_secret_key", first["created_secrets"])
            self.assertIn("prometheus_scrape_token", first["created_secrets"])
            self.assertIn("grafana_admin_password", first["created_secrets"])
            self.assertIn("runtime_agent_mtls_ca.crt", first["created_secrets"])
            self.assertIn("runtime_agent_server.crt", first["created_secrets"])
            self.assertIn("runtime_agent_client.crt", first["created_secrets"])
            self.assertNotIn("runtime_agent_token", second["created_secrets"])
            self.assertNotIn("runtime_control_token", second["created_secrets"])
            self.assertNotIn("artifact_server_token", second["created_secrets"])
            self.assertNotIn("open_webui_api_key", second["created_secrets"])
            self.assertNotIn("open_webui_secret_key", second["created_secrets"])
            self.assertNotIn("prometheus_scrape_token", second["created_secrets"])
            self.assertNotIn("grafana_admin_password", second["created_secrets"])
            self.assertNotIn("runtime_agent_mtls_ca.crt", second["created_secrets"])
            self.assertTrue(token_path.read_text(encoding="utf-8").startswith("b1rt_"))
            self.assertTrue(runtime_control_token_path.read_text(encoding="utf-8").startswith("b1rctl_"))
            self.assertTrue(artifact_token_path.read_text(encoding="utf-8").startswith("b1art_"))
            self.assertTrue((root / "secrets" / "prometheus_scrape_token").read_text(encoding="utf-8").startswith("b1prom_"))
            self.assertGreaterEqual(len((root / "secrets" / "grafana_admin_password").read_text(encoding="utf-8").strip()), 32)
            open_webui_key = open_webui_key_path.read_text(encoding="utf-8").strip()
            self.assertTrue(open_webui_key.startswith("b1k_"))
            self.assertGreaterEqual(len(open_webui_secret_key_path.read_text(encoding="utf-8").strip()), 32)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(runtime_control_token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(artifact_token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(open_webui_key_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(open_webui_secret_key_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual((root / "data" / "open-webui").stat().st_mode & 0o777, 0o775)
            self.assertTrue((root / "secrets" / "caddy-certs").is_dir())
            self.assertEqual((root / "secrets" / "caddy-certs").stat().st_mode & 0o777, 0o750)
            for filename in bootstrap.RUNTIME_AGENT_MTLS_FILES.values():
                cert_path = root / "secrets" / filename
                self.assertTrue(cert_path.exists(), filename)
                self.assertEqual(cert_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual((root / "backups").stat().st_mode & 0o777, 0o770)
            self.assertEqual((root / "restore-tests").stat().st_mode & 0o777, 0o770)
            for relative in ("configuration", "backends", "data"):
                path = root / "data" / "localai" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("input", "user"):
                path = root / "data" / "comfyui" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("comfyui-output", "comfyui-temp"):
                path = root / "artifacts" / "temporary" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("generations", "profiles", "captures", "cache"):
                path = root / "data" / "voicebox" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for runtime_cache in ("localai", "comfyui", "voicebox"):
                path = root / "cache" / runtime_cache
                self.assertTrue(path.is_dir(), runtime_cache)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for monitoring_data_dir in ("prometheus", "grafana"):
                path = root / "data" / monitoring_data_dir
                self.assertTrue(path.is_dir(), monitoring_data_dir)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for runtime in ("localai", "comfyui", "voicebox", "audio-cpu"):
                self.assertTrue((root / "models" / "runtime-views" / runtime).is_dir())
                self.assertEqual((root / "models" / "runtime-views" / runtime).stat().st_mode & 0o777, 0o775)
            self.assertTrue((root / "models" / "quarantine" / "runtime-views").is_dir())

    def test_bootstrap_creates_complete_external_data_tree_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = bootstrap.bootstrap(root)

            missing = sorted(
                relative
                for relative in self.REQUIRED_PLAN_DIRS | self.REQUIRED_B1_MANAGED_DIRS
                if not (root / relative).is_dir()
            )
            self.assertEqual(missing, [])
            covered_dirs = set(bootstrap.DIRS)
            for relative in bootstrap.DIRS:
                parent = Path(relative).parent
                while str(parent) != ".":
                    covered_dirs.add(str(parent))
                    parent = parent.parent
            self.assertTrue(self.REQUIRED_PLAN_DIRS <= covered_dirs)
            self.assertTrue(self.REQUIRED_B1_MANAGED_DIRS <= covered_dirs)
            created_relative = {str(Path(path).relative_to(root)) for path in result["created_dirs"]}
            self.assertEqual(created_relative, set(bootstrap.DIRS))

    def test_acceptance_templates_are_valid_json_and_match_live_harness_contract(self) -> None:
        source = ROOT / "workflows" / "acceptance"
        missing = [filename for filename in bootstrap.ACCEPTANCE_TEMPLATE_FILES if not (source / filename).is_file()]
        self.assertEqual(missing, [])

        prompt = json.loads((source / "text-to-image-api-prompt.json").read_text(encoding="utf-8"))
        smoke_prompt = json.loads((source / "native-comfyui-smoke-prompt.json").read_text(encoding="utf-8"))
        image_job = json.loads((source / "image-generation-job.json").read_text(encoding="utf-8"))
        image_edit_job = json.loads((source / "image-edit-job.json").read_text(encoding="utf-8"))
        video_job = json.loads((source / "short-video-job.json").read_text(encoding="utf-8"))

        self.assertIn("prompt", prompt)
        self.assertIn("CheckpointLoaderSimple", {node.get("class_type") for node in prompt["prompt"].values()})
        self.assertIn("SaveImage", {node.get("class_type") for node in prompt["prompt"].values()})
        self.assertIn("B1RuntimeTinyImage", {node.get("class_type") for node in smoke_prompt["prompt"].values()})
        self.assertIn("SaveImage", {node.get("class_type") for node in smoke_prompt["prompt"].values()})

        for body, modality, operation, model in (
            (image_job, "image", "generation", "image-default"),
            (image_edit_job, "image", "edit", "image-edit"),
            (video_job, "video", "generation", "video-text"),
        ):
            self.assertEqual(body["modality"], modality)
            self.assertEqual(body["operation"], operation)
            self.assertEqual(body["model"], model)
            self.assertEqual(body["runtime_policy"], "any")
            self.assertIsInstance(body["input"], dict)
        self.assertIn("comfyui_prompt", image_job["input"])
        self.assertIn("comfyui_prompt", image_edit_job["input"])
        self.assertEqual(video_job["input"]["workflow_id"], "text-to-video")
        self.assertEqual(video_job["input"]["workflow_version"], "0.1.0")

    def test_acceptance_templates_copy_once_without_overwriting_operator_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data-root"
            source = Path(tmp) / "source"
            target = root / "workflows" / "acceptance"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            for filename in bootstrap.ACCEPTANCE_TEMPLATE_FILES:
                (source / filename).write_text(f"repo template: {filename}\n", encoding="utf-8")
            edited = target / "image-generation-job.json"
            edited.write_text("operator-edited\n", encoding="utf-8")

            created = bootstrap.copy_acceptance_templates(root, source=source)
            second = bootstrap.copy_acceptance_templates(root, source=source)

            created_relative = {str(Path(path).relative_to(root)) for path in created}
            expected = {
                f"workflows/acceptance/{filename}"
                for filename in bootstrap.ACCEPTANCE_TEMPLATE_FILES
                if filename != "image-generation-job.json"
            }
            self.assertEqual(created_relative, expected)
            self.assertEqual(second, [])
            self.assertEqual(edited.read_text(encoding="utf-8"), "operator-edited\n")
            for relative in expected:
                path = root / relative
                self.assertTrue(path.exists(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o664)


if __name__ == "__main__":
    unittest.main()
