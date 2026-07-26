from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class SelfTestApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_settings(self, **changes: Any) -> None:
        original = main.settings
        main.settings = replace(main.settings, **changes)
        self.addCleanup(lambda: setattr(main, "settings", original))

    def patch_auth(self, scopes: set[str]) -> None:
        auth = main.AuthContext(subject_id="admin_1", role=main.Role.ADMIN, scopes=frozenset(scopes))

        async def authenticate(_: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", authenticate)

    def test_admin_runtimes_exposes_compose_selection_readiness(self) -> None:
        class FakeAdapter:
            external = False

            async def health(self) -> dict[str, Any]:
                return {"name": "localai", "status": "ok", "details": {"version": "pinned"}}

        class FakeDatabase:
            async def list_runtime_states(self) -> list[dict[str, Any]]:
                return []

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            self.assertEqual(path, "/v1/services")
            return {"services": []}, None

        def compose_selection_snapshot() -> dict[str, Any]:
            return {
                "format": "b1-ai-hub-compose-selection/v1",
                "status": "blocked",
                "raw_compose_file": "compose.yaml:compose.production-localai.yaml",
                "raw_compose_profiles": "",
                "selected_file_basenames": ["compose.yaml", "compose.production-localai.yaml"],
                "selected_profiles": [],
                "production_required_runtimes": ["localai", "comfyui"],
                "required_files": ["compose.yaml", "compose.production-comfyui.yaml", "compose.production-localai.yaml"],
                "required_profiles": [],
                "missing_files": ["compose.production-comfyui.yaml"],
                "missing_profiles": [],
            }

        self.patch_auth({"runtimes:read"})
        self.patch_settings(runtime_deployment_mode="production", runtime_production_required=("localai", "comfyui"))
        self.patch_attr("database", FakeDatabase())
        self.patch_attr("runtime_agent_get", runtime_agent_get)
        self.patch_attr("runtime_registry_snapshot", lambda: SimpleNamespace(adapters={"localai": FakeAdapter()}, public_adapters=lambda: []))
        original_compose_selection = main.acceptance.compose_selection_snapshot
        main.acceptance.compose_selection_snapshot = compose_selection_snapshot
        self.addCleanup(lambda: setattr(main.acceptance, "compose_selection_snapshot", original_compose_selection))

        result = asyncio.run(main.admin_runtimes())

        self.assertEqual(result["compose_readiness"]["status"], "failed")
        self.assertEqual(result["compose_selection"]["missing_files"], ["compose.production-comfyui.yaml"])
        self.assertEqual(result["readiness"]["status"], "failed")

    def test_runtime_unload_probe_uses_agent_dry_run(self) -> None:
        self.patch_settings(self_test_unload_runtime="localai")
        calls: list[dict[str, Any]] = []

        async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
            calls.append({"path": path, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status": "dry_run", "service": "localai", "action": "unload"}, None

        self.patch_attr("runtime_agent_post", runtime_agent_post)

        result = asyncio.run(main.self_test_runtime_unload())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[0]["path"], "/v1/runtime-actions/localai/unload")
        self.assertTrue(calls[0]["payload"]["dry_run"])

    def test_comfyui_build_info_self_test_passes_for_pinned_runtime(self) -> None:
        payload = {
            "status": "ok",
            "runtime": "comfyui",
            "action": "build-info",
            "hook": "b1-comfyui-runtime-hooks",
            "hook_version": "b1-comfyui-hooks/v0.3.77-b1",
            "upstream_repository": "Comfy-Org/ComfyUI",
            "upstream_version": "v0.3.77",
            "upstream_commit": "59afc3984868289f808d02fa5cd180edfb2de240",
            "source_archive_sha256": "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1",
            "pinned": True,
        }

        class FakeResponse:
            status_code = 200
            content = json.dumps(payload).encode("utf-8")

            def json(self) -> dict[str, Any]:
                return dict(payload)

        class FakeAsyncClient:
            def __init__(self, timeout: float, trust_env: bool) -> None:
                self.timeout = timeout
                self.trust_env = trust_env

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout, "trust_env": self.trust_env})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))
        self.patch_settings(comfyui_url="http://comfyui:8188", runtime_control_token="hook-token", runtime_deployment_mode="production", runtime_production_required=("comfyui",))

        result = asyncio.run(main.self_test_comfyui_build_info())

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["required"])
        self.assertEqual(result["data"]["build_info"]["upstream_commit"], "59afc3984868289f808d02fa5cd180edfb2de240")
        self.assertEqual(FakeAsyncClient.calls[0]["url"], "http://comfyui:8188/b1/runtime/build-info")  # type: ignore[attr-defined]
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer hook-token")  # type: ignore[attr-defined]
        self.assertFalse(FakeAsyncClient.calls[0]["trust_env"])  # type: ignore[attr-defined]

    def test_comfyui_build_info_self_test_fails_when_required_in_production(self) -> None:
        payload = {"status": "unconfigured", "runtime": "comfyui", "action": "build-info", "pinned": False}

        class FakeResponse:
            status_code = 200
            content = json.dumps(payload).encode("utf-8")

            def json(self) -> dict[str, Any]:
                return dict(payload)

        class FakeAsyncClient:
            def __init__(self, **_: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, *_: Any, **__: Any) -> FakeResponse:
                return FakeResponse()

        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))
        self.patch_settings(runtime_deployment_mode="production", runtime_production_required=("localai", "comfyui"))

        result = asyncio.run(main.self_test_comfyui_build_info())

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["data"]["required"])
        self.assertEqual(result["data"]["build_info"]["status"], "unconfigured")

    def test_comfyui_build_info_self_test_is_ok_when_not_required_in_production(self) -> None:
        self.patch_settings(runtime_deployment_mode="production", runtime_production_required=("localai", "audio-cpu"))

        result = asyncio.run(main.self_test_comfyui_build_info())

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["required"])
        self.assertNotIn("build_info", result["data"])

    def test_tiny_inference_requires_expected_embedding_shape(self) -> None:
        calls: list[dict[str, Any]] = []
        resolution = main.RuntimeResolution(
            public_alias="embedding-default",
            model_id="b1-cpu-placeholder-embedding",
            model_version="0.1.0",
            resolved_model_version="b1-cpu-placeholder-embedding@0.1.0",
            runtime="audio-cpu",
            preferred_runtime="audio-cpu",
            requires_gpu=False,
            resource_label="recommended",
            runtime_policy="any",
        )

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            calls.append({"resolve": model, "modality": modality, "runtime_policy": runtime_policy, "operation": operation})
            return resolution

        async def call_openai_runtime_json(
            path: str,
            payload: dict[str, Any],
            selected: Any,
            operation: str,
            owner_id: str | None = None,
        ) -> Any:
            calls.append({"path": path, "payload": payload, "runtime": selected.runtime, "operation": operation, "owner_id": owner_id})
            return main.JSONResponse(content={"data": [{"embedding": [0.0] * 8}], "b1_placeholder": True})

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("call_openai_runtime_json", call_openai_runtime_json)

        result = asyncio.run(main.self_test_tiny_inference("admin_1"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["runtime"], "audio-cpu")
        self.assertTrue(result["data"]["placeholder"])
        self.assertEqual(calls[1]["payload"]["dimensions"], 8)
        self.assertEqual(calls[1]["owner_id"], "admin_1")

    def test_artifact_delivery_probe_uses_range_and_cleans_temp_file(self) -> None:
        class FakeResponse:
            status_code = 206
            content = b"b1"
            headers = {"content-range": "bytes 0-1/31", "content-length": "2"}

        class FakeAsyncClient:
            def __init__(self, timeout: float, **_: Any) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"url": url, "headers": headers, "timeout": self.timeout})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp)
            self.patch_settings(artifact_root=str(artifact_root), artifact_base_url="http://artifact-server:8000", artifact_server_token="service-token")
            result = asyncio.run(main.self_test_artifact_delivery())
            leftovers = list((artifact_root / "temporary").glob("self-test-*.txt"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Range"], "bytes=0-1")  # type: ignore[attr-defined]
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer service-token")  # type: ignore[attr-defined]
        self.assertEqual(leftovers, [])

    def test_artifact_delivery_probe_fails_closed_without_internal_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, artifact_base_url="http://artifact-server:8000", artifact_server_token="")
            result = asyncio.run(main.self_test_artifact_delivery())

        self.assertEqual(result["status"], "failed")
        self.assertIn("artifact-server service token is not configured", result["detail"])

    def test_tls_routing_probe_uses_configured_ca_file(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b"{}"
            headers = {
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }

        class FakeAsyncClient:
            def __init__(self, timeout: float, verify: bool | str) -> None:
                self.timeout = timeout
                self.verify = verify

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"url": url, "headers": headers, "timeout": self.timeout, "verify": self.verify})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "root.crt"
            ca_file.write_text("test ca\n", encoding="utf-8")
            self.patch_settings(
                self_test_tls_urls=("https://api.ai.b1.germering/healthz",),
                self_test_tls_ca_file=str(ca_file),
                self_test_tls_verify=True,
            )
            result = asyncio.run(main.self_test_tls_routing())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["routes"][0]["security_headers"], "ok")
        self.assertEqual(result["data"]["routes"][0]["route_keys"], ["api"])
        self.assertEqual(result["data"]["expected_route_keys"], ["chat", "control", "media", "comfy", "voice", "models", "api"])
        self.assertEqual(FakeAsyncClient.calls[0]["verify"], str(ca_file))  # type: ignore[attr-defined]
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Accept"], "application/json")  # type: ignore[attr-defined]

    def test_tls_routing_probe_fails_without_gateway_security_headers(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b"{}"
            headers = {"X-Content-Type-Options": "nosniff"}

        class FakeAsyncClient:
            def __init__(self, timeout: float, verify: bool | str) -> None:
                self.timeout = timeout
                self.verify = verify

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
                return FakeResponse()

        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        self.patch_settings(
            self_test_tls_urls=("https://api.ai.b1.germering/healthz",),
            self_test_tls_verify=False,
        )

        result = asyncio.run(main.self_test_tls_routing())

        self.assertEqual(result["status"], "failed")
        route = result["data"]["routes"][0]
        self.assertEqual(route["security_headers"], "failed")
        self.assertIn("missing strict-transport-security", route["header_failures"])

    def test_caddy_internal_ca_status_and_download_use_configured_root(self) -> None:
        self.patch_auth({"admin:read"})
        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "root.crt"
            content = b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
            ca_file.write_bytes(content)
            expected_digest = main.hashlib.sha256(content).hexdigest()
            self.patch_settings(caddy_internal_ca_file=str(ca_file))

            status = asyncio.run(main.admin_caddy_internal_ca_status(authorization="Bearer key"))
            response = asyncio.run(main.admin_caddy_internal_ca_root(authorization="Bearer key"))

        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["available"])
        self.assertEqual(status["sha256"], expected_digest)
        self.assertEqual(status["download_url"], "/admin/tls/caddy-ca/root.crt")
        self.assertEqual(status["fingerprint_sha256"], ":".join(expected_digest[index : index + 2].upper() for index in range(0, 64, 2)))
        self.assertEqual(response.body, content)
        self.assertEqual(response.media_type, "application/x-x509-ca-cert")
        self.assertEqual(response.headers["x-b1-sha256"], expected_digest)
        self.assertIn("b1-ai-hub-caddy-root.crt", response.headers["content-disposition"])

    def test_caddy_internal_ca_self_test_passes_when_internal_root_is_exportable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "root.crt"
            ca_file.write_bytes(b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n")
            self.patch_settings(caddy_tls_args="internal", caddy_internal_ca_file=str(ca_file), runtime_deployment_mode="production")
            result = asyncio.run(main.self_test_caddy_internal_ca())

        self.assertEqual(result["name"], "tls:caddy-ca")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["required"])
        self.assertTrue(result["data"]["available"])

    def test_caddy_internal_ca_self_test_warns_missing_root_in_development(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(caddy_tls_args="internal", caddy_internal_ca_file=str(Path(tmp) / "missing.crt"), runtime_deployment_mode="development")
            result = asyncio.run(main.self_test_caddy_internal_ca())

        self.assertEqual(result["status"], "warning")
        self.assertIn("development mode permits bootstrapping only", result["detail"])
        self.assertFalse(result["data"]["available"])

    def test_caddy_internal_ca_self_test_fails_missing_root_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(caddy_tls_args="internal", caddy_internal_ca_file=str(Path(tmp) / "missing.crt"), runtime_deployment_mode="production")
            result = asyncio.run(main.self_test_caddy_internal_ca())

        self.assertEqual(result["status"], "failed")
        self.assertIn("has not been generated", result["detail"])

    def test_caddy_internal_ca_self_test_passes_when_external_certs_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(
                caddy_tls_args="/etc/caddy/external-certs/fullchain.pem /etc/caddy/external-certs/privkey.pem",
                caddy_internal_ca_file=str(Path(tmp) / "missing.crt"),
                runtime_deployment_mode="production",
            )
            result = asyncio.run(main.self_test_caddy_internal_ca())
            status = result["data"]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(status["status"], "not_required")
        self.assertFalse(status["required"])
        self.assertFalse(status["available"])

    def test_caddy_internal_ca_download_refuses_missing_root(self) -> None:
        self.patch_auth({"admin:read"})
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(caddy_internal_ca_file=str(Path(tmp) / "missing-root.crt"))
            status = asyncio.run(main.admin_caddy_internal_ca_status(authorization="Bearer key"))
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_caddy_internal_ca_root(authorization="Bearer key"))

        self.assertEqual(status["status"], "missing")
        self.assertFalse(status["available"])
        self.assertEqual(raised.exception.status_code, 404)

    def test_caddy_internal_ca_download_refuses_symlink_root(self) -> None:
        self.patch_auth({"admin:read"})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.crt"
            target.write_text("target ca\n", encoding="utf-8")
            link = Path(tmp) / "root.crt"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            self.patch_settings(caddy_internal_ca_file=str(link))
            status = asyncio.run(main.admin_caddy_internal_ca_status(authorization="Bearer key"))
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.admin_caddy_internal_ca_root(authorization="Bearer key"))

        self.assertEqual(status["status"], "blocked")
        self.assertTrue(status["symlink"])
        self.assertFalse(status["available"])
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
