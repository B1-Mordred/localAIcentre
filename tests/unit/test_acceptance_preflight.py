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
        self.write_backup_migration_files(root)

    def write_backup_migration_files(self, root: Path) -> dict[str, str]:
        b1_backup = root / "backups" / "b1-acceptance"
        self.write_json(
            b1_backup / "manifest.json",
            """
            {
              "format": "b1-ai-hub-backup/v1",
              "postgres_dump_included": true,
              "files": [{"path": "data/control-plane/postgres-logical-export.json"}]
            }
            """,
        )
        restore_report = root / "restore-tests" / "b1-acceptance" / "restore-report.json"
        self.write_json(
            restore_report,
            """
            {
              "status": "restored",
              "backup": "b1-acceptance",
              "files_verified": 1,
              "postgres_dump_included": true
            }
            """,
        )
        inventory = root / "backups" / "inventory-acceptance.json"
        self.write_json(
            inventory,
            """
            {
              "format": "b1-ai-hub-host-inventory/v1",
              "host": {
                "identity": {
                  "hostname": "ai",
                  "fqdn": "ai.b1.germering",
                  "platform_node": "ai"
                }
              },
              "migration_readiness": {
                "target_identity": {
                  "expected_target_host": "ai.b1.germering",
                  "expected_short_hostname": "ai",
                  "observed_hostname": "ai",
                  "observed_fqdn": "ai.b1.germering",
                  "observed_platform_node": "ai",
                  "hostname_matches_expected": true,
                  "fqdn_matches_expected": true,
                  "platform_node_matches_expected": true,
                  "accepted": true,
                  "operator_must_review_target_identity": false,
                  "warnings": []
                },
                "networking": {
                  "hostname_source": "system-hostname",
                  "network_property_source": "host-dhcp-client",
                  "b1_manages_host_networking": false,
                  "b1_static_ip_configures": false,
                  "expected_operator_networking": "host-managed DHCP lease/reservation plus LAN DNS records",
                  "non_loopback_address_count": 1,
                  "dynamic_address_count": 1,
                  "default_route_interfaces": ["eth0"],
                  "default_route_address_count": 1,
                  "default_route_count": 1,
                  "default_route_protocols": ["dhcp"],
                  "has_dhcp_default_route": true,
                  "dns_record_count": 7,
                  "operator_must_review_networking": false,
                  "warnings": []
                }
              },
              "classification": {"containers": []}
            }
            """,
        )
        old_stack = root / "backups" / "old-stack-acceptance"
        self.write_json(
            old_stack / "manifest.json",
            """
            {
              "format": "b1-ai-hub-old-stack-backup/v1",
              "safety": {"old_stack_deletion_allowed": false}
            }
            """,
        )
        open_webui_plan = root / "backups" / "open-webui-migration-plan.json"
        self.write_json(
            open_webui_plan,
            """
            {
              "format": "b1-ai-hub-open-webui-migration-plan/v1",
              "warnings": []
            }
            """,
        )
        cutover_plan = root / "backups" / "cutover-plan.json"
        self.write_json(
            cutover_plan,
            """
            {
              "format": "b1-ai-hub-cutover-plan/v1",
              "warnings": [],
              "target_identity_readiness": {
                "available": true,
                "expected_target_host": "ai.b1.germering",
                "expected_short_hostname": "ai",
                "observed_hostname": "ai",
                "observed_fqdn": "ai.b1.germering",
                "observed_platform_node": "ai",
                "hostname_matches_expected": true,
                "fqdn_matches_expected": true,
                "platform_node_matches_expected": true,
                "accepted": true,
                "operator_must_review_target_identity": false,
                "warnings": []
              },
              "networking_readiness": {
                "available": true,
                "hostname_source": "system-hostname",
                "network_property_source": "host-dhcp-client",
                "b1_manages_host_networking": false,
                "b1_static_ip_configures": false,
                "non_loopback_address_count": 1,
                "default_route_address_count": 1,
                "default_route_count": 1,
                "default_route_interfaces": ["eth0"],
                "default_route_protocols": ["dhcp"],
                "has_dhcp_default_route": true,
                "dns_record_count": 7,
                "operator_must_review_networking": false,
                "warnings": []
              },
              "safety": {
                "deletes_nothing": true,
                "old_stack_deletion_allowed": false
              }
            }
            """,
        )
        rollback_report = root / "backups" / "rollback-rehearsal.json"
        self.write_json(
            rollback_report,
            """
            {
              "format": "b1-ai-hub-rollback-rehearsal/v1",
              "status": "ok"
            }
            """,
        )
        return {
            "B1_BACKUP_DIR": str(b1_backup),
            "RESTORE_REPORT": str(restore_report),
            "INVENTORY": str(inventory),
            "OLD_STACK_BACKUP": str(old_stack),
            "OPEN_WEBUI_PLAN": str(open_webui_plan),
            "CUTOVER_PLAN": str(cutover_plan),
            "ROLLBACK_REPORT": str(rollback_report),
        }

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
        merged = {**self.base_env(), **self.backup_migration_env(root), **(env or {})}
        loaded = acceptance_preflight.load_env_exports(env_file, merged)
        context = acceptance_preflight.PreflightContext(data_root=root, env=loaded, env_file=env_file)
        return acceptance_preflight.run_preflight(context)

    def backup_migration_env(self, root: Path) -> dict[str, str]:
        return {
            "B1_BACKUP_DIR": str(root / "backups" / "b1-acceptance"),
            "RESTORE_REPORT": str(root / "restore-tests" / "b1-acceptance" / "restore-report.json"),
            "INVENTORY": str(root / "backups" / "inventory-acceptance.json"),
            "OLD_STACK_BACKUP": str(root / "backups" / "old-stack-acceptance"),
            "OPEN_WEBUI_PLAN": str(root / "backups" / "open-webui-migration-plan.json"),
            "CUTOVER_PLAN": str(root / "backups" / "cutover-plan.json"),
            "ROLLBACK_REPORT": str(root / "backups" / "rollback-rehearsal.json"),
        }

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
        self.assertEqual(self.check_by_name(report, "backup_migration_rollback_inputs")["status"], "ok")
        self.assertEqual(self.check_by_name(report, "production_topology")["status"], "ok")
        self.assertEqual(self.check_by_name(report, "target_network_policy")["status"], "ok")
        text = acceptance_preflight.human_report(report)
        self.assertNotIn("b1k_acceptance.secret", text)
        self.assertNotIn("correct horse battery staple", text)

    def test_preflight_stamps_source_metadata_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            with patch.dict(
                os.environ,
                {
                    "B1_SOURCE_COMMIT": "b" * 40,
                    "B1_SOURCE_REF": "agent/source-proof",
                    "B1_SOURCE_DIRTY": "false",
                    "B1_SOURCE_DIRTY_PATH_COUNT": "0",
                },
                clear=False,
            ):
                report = self.run_report(root, env_file)

        self.assertEqual(report["source"], "environment")
        self.assertEqual(report["source_commit"], "b" * 40)
        self.assertEqual(report["short_commit"], "b" * 12)
        self.assertEqual(report["source_ref"], "agent/source-proof")
        self.assertFalse(report["source_dirty"])
        self.assertEqual(report["dirty_path_count"], 0)

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

    def test_preflight_rejects_missing_backup_migration_rollback_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"B1_BACKUP_DIR": ""})

        self.assertEqual(report["status"], "fail")
        check = self.check_by_name(report, "backup_migration_rollback_inputs")
        self.assertEqual(check["status"], "fail")
        self.assertIn("B1_BACKUP_DIR", str(check["data"]))

    def test_preflight_rejects_bad_backup_migration_artifact_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            self.write_json(root / "backups" / "cutover-plan.json", '{"format": "wrong", "warnings": []}')
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "fail")
        check = self.check_by_name(report, "backup_migration_rollback_inputs")
        self.assertEqual(check["status"], "fail")
        self.assertIn("unsupported format wrong", str(check["data"]))

    def test_preflight_rejects_malformed_restore_report_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            self.write_json(
                root / "restore-tests" / "b1-acceptance" / "restore-report.json",
                '{"status": "restored", "files_verified": "many", "postgres_dump_included": true}',
            )
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "fail")
        check = self.check_by_name(report, "backup_migration_rollback_inputs")
        self.assertEqual(check["status"], "fail")
        self.assertIn("restore report does not record verified files", str(check["data"]))

    def test_preflight_rejects_inventory_without_dhcp_network_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            self.write_json(
                root / "backups" / "inventory-acceptance.json",
                """
                {
                  "format": "b1-ai-hub-host-inventory/v1",
                  "migration_readiness": {
                    "target_identity": {
                      "expected_target_host": "ai.b1.germering",
                      "expected_short_hostname": "ai",
                      "observed_hostname": "ai",
                      "observed_fqdn": "ai.b1.germering",
                      "observed_platform_node": "ai",
                      "hostname_matches_expected": true,
                      "fqdn_matches_expected": true,
                      "platform_node_matches_expected": true,
                      "accepted": true,
                      "operator_must_review_target_identity": false,
                      "warnings": []
                    },
                    "networking": {
                      "hostname_source": "system-hostname",
                      "network_property_source": "host-dhcp-client",
                      "b1_manages_host_networking": false,
                      "b1_static_ip_configures": false,
                      "non_loopback_address_count": 1,
                      "default_route_address_count": 1,
                      "default_route_count": 1,
                      "default_route_protocols": ["static"],
                      "has_dhcp_default_route": false,
                      "operator_must_review_networking": true,
                      "warnings": ["Inventory did not prove a DHCP-owned default route"]
                    }
                  },
                  "classification": {"containers": []}
                }
                """,
            )
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file)

        self.assertEqual(report["status"], "fail")
        check = self.check_by_name(report, "backup_migration_rollback_inputs")
        self.assertEqual(check["status"], "fail")
        self.assertIn("DHCP-owned default route", str(check["data"]))

    def test_preflight_rejects_static_legacy_comfy_listener_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(
                root,
                env_file,
                {
                    "B1_LEGACY_COMFY_PUBLISH": "192.168.2.100:8188:8188",
                    "B1_LEGACY_COMFY_BIND": "192.168.2.100",
                },
            )

        self.assertEqual(report["status"], "fail")
        policy = self.check_by_name(report, "target_network_policy")
        self.assertEqual(policy["status"], "fail")
        self.assertIn("static host IP", str(policy["data"]))

    def test_preflight_rejects_target_host_configured_as_ip_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_operator_files(root)
            env_file = self.generate_env_file(root)
            report = self.run_report(root, env_file, {"B1_EXPECTED_TARGET_HOST": "192.168.2.100"})

        self.assertEqual(report["status"], "fail")
        policy = self.check_by_name(report, "target_network_policy")
        self.assertEqual(policy["status"], "fail")
        self.assertIn("hostname/FQDN", str(policy["data"]))

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
