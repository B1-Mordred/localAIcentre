from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


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

    def test_expected_jobs_are_present(self) -> None:
        self.assertEqual(
            set(self.workflow["jobs"]),
            {"backend", "frontend", "security-and-sbom", "containers"},
        )

    def test_backend_job_validates_compose_unit_tests_openapi_and_migrations(self) -> None:
        backend = self.workflow["jobs"]["backend"]
        commands = "\n".join(flatten_strings(backend))

        self.assertIn("make validate", commands)
        self.assertIn("make openapi-check", commands)
        self.assertIn("tests.unit.test_migrations", commands)
        self.assertIn("deploy/scripts/bootstrap.py", commands)
        self.assertIn("PyYAML==6.0.2", commands)
        self.assertIn("services/control-plane/requirements.txt", commands)

    def test_default_validation_runs_security_and_compatibility_harnesses(self) -> None:
        validate_line = next(
            line for line in self.makefile_text.splitlines() if line.startswith("validate:")
        )

        self.assertIn("unit", validate_line)
        self.assertIn("python-check", validate_line)
        self.assertIn("compatibility", validate_line)
        self.assertIn("security", validate_line)

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

    def test_security_job_runs_secret_scan_sbom_validation_and_python_audit(self) -> None:
        security = self.workflow["jobs"]["security-and-sbom"]
        commands = "\n".join(flatten_strings(security))

        self.assertEqual(self.workflow["env"]["CYCLONEDX_CLI_IMAGE"], "cyclonedx/cyclonedx-cli:0.29.1")
        self.assertNotIn(":latest", self.workflow["env"]["CYCLONEDX_CLI_IMAGE"])
        self.assertIn("pip-audit==2.9.0", commands)
        self.assertIn("make secret-scan", commands)
        self.assertIn("make sbom", commands)
        self.assertIn("make voicebox-audit-inventory", commands)
        self.assertIn("validate --input-file /sbom/b1-ai-hub.cdx.json", commands)
        self.assertIn("actions/upload-artifact@v4", commands)
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

        self.assertEqual(self.workflow["env"]["TRIVY_IMAGE"], "aquasec/trivy:0.66.0")
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
        self.assertIn("actions/upload-artifact@v4", steps["Upload upstream-heavy vulnerability inventory"]["uses"])


if __name__ == "__main__":
    unittest.main()
