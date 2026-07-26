from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "deploy" / "scripts" / "acceptance_env.py"
spec = importlib.util.spec_from_file_location("b1_acceptance_env", SCRIPT_PATH)
acceptance_env = importlib.util.module_from_spec(spec)
sys.modules["b1_acceptance_env"] = acceptance_env
assert spec.loader is not None
spec.loader.exec_module(acceptance_env)


class AcceptanceEnvTests(unittest.TestCase):
    def test_render_contains_live_flags_paths_and_blank_operator_values(self) -> None:
        config = acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"))
        text = acceptance_env.render_acceptance_env(config)

        for key in acceptance_env.LIVE_FLAGS:
            self.assertIn(f'export {key}="${{{key}:-1}}"', text)
        for key, filename in acceptance_env.EVIDENCE_FILES.items():
            self.assertIn(f'export {key}="${{{key}:-/srv/example/backups/acceptance/{filename}}}"', text)
        self.assertIn(
            'export B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE="${B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE:-/srv/example/workflows/acceptance/text-to-image-api-prompt.json}"',
            text,
        )
        self.assertIn(
            'export B1_WORKFLOWS_IMAGE_JOB_FILE="${B1_WORKFLOWS_IMAGE_JOB_FILE:-/srv/example/workflows/acceptance/image-generation-job.json}"',
            text,
        )
        self.assertIn('export B1_ACCEPTANCE_API_KEY="${B1_ACCEPTANCE_API_KEY:-}"', text)
        self.assertIn('export B1_SMOKE_OPEN_WEBUI_BASE="${B1_SMOKE_OPEN_WEBUI_BASE:-https://ai.b1.germering}"', text)
        self.assertIn('export B1_SMOKE_OPEN_WEBUI_HOST_HEADER="${B1_SMOKE_OPEN_WEBUI_HOST_HEADER:-}"', text)
        self.assertIn('export B1_ACCEPTANCE_ALLOW_INSECURE_HTTP="false"', text)
        self.assertIn('export B1_SMOKE_ALLOW_PLACEHOLDER="false"', text)
        self.assertIn('export B1_WORKFLOWS_ALLOW_PLACEHOLDER="false"', text)
        self.assertIn('export B1_REMOTE_NODES_ALLOW_PLACEHOLDER="false"', text)
        self.assertIn('export B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN="false"', text)
        self.assertIn('export B1_GPU_ACCEPTANCE_SKIP_VOICEBOX="false"', text)
        self.assertIn('export B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION="true"', text)
        self.assertIn('export B1_GPU_ACCEPTANCE_ENFORCE_VRAM_RESERVE="true"', text)
        self.assertIn('export B1_MODELHUB_ACCEPT_LICENSES="0"', text)
        self.assertIn('export B1_RUNTIME_DEPLOYMENT_MODE="${B1_RUNTIME_DEPLOYMENT_MODE:-}"', text)
        self.assertIn('export B1_RUNTIME_PRODUCTION_REQUIRED="${B1_RUNTIME_PRODUCTION_REQUIRED:-}"', text)
        self.assertIn('export COMPOSE_FILE="${COMPOSE_FILE:-}"', text)
        self.assertIn('export COMPOSE_PROFILES="${COMPOSE_PROFILES:-}"', text)
        self.assertIn('export B1_CPU_AUDIO_ENABLE_PLACEHOLDER="${B1_CPU_AUDIO_ENABLE_PLACEHOLDER:-}"', text)
        self.assertIn('export B1_CPU_AUDIO_ENGINE="${B1_CPU_AUDIO_ENGINE:-}"', text)
        self.assertIn('export B1_CPU_EMBEDDING_ENGINE="${B1_CPU_EMBEDDING_ENGINE:-}"', text)
        self.assertIn('export B1_CPU_STT_ENGINE="${B1_CPU_STT_ENGINE:-}"', text)
        self.assertIn('export B1_RESTART_RECONCILIATION_STARTED_AFTER="${B1_RESTART_RECONCILIATION_STARTED_AFTER:-}"', text)
        self.assertIn('export B1_MODELHUB_SYNC_MODEL="${B1_MODELHUB_SYNC_MODEL:-}"', text)
        self.assertNotIn("github_pat_", text)
        self.assertNotIn("b1k_", text)
        self.assertNotIn("Bearer ", text)

    def test_render_uses_configured_chat_host_for_open_webui_smoke(self) -> None:
        config = acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"), host_chat="chat.test.lan")
        text = acceptance_env.render_acceptance_env(config)

        self.assertIn('export B1_SMOKE_OPEN_WEBUI_BASE="${B1_SMOKE_OPEN_WEBUI_BASE:-https://chat.test.lan}"', text)

    def test_generated_file_is_sourceable_and_preserves_preexisting_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "acceptance.env"
            config = acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"), output=output)
            acceptance_env.generate_acceptance_env(config)

            command = (
                "unset B1_SMOKE_EVIDENCE B1_BACKUP_ROOT; "
                "B1_ACCEPTANCE_API_KEY=test-token; "
                f'. "{output}"; '
                'printf "%s\\n%s\\n%s\\n%s\\n" "$B1_AI_HUB_API_KEY" "$B1_MODELHUB_TOKEN" "$B1_SMOKE_EVIDENCE" "$B1_SMOKE_OPEN_WEBUI_BASE"'
            )
            result = subprocess.run(["bash", "-c", command], check=True, text=True, stdout=subprocess.PIPE)
            lines = result.stdout.splitlines()

        self.assertEqual(lines[0], "test-token")
        self.assertEqual(lines[1], "test-token")
        self.assertEqual(lines[2], "/srv/example/backups/acceptance/live-smoke.json")
        self.assertEqual(lines[3], "https://ai.b1.germering")

    def test_generate_writes_private_file_and_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "operator-live-acceptance.env"
            config = acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"), output=output)

            first = acceptance_env.generate_acceptance_env(config)
            with self.assertRaises(acceptance_env.AcceptanceEnvError):
                acceptance_env.generate_acceptance_env(config)
            forced = acceptance_env.generate_acceptance_env(
                acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"), output=output, force=True)
            )

            self.assertTrue(first["created"])
            self.assertFalse(forced["created"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)

    def test_generate_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.env"
            target.write_text("existing\n", encoding="utf-8")
            link = Path(tmp) / "acceptance.env"
            link.symlink_to(target)

            with self.assertRaises(acceptance_env.AcceptanceEnvError):
                acceptance_env.generate_acceptance_env(
                    acceptance_env.AcceptanceEnvConfig(data_root=Path("/srv/example"), output=link, force=True)
                )


if __name__ == "__main__":
    unittest.main()
