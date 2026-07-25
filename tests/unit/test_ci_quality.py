from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHA_REF_RE = re.compile(r"@[0-9a-f]{40}\b")
IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")


def flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(flatten_strings(item))
        return flattened
    if isinstance(value, list):
        flattened = []
        for item in value:
            flattened.extend(flatten_strings(item))
        return flattened
    return []


class CiQualityGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_path = ROOT / ".github" / "workflows" / "ci.yaml"
        cls.workflow_text = cls.workflow_path.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.workflow_text)
        cls.workflow_strings = "\n".join(flatten_strings(cls.workflow))
        cls.makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
        cls.security_text = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")

    def test_expected_jobs_are_present(self) -> None:
        self.assertEqual(
            set(self.workflow["jobs"]),
            {"backend", "frontend", "security-and-sbom", "containers"},
        )

    def test_github_actions_are_pinned_to_immutable_commits(self) -> None:
        for job_name, job in self.workflow["jobs"].items():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if not uses:
                    continue
                with self.subTest(job=job_name, uses=uses):
                    self.assertRegex(uses, SHA_REF_RE)
                    self.assertNotRegex(uses, r"@v\d+\b")

    def test_github_runner_labels_are_fixed(self) -> None:
        for job_name, job in self.workflow["jobs"].items():
            with self.subTest(job=job_name):
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                self.assertNotIn("-latest", job["runs-on"])

    def test_backend_job_validates_compose_unit_tests_openapi_and_migrations(self) -> None:
        backend = self.workflow["jobs"]["backend"]
        commands = "\n".join(flatten_strings(backend))

        self.assertIn("make validate", commands)
        self.assertIn("make openapi-check", commands)
        self.assertIn("make openapi-client-check", commands)
        self.assertIn("tests.unit.test_migrations", commands)
        self.assertIn("deploy/scripts/bootstrap.py", commands)
        self.assertIn("PyYAML==6.0.2", commands)
        for path in (
            "services/control-plane/requirements.txt",
            "services/runtime-agent/requirements.txt",
            "services/artifact-server/requirements.txt",
            "services/audio-cpu/requirements.txt",
            "services/mock-runtime/requirements.txt",
        ):
            self.assertIn(f"-r {path}", commands)
            self.assertIn(path, self.makefile_text)

    def test_default_validation_runs_security_and_compatibility_harnesses(self) -> None:
        validate_line = next(
            line for line in self.makefile_text.splitlines() if line.startswith("validate:")
        )

        self.assertIn("unit", validate_line)
        self.assertIn("python-check", validate_line)
        self.assertIn("compatibility", validate_line)
        self.assertIn("security", validate_line)
        self.assertIn("openapi-client-check", validate_line)

    def test_makefile_exposes_live_acceptance_evidence_targets(self) -> None:
        expected_defaults = (
            ("B1_SMOKE_EVIDENCE", "live-smoke.json"),
            ("B1_WORKFLOWS_EVIDENCE", "installed-workflows.json"),
            ("B1_LOCALAI_ACCEPTANCE_EVIDENCE", "localai-runtime.json"),
            ("B1_GPU_ACCEPTANCE_EVIDENCE", "cross-runtime-gpu.json"),
            ("B1_RESTART_RECONCILIATION_EVIDENCE", "restart-reconciliation.json"),
            ("B1_NATIVE_COMFYUI_EVIDENCE", "native-comfyui.json"),
            ("B1_LEGACY_COMFY_EVIDENCE", "legacy-comfy-listener.json"),
            ("B1_REMOTE_NODES_EVIDENCE", "remote-nodes-non-comfy.json"),
            ("B1_MODELHUB_EVIDENCE", "modelhub-client-sync.json"),
            ("B1_VOICEBOX_EVIDENCE", "voicebox-remote.json"),
            ("B1_SECURITY_EVIDENCE", "security-acceptance.json"),
        )
        for variable, filename in expected_defaults:
            with self.subTest(variable=variable):
                self.assertIn(f"{variable} ?= $(B1_BACKUP_ROOT)/acceptance/{filename}", self.makefile_text)
        self.assertIn("B1_ACCEPTANCE_ENV ?= $(B1_BACKUP_ROOT)/acceptance/operator-live-acceptance.env", self.makefile_text)
        self.assertIn("\nacceptance-env:", self.makefile_text)
        self.assertIn('deploy/scripts/acceptance_env.py --data-root "$(B1_DATA_ROOT)" --output "$(B1_ACCEPTANCE_ENV)"', self.makefile_text)
        self.assertIn("\nacceptance-preflight:", self.makefile_text)
        self.assertIn('deploy/scripts/acceptance_preflight.py --data-root "$(B1_DATA_ROOT)" --env-file "$(B1_ACCEPTANCE_ENV)"', self.makefile_text)

        expected_targets = (
            ("live-smoke-acceptance", "B1_SMOKE_LIVE_TEST=1", "B1_SMOKE_EVIDENCE"),
            ("installed-workflows-acceptance", "B1_WORKFLOWS_LIVE_TEST=1", "B1_WORKFLOWS_EVIDENCE"),
            ("localai-acceptance", "B1_LOCALAI_ACCEPTANCE_LIVE_TEST=1", "B1_LOCALAI_ACCEPTANCE_EVIDENCE"),
            ("gpu-acceptance", "B1_GPU_ACCEPTANCE_LIVE_TEST=1", "B1_GPU_ACCEPTANCE_EVIDENCE"),
            (
                "restart-reconciliation-acceptance",
                "B1_RESTART_RECONCILIATION_LIVE_TEST=1",
                "B1_RESTART_RECONCILIATION_EVIDENCE",
            ),
            ("native-comfyui-compatibility", "B1_NATIVE_COMFYUI_LIVE_TEST=1", "B1_NATIVE_COMFYUI_EVIDENCE"),
            ("legacy-comfyui-compatibility", "B1_LEGACY_COMFY_LIVE_TEST=1", "B1_LEGACY_COMFY_EVIDENCE"),
            ("remote-nodes-non-comfy-compatibility", "B1_REMOTE_NODES_LIVE_TEST=1", "B1_REMOTE_NODES_EVIDENCE"),
            ("modelhub-compatibility", "B1_MODELHUB_LIVE_TEST=1", "B1_MODELHUB_EVIDENCE"),
            ("voicebox-compatibility", "B1_VOICEBOX_LIVE_TEST=1", "B1_VOICEBOX_EVIDENCE"),
            ("security-acceptance", "B1_SECURITY_LIVE_TEST=1", "B1_SECURITY_EVIDENCE"),
        )
        for target, live_flag, evidence_variable in expected_targets:
            with self.subTest(target=target):
                self.assertIn(f"\n{target}:", self.makefile_text)
                self.assertIn(live_flag, self.makefile_text)
                self.assertIn(f'{evidence_variable}="$({evidence_variable})"', self.makefile_text)

        self.assertIn(
            "external-compatibility-acceptance: native-comfyui-compatibility remote-nodes-non-comfy-compatibility "
            "modelhub-compatibility voicebox-compatibility",
            self.makefile_text,
        )
        self.assertIn("operator-live-acceptance:", self.makefile_text)
        self.assertIn("restart-reconciliation drill state documented in tests/.", self.makefile_text)

    def test_makefile_quality_target_collects_backend_schema_and_frontend_gates(self) -> None:
        target_start = self.makefile_text.index("quality:")
        target_end = self.makefile_text.index("\ncompose-config:", target_start)
        target = self.makefile_text[target_start:target_end]

        self.assertIn("B1_QUALITY_PYTHON ?= python3.12", self.makefile_text)
        quality_image_line = next(
            line for line in self.makefile_text.splitlines() if line.startswith("B1_QUALITY_PYTHON_IMAGE ?=")
        )
        self.assertIn("@sha256:", quality_image_line)
        self.assertNotIn(":latest", quality_image_line)
        self.assertIn("quality-local", target)
        self.assertIn("quality-container", target)
        self.assertIn("B1_QUALITY_PYTHON must be Python 3.12", target)
        self.assertIn("$(B1_QUALITY_PYTHON)\" -m venv", target)
        self.assertIn("$(B1_QUALITY_PYTHON_IMAGE)", target)
        self.assertIn("backend-python-quality-container", target)
        self.assertIn("python -m unittest discover -s tests/unit -v", target)
        self.assertIn("generate_openapi.py --output docs/openapi.json --check", target)
        self.assertIn("generate_openapi_client.py --check", target)
        self.assertIn("pip install PyYAML==6.0.2", target)
        self.assertIn("$(MAKE) validate openapi-check", target)
        self.assertIn("$(MAKE) frontend", target)

    def test_security_docs_describe_strong_local_quality_gate(self) -> None:
        self.assertIn("make quality", self.security_text)
        self.assertIn("B1_QUALITY_PYTHON_IMAGE", self.security_text)
        self.assertIn("committed OpenAPI/schema-client drift checks", self.security_text)
        self.assertIn("production NPM audits", self.security_text)

    def test_makefile_caddy_validation_image_is_digest_pinned(self) -> None:
        caddy_line = next(
            line for line in self.makefile_text.splitlines() if line.startswith("CADDY_IMAGE ?=")
        )
        image = caddy_line.split("?=", 1)[1].strip()

        self.assertIn("@sha256:", image)
        self.assertNotIn(":latest", image)

    def test_python_check_compiles_source_tree_without_writing_repo_bytecode(self) -> None:
        target_start = self.makefile_text.index("python-check:")
        target_end = self.makefile_text.index("\nunit:", target_start)
        target = self.makefile_text[target_start:target_end]

        self.assertIn("python3 -m compileall -q services deploy integrations tests", target)
        self.assertIn("PYTHONPYCACHEPREFIX", target)
        self.assertIn("mktemp -d", target)
        self.assertIn("rm -rf", target)

    def test_voicebox_audit_inventory_records_upstream_vulnerabilities_without_failing(self) -> None:
        target_start = self.makefile_text.index("voicebox-audit-inventory:")
        target_end = self.makefile_text.index("\ndb-migrate:", target_start)
        target = self.makefile_text[target_start:target_end]

        self.assertIn("B1_VOICEBOX_AUDIT_REPORT", self.makefile_text)
        self.assertIn("pip-audit --no-deps --disable-pip -r deploy/voicebox/constraints.txt", target)
        self.assertIn("|| true", target)
        self.assertIn("test -s", target)

    def test_frontend_job_builds_both_react_apps_and_audits_dependencies(self) -> None:
        frontend = self.workflow["jobs"]["frontend"]

        self.assertEqual(
            frontend["strategy"]["matrix"]["app"],
            ["web/control-center", "web/media-studio"],
        )
        commands = "\n".join(flatten_strings(frontend))
        self.assertIn("npm ci", commands)
        self.assertIn("npm run build", commands)
        self.assertIn("npm audit --omit=dev --audit-level=high", commands)

    def test_makefile_frontend_target_builds_and_audits_both_react_apps(self) -> None:
        target_start = self.makefile_text.index("frontend: frontend-control-center frontend-media-studio")
        target_end = self.makefile_text.index("\nunit:", target_start)
        target = self.makefile_text[target_start:target_end]

        for app in ("web/control-center", "web/media-studio"):
            self.assertIn(f"npm --prefix {app} ci", target)
            self.assertIn(f"npm --prefix {app} run build", target)
            self.assertIn(f"npm --prefix {app} audit --omit=dev --audit-level=high", target)

    def test_security_job_runs_secret_scan_sbom_validation_and_python_audit(self) -> None:
        security = self.workflow["jobs"]["security-and-sbom"]
        commands = "\n".join(flatten_strings(security))

        self.assertEqual(
            self.workflow["env"]["CYCLONEDX_CLI_IMAGE"],
            "cyclonedx/cyclonedx-cli:0.29.1@sha256:f025573a1dcc12971d711badf32bff1e030192a051a3a4987204fc6b79f91b6c",
        )
        self.assertRegex(self.workflow["env"]["CYCLONEDX_CLI_IMAGE"], IMAGE_DIGEST_RE)
        self.assertNotIn(":latest", self.workflow["env"]["CYCLONEDX_CLI_IMAGE"])
        self.assertIn("pip-audit==2.9.0", commands)
        self.assertIn("make secret-scan", commands)
        self.assertIn("make sbom", commands)
        self.assertIn("make voicebox-audit-inventory", commands)
        self.assertIn("validate --input-file /sbom/b1-ai-hub.cdx.json", commands)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", commands)
        self.assertIn("artifacts/pip-audit/voicebox-constraints.json", commands)
        self.assertIn("voicebox-pip-audit", commands)
        for path in (
            "services/control-plane/requirements.txt",
            "services/runtime-agent/requirements.txt",
            "services/artifact-server/requirements.txt",
            "services/audio-cpu/requirements.txt",
            "services/mock-runtime/requirements.txt",
        ):
            self.assertIn(f"pip-audit -r {path}", commands)

    def test_container_job_builds_and_scans_all_local_images(self) -> None:
        containers = self.workflow["jobs"]["containers"]
        steps = {step["name"]: step for step in containers["steps"] if "name" in step}
        commands = "\n".join(flatten_strings(containers))
        strict_image_scan = steps["Image vulnerability scan"]["run"]
        upstream_inventory = steps["Upstream-heavy image vulnerability inventory"]["run"]

        self.assertEqual(
            self.workflow["env"]["TRIVY_IMAGE"],
            "aquasec/trivy:0.66.0@sha256:086971aaf400beebd94e8300fd8ea623774419597169156cec56eec5b00dfb1e",
        )
        self.assertRegex(self.workflow["env"]["TRIVY_IMAGE"], IMAGE_DIGEST_RE)
        self.assertNotIn(":latest", self.workflow["env"]["TRIVY_IMAGE"])
        for image, context in (
            ("b1-ai-hub/control-plane:ci", "services/control-plane"),
            ("b1-ai-hub/runtime-agent:ci", "services/runtime-agent"),
            ("b1-ai-hub/artifact-server:ci", "services/artifact-server"),
            ("b1-ai-hub/audio-cpu:ci", "services/audio-cpu"),
            ("b1-ai-hub/mock-runtime:ci", "services/mock-runtime"),
            ("b1-ai-hub/control-center:ci", "web/control-center"),
            ("b1-ai-hub/media-studio:ci", "web/media-studio"),
            ("b1-ai-hub/open-webui-wrapper:ci", "deploy/open-webui"),
            ("b1-ai-hub/localai:ci", "deploy/localai"),
            ("b1-ai-hub/comfyui:ci", "deploy/comfyui"),
            ("b1-ai-hub/voicebox:ci", "deploy/voicebox"),
            ("b1-ai-hub/model-client:ci", "integrations/b1-model-client"),
        ):
            self.assertIn(f"docker build -t {image} {context}", commands)
            self.assertIn(image, commands)

        self.assertIn('"$TRIVY_IMAGE"', commands)
        self.assertIn("$RUNNER_TEMP/trivy-cache:/root/.cache/", commands)
        self.assertIn("fs --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed /repo", commands)
        self.assertIn("image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed", commands)
        self.assertNotIn("b1-ai-hub/open-webui-wrapper:ci", strict_image_scan)
        self.assertNotIn("b1-ai-hub/localai:ci", strict_image_scan)
        self.assertNotIn("b1-ai-hub/comfyui:ci", strict_image_scan)
        self.assertNotIn("b1-ai-hub/voicebox:ci", strict_image_scan)
        self.assertIn("b1-ai-hub/open-webui-wrapper:ci", upstream_inventory)
        self.assertIn("b1-ai-hub/localai:ci", upstream_inventory)
        self.assertIn("b1-ai-hub/comfyui:ci", upstream_inventory)
        self.assertIn("b1-ai-hub/voicebox:ci", upstream_inventory)
        self.assertIn("image --exit-code 0 --severity HIGH,CRITICAL --ignore-unfixed", upstream_inventory)
        self.assertIn('report="$(echo "$image" | tr \'/:\' \'--\').trivy.json"', upstream_inventory)
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            steps["Upload upstream-heavy vulnerability inventory"]["uses"],
        )


if __name__ == "__main__":
    unittest.main()
