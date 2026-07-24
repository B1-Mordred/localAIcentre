from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate_sbom = load_script("generate_sbom", "deploy/scripts/generate_sbom.py")
secret_scan = load_script("secret_scan", "deploy/scripts/secret_scan.py")


class SbomGenerationTests(unittest.TestCase):
    def test_repository_sbom_includes_python_npm_and_container_components(self) -> None:
        sbom = generate_sbom.build_sbom(ROOT)
        components = sbom["components"]

        self.assertEqual(sbom["bomFormat"], "CycloneDX")
        self.assertEqual(sbom["specVersion"], "1.5")
        self.assertTrue(any(component["name"] == "fastapi" for component in components))
        self.assertTrue(any(component["name"] == "react" for component in components))
        self.assertTrue(any(component["type"] == "container" and component["name"] == "python" for component in components))
        self.assertTrue(
            any(
                component["type"] == "container"
                and any(
                    prop == {"name": "b1:source", "value": "integrations/b1-model-client/Dockerfile"}
                    for prop in component.get("properties", [])
                )
                for component in components
            )
        )

        uvicorn = next(component for component in components if component["name"] == "uvicorn")
        self.assertEqual(uvicorn["version"], "0.35.0")
        self.assertEqual(uvicorn["purl"], "pkg:pypi/uvicorn@0.35.0")
        self.assertIn({"name": "b1:extras", "value": "standard"}, uvicorn["properties"])

        piper = next(component for component in components if component["name"] == "rhasspy/piper")
        self.assertEqual(piper["type"], "application")
        self.assertEqual(piper["version"], "2023.11.14-2")
        self.assertEqual(piper["purl"], "pkg:github/rhasspy/piper@2023.11.14-2")
        self.assertIn(
            {
                "name": "b1:sha256",
                "value": "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992",
            },
            piper["properties"],
        )

        localai = next(component for component in components if component["name"] == "mudler/LocalAI")
        self.assertEqual(localai["type"], "application")
        self.assertEqual(localai["version"], "v4.7.1-gpu-nvidia-cuda-12")
        self.assertEqual(localai["purl"], "pkg:github/mudler/LocalAI@v4.7.1-gpu-nvidia-cuda-12")
        self.assertIn(
            {
                "name": "b1:upstream_image",
                "value": "localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973",
            },
            localai["properties"],
        )

        comfyui = next(component for component in components if component["name"] == "Comfy-Org/ComfyUI")
        self.assertEqual(comfyui["type"], "application")
        self.assertEqual(comfyui["version"], "v0.3.77")
        self.assertEqual(comfyui["purl"], "pkg:github/Comfy-Org/ComfyUI@v0.3.77")
        self.assertIn(
            {
                "name": "b1:sha256",
                "value": "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1",
            },
            comfyui["properties"],
        )

        voicebox = next(component for component in components if component["name"] == "jamiepine/voicebox")
        self.assertEqual(voicebox["type"], "application")
        self.assertEqual(voicebox["version"], "v0.5.0")
        self.assertEqual(voicebox["purl"], "pkg:github/jamiepine/voicebox@v0.5.0")
        self.assertIn(
            {
                "name": "b1:sha256",
                "value": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
            },
            voicebox["properties"],
        )
        chatterbox = next(component for component in components if component["name"] == "chatterbox-tts")
        self.assertEqual(chatterbox["version"], "0.1.7")
        self.assertIn({"name": "b1:source", "value": "deploy/voicebox/constraints.txt"}, chatterbox["properties"])

        checkout = next(component for component in components if component["name"] == "actions/checkout")
        self.assertEqual(checkout["type"], "application")
        self.assertEqual(checkout["version"], "11d5960a326750d5838078e36cf38b85af677262")
        self.assertEqual(checkout["purl"], "pkg:github/actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
        self.assertIn({"name": "b1:ecosystem", "value": "github-actions"}, checkout["properties"])
        self.assertIn({"name": "b1:pin", "value": "commit-sha"}, checkout["properties"])

        runner = next(component for component in components if component["name"] == "github-actions/runner-image")
        self.assertEqual(runner["type"], "operating-system")
        self.assertEqual(runner["version"], "ubuntu-24.04")
        self.assertIn({"name": "b1:pin", "value": "fixed-runner-label"}, runner["properties"])

        trivy = next(component for component in components if component["name"] == "aquasec/trivy")
        self.assertEqual(trivy["type"], "container")
        self.assertEqual(trivy["version"], "0.66.0")
        self.assertIn(
            {
                "name": "b1:image_reference",
                "value": "aquasec/trivy:0.66.0@sha256:086971aaf400beebd94e8300fd8ea623774419597169156cec56eec5b00dfb1e",
            },
            trivy["properties"],
        )

    def test_github_workflow_parser_records_ci_supply_chain_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = root / "ci.yaml"
            workflow.write_text(
                "\n".join(
                    [
                        'TRIVY_IMAGE: "aquasec/trivy:0.66.0@sha256:' + "a" * 64 + '"',
                        "jobs:",
                        "  backend:",
                        "    runs-on: ubuntu-24.04",
                        "    steps:",
                        "      - uses: actions/checkout@" + "b" * 40,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            components = generate_sbom.parse_github_workflow(workflow, ".github/workflows/ci.yaml")

        self.assertIn(
            {
                "type": "application",
                "name": "actions/checkout",
                "version": "b" * 40,
                "purl": "pkg:github/actions/checkout@" + "b" * 40,
                "properties": [
                    {"name": "b1:source", "value": ".github/workflows/ci.yaml"},
                    {"name": "b1:line", "value": "6"},
                    {"name": "b1:ecosystem", "value": "github-actions"},
                    {"name": "b1:pin", "value": "commit-sha"},
                ],
            },
            components,
        )
        self.assertTrue(any(component["type"] == "operating-system" and component["version"] == "ubuntu-24.04" for component in components))
        self.assertTrue(
            any(
                component["type"] == "container"
                and component["name"] == "aquasec/trivy"
                and component["version"] == "0.66.0"
                and {"name": "b1:ci_env", "value": "TRIVY_IMAGE"} in component["properties"]
                for component in components
            )
        )

    def test_npm_package_name_is_inferred_from_lockfile_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "package-lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"name": "app", "version": "0.1.0"},
                            "node_modules/react": {"version": "19.1.1"},
                            "node_modules/@radix-ui/react-tabs": {"version": "1.1.13"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            components = generate_sbom.parse_package_lock(lock, "package-lock.json")

        self.assertIn(
            {
                "type": "library",
                "name": "react",
                "version": "19.1.1",
                "scope": "required",
                "purl": "pkg:npm/react@19.1.1",
                "properties": [
                    {"name": "b1:source", "value": "package-lock.json"},
                    {"name": "b1:ecosystem", "value": "npm"},
                    {"name": "b1:package_path", "value": "node_modules/react"},
                ],
            },
            components,
        )
        self.assertTrue(any(component["name"] == "@radix-ui/react-tabs" for component in components))


class SecretScannerTests(unittest.TestCase):
    def test_detects_real_looking_tokens_without_committing_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "settings.env").write_text(
                "OPENAI_API_KEY=" + "sk-" + ("A" * 32) + "\n",
                encoding="utf-8",
            )

            findings = secret_scan.scan(root)

        self.assertEqual(len(findings), 2)
        self.assertEqual({finding["rule"] for finding in findings}, {"openai-token", "generic-env-assignment"})
        self.assertTrue(all("<secret>" in finding["excerpt"] for finding in findings))

    def test_allows_secret_file_paths_and_runtime_token_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text(
                "\n".join(
                    [
                        "POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password",
                        "B1_RUNTIME_AGENT_TOKEN_FILE=/run/secrets/runtime_agent_token",
                        "token = main.current_request.set(request)",
                        "api_key = settings.open_webui_api_key.strip()",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            findings = secret_scan.scan(root)

        self.assertEqual(findings, [])

    def test_ignores_node_modules_and_binary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node_modules = root / "node_modules"
            node_modules.mkdir()
            (node_modules / "leak.txt").write_text("OPENAI_API_KEY=" + "sk-" + ("B" * 32), encoding="utf-8")
            (root / "image.png").write_bytes(b"OPENAI_API_KEY=sk-" + (b"C" * 32))

            findings = secret_scan.scan(root)

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
