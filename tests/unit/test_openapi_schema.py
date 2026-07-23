from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "generate_openapi.py"
OPENAPI = ROOT / "docs" / "openapi.json"

try:
    spec = importlib.util.spec_from_file_location("generate_openapi", SCRIPT)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("generate_openapi")
    generate_openapi = importlib.util.module_from_spec(spec)
    sys.modules["generate_openapi"] = generate_openapi
    spec.loader.exec_module(generate_openapi)
    GENERATED = generate_openapi.schema_json()
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography"}:
        raise
    GENERATED = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(GENERATED is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class OpenApiSchemaTests(unittest.TestCase):
    def test_committed_openapi_schema_is_current(self) -> None:
        self.assertTrue(OPENAPI.is_file(), "docs/openapi.json must be committed")
        self.assertEqual(OPENAPI.read_text(encoding="utf-8"), GENERATED)

    def test_schema_exposes_required_public_surfaces(self) -> None:
        schema = json.loads(GENERATED)
        self.assertTrue(schema["openapi"].startswith("3.1."))
        paths = schema["paths"]
        self.assertIn("/auth/status", paths)
        self.assertIn("/auth/setup", paths)
        self.assertIn("/auth/login", paths)
        self.assertIn("/auth/logout", paths)
        self.assertIn("/v1/models", paths)
        self.assertIn("/v1/media/jobs/{job_id}/events", paths)
        self.assertIn("/admin/jobs", paths)
        self.assertIn("/admin/jobs/{job_id}/priority", paths)
        self.assertIn("/admin/jobs/{job_id}/cancel", paths)
        self.assertIn("/admin/jobs/{job_id}/retry", paths)
        self.assertIn("/admin/voicebox/profiles", paths)
        self.assertIn("/admin/voicebox/profiles/{profile_id}", paths)
        self.assertIn("/admin/voicebox/profiles/{profile_id}/export", paths)
        self.assertIn("/admin/backups/retention-plan", paths)
        self.assertIn("/admin/backups/cleanup", paths)
        self.assertIn("/admin/backups/schedule", paths)
        self.assertIn("/admin/backups/schedule/run", paths)
        self.assertIn("/admin/maintenance", paths)
        self.assertIn("/admin/updates", paths)
        self.assertIn("/admin/updates/{update_id}", paths)
        self.assertIn("/admin/updates/{update_id}/stage", paths)
        self.assertIn("/admin/updates/{update_id}/health-check", paths)
        self.assertIn("/admin/updates/{update_id}/rollback", paths)
        self.assertIn("/admin/resource-policy", paths)
        self.assertIn("/admin/resource-policy/validate", paths)
        self.assertIn("/admin/secrets", paths)
        self.assertIn("/admin/secrets/{name}", paths)
        self.assertIn("/admin/secrets/{name}/verify", paths)
        self.assertIn("/modelhub/v1/blobs/{sha256}", paths)
        self.assertIn("/admin/models/{model_id}/versions/{version}/blob-quarantine-plan", paths)
        self.assertIn("/admin/models/{model_id}/versions/{version}/blobs/quarantine", paths)


if __name__ == "__main__":
    unittest.main()
