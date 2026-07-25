from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "generate_openapi_client.py"
CONTROL_CLIENT = ROOT / "web" / "control-center" / "src" / "generated" / "b1-api-client.ts"
MEDIA_CLIENT = ROOT / "web" / "media-studio" / "src" / "generated" / "b1-api-client.ts"
CONTROL_SOURCE = ROOT / "web" / "control-center" / "src" / "main.tsx"
MEDIA_SOURCE = ROOT / "web" / "media-studio" / "src" / "main.tsx"

REQUIRED_GENERATED_ROUTES = (
    '"POST /v1/chat/completions"',
    '"POST /v1/audio/speech"',
    '"POST /v1/images/edits"',
    '"GET /v1/media/jobs/{job_id}/events"',
    '"POST /v1/runtime-reservations"',
    '"HEAD /modelhub/v1/blobs/{sha256}"',
    '"POST /admin/acceptance-reports/preview"',
)


spec = importlib.util.spec_from_file_location("generate_openapi_client", SCRIPT)
if spec is None or spec.loader is None:  # pragma: no cover
    raise ModuleNotFoundError("generate_openapi_client")
generate_openapi_client = importlib.util.module_from_spec(spec)
sys.modules["generate_openapi_client"] = generate_openapi_client
spec.loader.exec_module(generate_openapi_client)


class OpenApiClientGenerationTests(unittest.TestCase):
    def test_generated_web_clients_are_current(self) -> None:
        schema = generate_openapi_client.load_schema(ROOT / "docs" / "openapi.json")
        expected = generate_openapi_client.render_client(schema)
        self.assertEqual(CONTROL_CLIENT.read_text(encoding="utf-8"), expected)
        self.assertEqual(MEDIA_CLIENT.read_text(encoding="utf-8"), expected)

    def test_generated_client_contains_required_plan_routes(self) -> None:
        source = CONTROL_CLIENT.read_text(encoding="utf-8")
        for route in REQUIRED_GENERATED_ROUTES:
            with self.subTest(route=route):
                self.assertIn(route, source)

    def test_generated_client_uses_openapi_schema_paths(self) -> None:
        schema = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        source = CONTROL_CLIENT.read_text(encoding="utf-8")
        for path in sorted(schema["paths"]):
            with self.subTest(path=path):
                self.assertIn(json.dumps(path), source)

    def test_web_apps_import_and_use_generated_client(self) -> None:
        for source_path in (CONTROL_SOURCE, MEDIA_SOURCE):
            source = source_path.read_text(encoding="utf-8")
            with self.subTest(source=str(source_path.relative_to(ROOT))):
                self.assertIn("./generated/b1-api-client", source)
                self.assertIn("new B1ApiClient", source)
                self.assertIn("apiClient.fetch", source)
                self.assertIn("apiClient.json", source)


if __name__ == "__main__":
    unittest.main()
