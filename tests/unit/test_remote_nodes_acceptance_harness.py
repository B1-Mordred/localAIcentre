from __future__ import annotations

import os
import unittest

from tests.compatibility import test_remote_nodes_non_comfy as harness


class EnvPatch:
    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.original: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.original[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class RemoteNodesAcceptanceHarnessTests(unittest.TestCase):
    def test_example_workflows_do_not_embed_credentials(self) -> None:
        findings: list[str] = []
        for path in sorted(harness.EXAMPLES_ROOT.glob("*.json")):
            findings.extend(f"{path.name}:{finding}" for finding in harness.workflow_secret_findings(harness.json.loads(path.read_text(encoding="utf-8"))))

        self.assertFalse(findings)

    def test_workflow_secret_scanner_detects_keys_and_token_values(self) -> None:
        findings = harness.workflow_secret_findings(
            {
                "nodes": [
                    {"inputs": {"api_key": "do-not-store-this"}},
                    {"inputs": {"model": "chat-default", "header": "Bearer b1k_public.secret"}},
                ]
            }
        )

        self.assertIn("$.nodes[0].inputs.api_key", findings)
        self.assertIn("$.nodes[1].inputs.header", findings)

    def test_configured_credential_source_detects_environment_sources(self) -> None:
        with EnvPatch(
            B1_AI_HUB_CONFIG_FILE="",
            B1_AI_HUB_API_KEY="b1k_env.secret",
            B1_AI_HUB_API_KEY_FILE=None,
        ):
            self.assertEqual(harness.configured_credential_source(), "environment")
        with EnvPatch(
            B1_AI_HUB_CONFIG_FILE="",
            B1_AI_HUB_API_KEY=None,
            B1_AI_HUB_API_KEY_FILE="/run/secrets/b1-api-key",
        ):
            self.assertEqual(harness.configured_credential_source(), "environment_file")


if __name__ == "__main__":
    unittest.main()
