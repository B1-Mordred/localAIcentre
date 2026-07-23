from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
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
            self.patch_settings(artifact_root=str(artifact_root), artifact_base_url="http://artifact-server:8000")
            result = asyncio.run(main.self_test_artifact_delivery())
            leftovers = list((artifact_root / "temporary").glob("self-test-*.txt"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Range"], "bytes=0-1")  # type: ignore[attr-defined]
        self.assertEqual(leftovers, [])

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


if __name__ == "__main__":
    unittest.main()
