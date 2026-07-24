from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "generate_openapi.py"
OPENAPI = ROOT / "docs" / "openapi.json"
MAIN = ROOT / "services" / "control-plane" / "app" / "main.py"
REQUIRED_PUBLIC_ROUTE_METHODS = {
    "/v1/models": {"get"},
    "/v1/chat/completions": {"post"},
    "/v1/responses": {"post"},
    "/v1/embeddings": {"post"},
    "/v1/audio/speech": {"post"},
    "/v1/audio/transcriptions": {"post"},
    "/v1/images/generations": {"post"},
    "/v1/images/edits": {"post"},
    "/v1/media/jobs": {"get", "post"},
    "/v1/media/jobs/{job_id}": {"get", "delete"},
    "/v1/media/jobs/{job_id}/events": {"get"},
    "/v1/media/jobs/{job_id}/artifacts": {"get"},
    "/v1/runtime-reservations": {"post"},
    "/v1/runtime-reservations/{id}": {"get", "delete"},
    "/modelhub/v1/catalog": {"get"},
    "/modelhub/v1/models/{id}": {"get"},
    "/modelhub/v1/models/{id}/versions": {"get"},
    "/modelhub/v1/blobs/{sha256}": {"get", "head"},
    "/modelhub/v1/sync/plan": {"post"},
    "/modelhub/v1/clients": {"get", "post"},
    "/modelhub/v1/clients/{id}/cidr-allowlist": {"put"},
    "/modelhub/v1/clients/{id}/policy": {"put"},
    "/modelhub/v1/clients/{id}": {"delete"},
}
COMFYUI_PASSTHROUGH_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}

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


class ApiRouteSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(MAIN.read_text(encoding="utf-8"))

    def test_fastapi_routes_do_not_repeat_the_same_method_and_path(self) -> None:
        registrations: dict[tuple[str, str], list[str]] = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "app"
                    and func.attr in {"get", "post", "put", "patch", "delete", "head"}
                ):
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
                    continue
                key = (func.attr.upper(), decorator.args[0].value)
                registrations.setdefault(key, []).append(node.name)

        duplicates = [
            f"{method} {path}: {', '.join(functions)}"
            for (method, path), functions in sorted(registrations.items())
            if len(functions) > 1
        ]
        self.assertEqual(duplicates, [])

    def test_source_registers_native_compatibility_websocket_and_passthrough_routes(self) -> None:
        websocket_paths: set[str] = set()
        passthrough_methods: dict[str, set[str]] = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "app"):
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
                    continue
                path = decorator.args[0].value
                if func.attr == "websocket":
                    websocket_paths.add(path)
                if func.attr == "api_route":
                    methods: set[str] = set()
                    for keyword in decorator.keywords:
                        if keyword.arg != "methods" or not isinstance(keyword.value, ast.List):
                            continue
                        for item in keyword.value.elts:
                            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                methods.add(item.value.upper())
                    passthrough_methods[path] = methods

        self.assertIn("/ws", websocket_paths)
        self.assertIn("/{path:path}", websocket_paths)
        self.assertEqual(passthrough_methods.get("/{path:path}"), COMFYUI_PASSTHROUGH_METHODS)

    def test_committed_openapi_documents_job_events_as_sse(self) -> None:
        schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
        for path in ("/v1/media/jobs/{job_id}/events", "/admin/jobs/{job_id}/events"):
            content = schema["paths"][path]["get"]["responses"]["200"]["content"]
            self.assertIn("text/event-stream", content)

    def test_committed_openapi_documents_prometheus_metrics_as_text(self) -> None:
        schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
        content = schema["paths"]["/admin/metrics.prometheus"]["get"]["responses"]["200"]["content"]
        self.assertIn("text/plain", content)

    def test_committed_openapi_exposes_plan_required_public_routes_and_methods(self) -> None:
        schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
        paths = schema["paths"]
        missing: list[str] = []
        for path, required_methods in REQUIRED_PUBLIC_ROUTE_METHODS.items():
            if path not in paths:
                missing.append(f"{path}: missing path")
                continue
            actual_methods = {method.lower() for method in paths[path]}
            missing_methods = sorted(required_methods - actual_methods)
            if missing_methods:
                missing.append(f"{path}: missing {', '.join(missing_methods)}")
        self.assertEqual(missing, [])


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
        self.assertIn("/admin/metrics", paths)
        self.assertIn("/admin/metrics.prometheus", paths)
        self.assertIn("/admin/admission", paths)
        self.assertIn("/admin/admission-policy", paths)
        self.assertIn("/admin/admission-policy/validate", paths)
        self.assertIn("/v1/models", paths)
        self.assertIn("/v1/media/jobs/{job_id}/events", paths)
        self.assertIn("/admin/jobs", paths)
        self.assertIn("/admin/jobs/{job_id}/events", paths)
        self.assertIn("/admin/jobs/{job_id}/priority", paths)
        self.assertIn("/admin/jobs/{job_id}/cancel", paths)
        self.assertIn("/admin/jobs/{job_id}/retry", paths)
        self.assertIn("/admin/runtime-reservations", paths)
        self.assertIn("/admin/voicebox/profiles", paths)
        self.assertIn("/admin/voicebox/profiles/{profile_id}", paths)
        self.assertIn("/admin/voicebox/profiles/{profile_id}/export", paths)
        self.assertIn("/admin/artifacts/retention-plan", paths)
        self.assertIn("/admin/artifacts/cleanup", paths)
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
