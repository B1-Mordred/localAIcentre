from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_SERVER_APP_ROOT = ROOT / "services" / "artifact-server" / "app"

try:
    artifact_pkg_spec = importlib.util.spec_from_file_location(
        "artifact_server_app",
        ARTIFACT_SERVER_APP_ROOT / "__init__.py",
        submodule_search_locations=[str(ARTIFACT_SERVER_APP_ROOT)],
    )
    if artifact_pkg_spec is None or artifact_pkg_spec.loader is None:
        raise ModuleNotFoundError("artifact_server_app")
    artifact_pkg = importlib.util.module_from_spec(artifact_pkg_spec)
    sys.modules["artifact_server_app"] = artifact_pkg
    artifact_pkg_spec.loader.exec_module(artifact_pkg)
    artifact_main_spec = importlib.util.spec_from_file_location("artifact_server_app.main", ARTIFACT_SERVER_APP_ROOT / "main.py")
    if artifact_main_spec is None or artifact_main_spec.loader is None:
        raise ModuleNotFoundError("artifact_server_app.main")
    artifact_main = importlib.util.module_from_spec(artifact_main_spec)
    sys.modules["artifact_server_app.main"] = artifact_main
    artifact_main_spec.loader.exec_module(artifact_main)
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "fastapi":
        raise
    artifact_main = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(artifact_main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ArtifactServerAuthTests(unittest.TestCase):
    def patch_env(self, values: dict[str, str | None]) -> None:
        original = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        def restore() -> None:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def request(self, authorization: str | None = None) -> Any:
        headers = {}
        if authorization is not None:
            headers["authorization"] = authorization
        return SimpleNamespace(headers=headers)

    def test_artifact_server_fails_closed_without_configured_service_token(self) -> None:
        self.patch_env({"B1_ARTIFACT_SERVER_TOKEN": None, "B1_ARTIFACT_SERVER_TOKEN_FILE": None})

        with self.assertRaises(artifact_main.HTTPException) as raised:
            artifact_main.require_service_authorization(self.request("Bearer anything"))

        self.assertEqual(raised.exception.status_code, 503)

    def test_artifact_server_requires_matching_bearer_token(self) -> None:
        self.patch_env({"B1_ARTIFACT_SERVER_TOKEN": "service-token", "B1_ARTIFACT_SERVER_TOKEN_FILE": None})

        for authorization in (None, "Basic service-token", "Bearer wrong-token"):
            with self.assertRaises(artifact_main.HTTPException) as raised:
                artifact_main.require_service_authorization(self.request(authorization))
            self.assertEqual(raised.exception.status_code, 401)

        artifact_main.require_service_authorization(self.request("Bearer service-token"))

    def test_artifact_server_reads_token_from_secret_file_and_reports_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "artifact_server_token"
            token_path.write_text("file-token\n", encoding="utf-8")
            self.patch_env({"B1_ARTIFACT_SERVER_TOKEN": None, "B1_ARTIFACT_SERVER_TOKEN_FILE": str(token_path)})

            artifact_main.require_service_authorization(self.request("Bearer file-token"))
            health = asyncio.run(artifact_main.healthz())

        self.assertTrue(health["auth_required"])


if __name__ == "__main__":
    unittest.main()
