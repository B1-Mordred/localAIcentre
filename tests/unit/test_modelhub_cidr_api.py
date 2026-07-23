from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from starlette.requests import Request  # noqa: E402

    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    HTTPException = None
    Request = None
    main = None
    AuthContext = None
    Role = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def request_for(peer: str, headers: dict[str, str] | None = None) -> Any:
    if Request is None:
        raise RuntimeError("starlette is unavailable")
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/modelhub/v1/catalog",
            "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()],
            "client": (peer, 49200),
            "server": ("control-plane", 8000),
            "scheme": "http",
        }
    )


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ModelHubCidrApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_settings = main.settings
        self.original_redis_client = main.redis_client
        main.redis_client = None
        main.modelhub_blob_rate_windows.clear()
        main.settings = replace(self.original_settings, trusted_proxy_cidrs=("172.18.0.0/16", "127.0.0.1/32"))

    def tearDown(self) -> None:
        main.settings = self.original_settings
        main.redis_client = self.original_redis_client
        main.modelhub_blob_rate_windows.clear()

    def test_client_host_uses_forwarded_header_only_from_trusted_proxy(self) -> None:
        trusted_request = request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.44, 172.18.0.10"})
        self.assertEqual(main.client_host(trusted_request), "192.168.2.44")

        untrusted_request = request_for("192.168.2.44", {"X-Forwarded-For": "10.10.10.10"})
        self.assertEqual(main.client_host(untrusted_request), "192.168.2.44")

    def test_client_host_parses_standard_forwarded_header(self) -> None:
        request = request_for("172.18.0.10", {"Forwarded": 'for="[2001:db8::10]";proto=https;host=models.ai.b1.germering'})
        self.assertEqual(main.client_host(request), "2001:db8::10")

    def test_modelhub_client_cidr_gate_denies_outside_allowlist(self) -> None:
        allowed = {"cidr_allowlist": ["192.168.2.0/24"]}
        main.require_modelhub_client_network_allowed(allowed, request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.44"}))

        with self.assertRaises(HTTPException) as raised:
            main.require_modelhub_client_network_allowed(allowed, request_for("172.18.0.10", {"X-Forwarded-For": "10.10.10.10"}))
        self.assertEqual(raised.exception.status_code, 403)

    def test_modelhub_catalog_filters_by_client_allowlist(self) -> None:
        catalog_payload = {
            "object": "catalog",
            "aliases": [
                {"id": "chat-default", "root": "downloadable-llm", "resolved_model": {"id": "downloadable-llm"}},
                {"id": "image-default", "root": "image-model", "resolved_model": {"id": "image-model"}},
                {"id": "tts-fast", "root": "tts-model", "resolved_model": {"id": "tts-model"}},
            ],
            "models": [
                {
                    "id": "downloadable-llm",
                    "aliases": ["chat-default"],
                    "source": {"url": "https://downloads.example.test/llm.gguf?token=secret", "revision": "1.0.0"},
                },
                {"id": "image-model", "aliases": ["image-default"], "source": {"url": "https://downloads.example.test/image.safetensors#frag"}},
                {"id": "tts-model", "aliases": ["tts-fast"]},
            ],
        }

        original_catalog = main.catalog_snapshot
        main.catalog_snapshot = lambda: SimpleNamespace(to_catalog=lambda: catalog_payload)  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main, "catalog_snapshot", original_catalog))

        filtered = main.modelhub_catalog_for_client({"allowed_models": ["chat-default", "image-model"]})

        self.assertEqual([record["id"] for record in filtered["aliases"]], ["chat-default", "image-default"])
        self.assertEqual([record["id"] for record in filtered["models"]], ["downloadable-llm", "image-model"])
        self.assertEqual(filtered["models"][0]["source"]["url"], "https://downloads.example.test/llm.gguf")
        self.assertTrue(filtered["models"][0]["source"]["url_redacted"])
        self.assertEqual(filtered["models"][1]["source"]["url"], "https://downloads.example.test/image.safetensors")
        self.assertEqual(catalog_payload["aliases"][2]["id"], "tts-fast")
        self.assertEqual(catalog_payload["models"][0]["source"]["url"], "https://downloads.example.test/llm.gguf?token=secret")

    def test_modelhub_catalog_admin_wildcard_returns_unfiltered_sanitized_catalog(self) -> None:
        catalog_payload = {
            "object": "catalog",
            "aliases": [{"id": "chat-default"}, {"id": "tts-fast"}],
            "models": [{"id": "downloadable-llm", "source": {"url": "https://models.example.test/model.gguf?signature=secret"}}, {"id": "tts-model"}],
        }

        original_catalog = main.catalog_snapshot
        main.catalog_snapshot = lambda: SimpleNamespace(to_catalog=lambda: catalog_payload)  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main, "catalog_snapshot", original_catalog))

        public = main.modelhub_catalog_for_client(None)

        self.assertEqual([record["id"] for record in public["models"]], ["downloadable-llm", "tts-model"])
        self.assertEqual(public["models"][0]["source"]["url"], "https://models.example.test/model.gguf")
        self.assertTrue(public["models"][0]["source"]["url_redacted"])
        self.assertEqual(catalog_payload["models"][0]["source"]["url"], "https://models.example.test/model.gguf?signature=secret")

    async def test_modelhub_client_lookup_enforces_cidr_allowlist(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"modelhub:read", "modelhub:sync"}))
        client_row = {"api_client_id": "client_1", "cidr_allowlist": ["192.168.2.0/24"], "allowed_models": ["*"], "allow_downloads": True}

        async def get_modelhub_client_by_api_client(api_client_id: str) -> dict[str, Any] | None:
            self.assertEqual(api_client_id, "client_1")
            return client_row

        original_lookup = main.database.get_modelhub_client_by_api_client
        main.database.get_modelhub_client_by_api_client = get_modelhub_client_by_api_client
        self.addCleanup(lambda: setattr(main.database, "get_modelhub_client_by_api_client", original_lookup))

        token = main.current_request.set(request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.44"}))
        try:
            self.assertIs(await main.modelhub_client_for_auth(auth), client_row)
        finally:
            main.current_request.reset(token)

        token = main.current_request.set(request_for("172.18.0.10", {"X-Forwarded-For": "10.10.10.10"}))
        try:
            with self.assertRaises(HTTPException) as raised:
                await main.modelhub_client_for_auth(auth)
            self.assertEqual(raised.exception.status_code, 403)
        finally:
            main.current_request.reset(token)

    async def test_modelhub_blob_requires_acceptance_for_gated_licence(self) -> None:
        auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"}))
        record = {
            "id": "downloadable-llm",
            "version": "1.0.0",
            "downloadable": True,
            "license": {"name": "Example", "redistribution": "downloadable", "acceptance_required": True},
        }

        original_records = main.downloadable_records_for_blob
        main.downloadable_records_for_blob = lambda sha256: [record]
        self.addCleanup(lambda: setattr(main, "downloadable_records_for_blob", original_records))

        with self.assertRaises(HTTPException) as raised:
            await main.require_modelhub_blob_authorized(auth, "a" * 64, set())
        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(raised.exception.detail["required_model_refs"], ["downloadable-llm@1.0.0"])

        await main.require_modelhub_blob_authorized(auth, "a" * 64, {"downloadable-llm@1.0.0"})

    async def test_modelhub_model_and_versions_redact_source_metadata(self) -> None:
        async def authenticate(authorization: str | None = None) -> AuthContext:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"}))

        model_record = {
            "id": "downloadable-llm",
            "version": "1.0.0",
            "source": {"type": "direct-url", "url": "https://downloads.example.test/model.gguf?token=secret", "revision": "1.0.0"},
        }

        original_authenticate = main.authenticate
        original_catalog = main.catalog_snapshot
        main.authenticate = authenticate  # type: ignore[assignment]
        main.catalog_snapshot = lambda: SimpleNamespace(
            model_or_alias_record=lambda model_id: model_record,
            versions_for=lambda model_id: [model_record],
        )  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main, "authenticate", original_authenticate))
        self.addCleanup(lambda: setattr(main, "catalog_snapshot", original_catalog))

        public_model = await main.modelhub_model("downloadable-llm")
        public_versions = await main.modelhub_versions("downloadable-llm")

        self.assertEqual(public_model["source"]["url"], "https://downloads.example.test/model.gguf")
        self.assertTrue(public_model["source"]["url_redacted"])
        self.assertEqual(public_versions["versions"][0]["source"]["url"], "https://downloads.example.test/model.gguf")
        self.assertEqual(model_record["source"]["url"], "https://downloads.example.test/model.gguf?token=secret")

    async def test_modelhub_blob_rate_limit_can_be_disabled(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"modelhub:sync"}), key_prefix="b1k_test")
        main.settings = replace(main.settings, modelhub_blob_requests_per_minute=0)
        self.assertEqual(await main.enforce_modelhub_blob_rate_limit(auth), {})

    async def test_modelhub_blob_rate_limit_fallback_rejects_exhausted_window(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"modelhub:sync"}), key_prefix="b1k_test")
        main.settings = replace(main.settings, modelhub_blob_requests_per_minute=2)
        token = main.current_request.set(request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.44"}))
        try:
            first = await main.enforce_modelhub_blob_rate_limit(auth)
            second = await main.enforce_modelhub_blob_rate_limit(auth)
            with self.assertRaises(HTTPException) as raised:
                await main.enforce_modelhub_blob_rate_limit(auth)
        finally:
            main.current_request.reset(token)

        self.assertEqual(first["X-RateLimit-Limit"], "2")
        self.assertEqual(first["X-RateLimit-Remaining"], "1")
        self.assertEqual(second["X-RateLimit-Remaining"], "0")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["X-RateLimit-Remaining"], "0")

    async def test_modelhub_blob_rate_limit_fallback_prunes_expired_and_caps_subjects(self) -> None:
        original_max_subjects = main.MODELHUB_BLOB_RATE_FALLBACK_MAX_SUBJECTS
        main.MODELHUB_BLOB_RATE_FALLBACK_MAX_SUBJECTS = 2
        self.addCleanup(lambda: setattr(main, "MODELHUB_BLOB_RATE_FALLBACK_MAX_SUBJECTS", original_max_subjects))
        main.settings = replace(main.settings, modelhub_blob_requests_per_minute=5)
        main.modelhub_blob_rate_windows["expired"] = (1, 0.0)
        main.modelhub_blob_rate_windows["oldest"] = (1, main.monotonic() + 60)

        auth = AuthContext(subject_id="client_new", role=Role.SERVICE, scopes=frozenset({"modelhub:sync"}), key_prefix="b1k_new")
        token = main.current_request.set(request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.46"}))
        try:
            headers = await main.enforce_modelhub_blob_rate_limit(auth)
        finally:
            main.current_request.reset(token)

        self.assertEqual(headers["X-RateLimit-Limit"], "5")
        self.assertNotIn("expired", main.modelhub_blob_rate_windows)
        self.assertLessEqual(len(main.modelhub_blob_rate_windows), 2)

    async def test_modelhub_blob_rate_limit_uses_redis_when_available(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.counts: dict[str, int] = {}
                self.ttls: dict[str, int] = {}

            async def incr(self, key: str) -> int:
                self.counts[key] = self.counts.get(key, 0) + 1
                return self.counts[key]

            async def expire(self, key: str, seconds: int) -> bool:
                self.ttls[key] = seconds
                return True

            async def ttl(self, key: str) -> int:
                return self.ttls.get(key, -1)

        auth = AuthContext(subject_id="client_redis", role=Role.SERVICE, scopes=frozenset({"modelhub:sync"}), key_prefix="b1k_redis")
        fake_redis = FakeRedis()
        main.redis_client = fake_redis
        main.settings = replace(main.settings, modelhub_blob_requests_per_minute=1)

        token = main.current_request.set(request_for("172.18.0.10", {"X-Forwarded-For": "192.168.2.45"}))
        try:
            first = await main.enforce_modelhub_blob_rate_limit(auth)
            with self.assertRaises(HTTPException) as raised:
                await main.enforce_modelhub_blob_rate_limit(auth)
        finally:
            main.current_request.reset(token)

        self.assertEqual(first["X-RateLimit-Limit"], "1")
        self.assertEqual(first["X-RateLimit-Remaining"], "0")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(len(fake_redis.counts), 1)
        redis_key = next(iter(fake_redis.counts))
        self.assertTrue(redis_key.startswith("b1:modelhub:blob-rate:"))
        self.assertEqual(fake_redis.ttls[redis_key], main.MODELHUB_BLOB_RATE_WINDOW_SECONDS)

    async def test_modelhub_blob_proxy_injects_internal_artifact_server_token(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"modelhub:sync"}), key_prefix="b1k_test")
        calls: list[dict[str, Any]] = []

        async def authenticate(authorization: str | None = None) -> AuthContext:
            return auth

        async def require_blob_authorized(auth_context: AuthContext, sha256: str, accepted_license_refs: set[str]) -> None:
            calls.append({"authorized": sha256, "accepted": sorted(accepted_license_refs), "subject": auth_context.subject_id})

        async def enforce_rate_limit(auth_context: AuthContext) -> dict[str, str]:
            calls.append({"rate_limit": auth_context.subject_id})
            return {"X-RateLimit-Limit": "120"}

        async def proxy(base_url: str, path: str, request: Any, extra_headers: dict[str, str] | None = None) -> Any:
            calls.append(
                {
                    "base_url": base_url,
                    "path": path,
                    "extra_headers": extra_headers,
                    "client_authorization": request.headers.get("authorization"),
                }
            )
            return main.Response(content=b"blob", status_code=206)

        original_authenticate = main.authenticate
        original_require = main.require_modelhub_blob_authorized
        original_rate_limit = main.enforce_modelhub_blob_rate_limit
        original_proxy = main.proxy_http
        main.authenticate = authenticate  # type: ignore[assignment]
        main.require_modelhub_blob_authorized = require_blob_authorized  # type: ignore[assignment]
        main.enforce_modelhub_blob_rate_limit = enforce_rate_limit  # type: ignore[assignment]
        main.proxy_http = proxy  # type: ignore[assignment]
        main.settings = replace(main.settings, artifact_server_token="artifact-token")
        self.addCleanup(lambda: setattr(main, "authenticate", original_authenticate))
        self.addCleanup(lambda: setattr(main, "require_modelhub_blob_authorized", original_require))
        self.addCleanup(lambda: setattr(main, "enforce_modelhub_blob_rate_limit", original_rate_limit))
        self.addCleanup(lambda: setattr(main, "proxy_http", original_proxy))

        response = await main.modelhub_blob(
            "a" * 64,
            request_for("172.18.0.10", {"Authorization": "Bearer client-token"}),
            authorization="Bearer client-token",
            x_b1_accept_license="downloadable-llm@1.0.0",
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["X-RateLimit-Limit"], "120")
        self.assertEqual(calls[2]["extra_headers"], {"Authorization": "Bearer artifact-token"})
        self.assertEqual(calls[2]["client_authorization"], "Bearer client-token")

    async def test_proxy_http_bytes_replaces_client_authorization_with_internal_header(self) -> None:
        class FakeRequest:
            method = "GET"
            headers = {
                "authorization": "Bearer client-token",
                "host": "api.ai.b1.germering",
                "range": "bytes=0-1",
            }
            url = SimpleNamespace(query="")

            async def body(self) -> bytes:
                return b""

        class FakeResponse:
            status_code = 206
            content = b"ab"
            headers = {"content-type": "text/plain", "content-length": "2"}

        class FakeAsyncClient:
            def __init__(self, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def request(self, method: str, url: str, content: bytes, headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"method": method, "url": url, "content": content, "headers": headers})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        response = await main.proxy_http_bytes(
            "http://artifact-server:8000",
            "/artifacts/test.txt",
            FakeRequest(),
            extra_headers={"Authorization": "Bearer artifact-token"},
        )

        self.assertEqual(response.status_code, 206)
        forwarded_headers = FakeAsyncClient.calls[0]["headers"]  # type: ignore[attr-defined]
        self.assertEqual(forwarded_headers["Authorization"], "Bearer artifact-token")
        self.assertEqual(forwarded_headers["range"], "bytes=0-1")
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotIn("host", forwarded_headers)


if __name__ == "__main__":
    unittest.main()
