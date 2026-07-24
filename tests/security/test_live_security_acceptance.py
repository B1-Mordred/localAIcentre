from __future__ import annotations

import json
import os
import re
import ssl
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


SECURITY_EVIDENCE_FORMAT = "b1-ai-hub-security-acceptance/v1"
SECURITY_REQUIRED_CHECKS = (
    "unauthenticated_requests_rejected",
    "under_scoped_requests_rejected",
    "cors_credentials_not_wildcard",
    "csrf_browser_mutation_rejected",
    "comfyui_management_routes_blocked",
    "import_ssrf_blocked",
    "artifact_traversal_blocked",
    "logs_redacted",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def bounded(value: str, limit: int = 1000) -> str:
    return value.strip()[:limit]


@unittest.skipUnless(os.getenv("B1_SECURITY_LIVE_TEST") == "1", "set B1_SECURITY_LIVE_TEST=1 to run deployed security acceptance")
class LiveSecurityAcceptanceTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    temp_api_client_id: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.temp_api_client_id = ""
        cls.api_base = os.getenv("B1_SECURITY_API_BASE", "https://api.ai.b1.germering").rstrip("/")
        cls.comfy_base = os.getenv("B1_SECURITY_COMFY_BASE", "https://comfy.ai.b1.germering").rstrip("/")
        cls.api_key = os.getenv("B1_SECURITY_API_KEY") or os.getenv("B1_SMOKE_ADMIN_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or ""
        cls.under_scoped_key = os.getenv("B1_SECURITY_UNDERSCOPED_API_KEY", "").strip()
        cls.timeout_seconds = float(os.getenv("B1_SECURITY_TIMEOUT_SECONDS", "30"))
        cls.api_host_header = os.getenv("B1_SECURITY_API_HOST_HEADER", "").strip()
        cls.comfy_host_header = os.getenv("B1_SECURITY_COMFY_HOST_HEADER", "").strip()
        cls.session_cookie_name = os.getenv("B1_SECURITY_SESSION_COOKIE_NAME", "b1_ai_hub_session")
        cls.browser_cookie = os.getenv("B1_SECURITY_BROWSER_SESSION_COOKIE", "").strip()
        if not cls.api_key:
            raise unittest.SkipTest("set B1_SECURITY_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY to an admin key")
        if not cls.under_scoped_key and env_flag("B1_SECURITY_CREATE_TEMP_UNDERSCOPED_CLIENT", False):
            cls.under_scoped_key = cls.create_temp_under_scoped_client()
        if not cls.under_scoped_key:
            raise unittest.SkipTest("set B1_SECURITY_UNDERSCOPED_API_KEY or B1_SECURITY_CREATE_TEMP_UNDERSCOPED_CLIENT=1")
        if not cls.browser_cookie:
            cls.browser_cookie = cls.login_browser_session()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.temp_api_client_id:
            try:
                cls.request_json(
                    cls.api_base,
                    "DELETE",
                    f"/admin/api-clients/{urllib.parse.quote(cls.temp_api_client_id)}",
                    token=cls.api_key,
                    allow_http_error=True,
                )
            except AssertionError:
                pass
        evidence_path = os.getenv("B1_SECURITY_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in SECURITY_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": SECURITY_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.api_base,
                    "comfy_base_url": cls.comfy_base,
                    "status": status,
                    "required_checks": list(SECURITY_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def ssl_context(cls, base_url: str) -> ssl.SSLContext | None:
        if not base_url.lower().startswith("https://"):
            return None
        if not env_flag("B1_SECURITY_TLS_VERIFY", True):
            return ssl._create_unverified_context()
        ca_file = os.getenv("B1_SECURITY_CA_FILE", "").strip()
        if ca_file:
            return ssl.create_default_context(cafile=ca_file)
        return ssl.create_default_context()

    @classmethod
    def host_header(cls, base_url: str) -> str:
        if base_url == cls.comfy_base:
            return cls.comfy_host_header
        return cls.api_host_header

    @classmethod
    def request_raw(
        cls,
        base_url: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | bytes | None = None,
        token: str = "",
        headers: dict[str, str] | None = None,
        cookie: str = "",
        allow_http_error: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        host = cls.host_header(base_url)
        if host:
            request_headers["Host"] = host
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if cookie:
            request_headers["Cookie"] = cookie
        data: bytes | None
        if isinstance(body, dict):
            data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            data = body
        url = urllib.parse.urljoin(base_url + "/", path.lstrip("/"))
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=cls.timeout_seconds, context=cls.ssl_context(base_url)) as response:
                return int(getattr(response, "status", response.getcode())), dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            if allow_http_error:
                return int(exc.code), dict(exc.headers.items()), body_bytes
            raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {body_bytes[:300]!r}") from exc
        except urllib.error.URLError as exc:
            raise AssertionError(f"{method} {path} failed: {exc.reason}") from exc

    @classmethod
    def request_json(
        cls,
        base_url: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str = "",
        headers: dict[str, str] | None = None,
        cookie: str = "",
        allow_http_error: bool = False,
    ) -> tuple[int, dict[str, str], Any]:
        status, response_headers, raw = cls.request_raw(
            base_url,
            method,
            path,
            body=body,
            token=token,
            headers=headers,
            cookie=cookie,
            allow_http_error=allow_http_error,
        )
        if not raw:
            return status, response_headers, None
        try:
            return status, response_headers, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return status, response_headers, {"non_json_body": raw[:300].decode("utf-8", errors="replace")}

    @classmethod
    def create_temp_under_scoped_client(cls) -> str:
        display_name = f"security-acceptance-{uuid.uuid4().hex[:8]}"
        status, _headers, payload = cls.request_json(
            cls.api_base,
            "POST",
            "/admin/api-clients",
            body={"display_name": display_name, "role": "user", "scopes": ["models:read"], "cidr_allowlist": []},
            token=cls.api_key,
            allow_http_error=True,
        )
        if status != 200 or not isinstance(payload, dict) or not payload.get("api_key"):
            raise unittest.SkipTest(f"could not create temporary under-scoped API client: HTTP {status}")
        cls.temp_api_client_id = str(payload.get("id") or "")
        return str(payload["api_key"])

    @classmethod
    def login_browser_session(cls) -> str:
        username = os.getenv("B1_SECURITY_BROWSER_USERNAME", "").strip()
        password = os.getenv("B1_SECURITY_BROWSER_PASSWORD", "")
        if not username or not password:
            raise unittest.SkipTest("set B1_SECURITY_BROWSER_SESSION_COOKIE or B1_SECURITY_BROWSER_USERNAME/B1_SECURITY_BROWSER_PASSWORD")
        status, headers, payload = cls.request_json(
            cls.api_base,
            "POST",
            "/auth/login",
            body={"username": username, "password": password},
            allow_http_error=True,
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("authenticated") is not True:
            raise unittest.SkipTest(f"browser login failed for CSRF acceptance check: HTTP {status}")
        cookie = SimpleCookie()
        cookie.load(headers.get("Set-Cookie", ""))
        morsel = cookie.get(cls.session_cookie_name)
        if morsel is None or not morsel.value:
            raise unittest.SkipTest("browser login did not return the configured session cookie")
        return f"{cls.session_cookie_name}={morsel.value}"

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def sample(self, label: str, **data: Any) -> None:
        self.samples.append({"label": label, **data})

    def test_deployed_security_controls(self) -> None:
        self.verify_unauthenticated_rejected()
        self.verify_under_scoped_rejected()
        self.verify_cors_denied_without_wildcard_credentials()
        self.verify_csrf_browser_mutation_rejected()
        self.verify_comfyui_management_route_blocked()
        self.verify_import_ssrf_blocked()
        self.verify_artifact_traversal_blocked()
        self.verify_logs_redacted()

    def verify_unauthenticated_rejected(self) -> None:
        status, _headers, payload = self.request_json(self.api_base, "GET", "/admin/self-test", allow_http_error=True)
        self.assertEqual(status, 401, payload)
        self.record_check("unauthenticated_requests_rejected", path="/admin/self-test", http_status=status)
        self.sample("unauthenticated-admin", path="/admin/self-test", http_status=status)

    def verify_under_scoped_rejected(self) -> None:
        status, _headers, status_payload = self.request_json(
            self.api_base,
            "GET",
            "/auth/status",
            token=self.under_scoped_key,
            allow_http_error=True,
        )
        self.assertEqual(status, 200, status_payload)
        self.assertIsInstance(status_payload, dict)
        self.assertTrue(status_payload.get("authenticated"), status_payload)
        status, _headers, payload = self.request_json(
            self.api_base,
            "GET",
            "/admin/self-test",
            token=self.under_scoped_key,
            allow_http_error=True,
        )
        self.assertEqual(status, 403, payload)
        self.record_check(
            "under_scoped_requests_rejected",
            auth_status=bool(status_payload.get("authenticated")),
            rejected_path="/admin/self-test",
            http_status=status,
            temporary_client=bool(self.temp_api_client_id),
        )
        self.sample("under-scoped-admin", path="/admin/self-test", http_status=status)

    def verify_cors_denied_without_wildcard_credentials(self) -> None:
        origin = os.getenv("B1_SECURITY_BLOCKED_ORIGIN", "https://evil.example").strip()
        status, headers, payload = self.request_json(
            self.api_base,
            "OPTIONS",
            "/admin/self-test",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            allow_http_error=True,
        )
        header_map = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(status, 400, payload)
        self.assertNotEqual(header_map.get("access-control-allow-origin"), "*")
        self.assertFalse(
            header_map.get("access-control-allow-origin") == "*" and header_map.get("access-control-allow-credentials", "").lower() == "true"
        )
        self.record_check(
            "cors_credentials_not_wildcard",
            blocked_origin=origin,
            http_status=status,
            allow_origin=header_map.get("access-control-allow-origin", ""),
            allow_credentials=header_map.get("access-control-allow-credentials", ""),
        )
        self.sample("cors-denied-origin", http_status=status, allow_origin=header_map.get("access-control-allow-origin", ""))

    def verify_csrf_browser_mutation_rejected(self) -> None:
        status, _headers, payload = self.request_json(
            self.api_base,
            "POST",
            "/admin/network-policy/validate",
            body={"cors_allow_origins": [], "trusted_proxy_cidrs": []},
            cookie=self.browser_cookie,
            allow_http_error=True,
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("CSRF", json.dumps(payload, sort_keys=True))
        self.record_check("csrf_browser_mutation_rejected", path="/admin/network-policy/validate", http_status=status)
        self.sample("csrf-missing-token", path="/admin/network-policy/validate", http_status=status)

    def verify_comfyui_management_route_blocked(self) -> None:
        path = os.getenv("B1_SECURITY_COMFY_DENIED_PATH", "/api/manager/install")
        status, _headers, payload = self.request_json(self.comfy_base, "POST", path, body={"url": "https://example.invalid/node"}, allow_http_error=True)
        self.assertEqual(status, 403, payload)
        self.assertIn("comfyui_route_denied", json.dumps(payload, sort_keys=True))
        self.record_check("comfyui_management_routes_blocked", path=path, http_status=status)
        self.sample("comfyui-manager-denied", path=path, http_status=status)

    def verify_import_ssrf_blocked(self) -> None:
        manifest_url = os.getenv("B1_SECURITY_SSRF_MANIFEST_URL", "http://127.0.0.1:1/manifest.json")
        status, _headers, payload = self.request_json(
            self.api_base,
            "POST",
            "/admin/models/download-plan",
            body={"manifest_url": manifest_url},
            token=self.api_key,
            allow_http_error=True,
        )
        self.assertEqual(status, 422, payload)
        self.assertIn("public HTTPS URL", json.dumps(payload, sort_keys=True))
        parsed = urllib.parse.urlsplit(manifest_url)
        self.record_check(
            "import_ssrf_blocked",
            path="/admin/models/download-plan",
            http_status=status,
            rejected_scheme=parsed.scheme,
            rejected_host=parsed.hostname or "",
        )
        self.sample("manifest-ssrf-denied", http_status=status, rejected_host=parsed.hostname or "")

    def verify_artifact_traversal_blocked(self) -> None:
        path = os.getenv("B1_SECURITY_TRAVERSAL_ARTIFACT_PATH", "/artifacts/%2e%2e/secrets/master_encryption_key")
        status, _headers, body = self.request_raw(self.api_base, "GET", path, token=self.api_key, allow_http_error=True)
        self.assertIn(status, {400, 403, 404}, body[:300])
        self.assertLess(len(body), 4096)
        self.record_check("artifact_traversal_blocked", path=path, http_status=status, response_bytes=len(body))
        self.sample("artifact-traversal-denied", path=path, http_status=status)

    def verify_logs_redacted(self) -> None:
        service = os.getenv("B1_SECURITY_LOG_SERVICE", "control-plane")
        lines = int(os.getenv("B1_SECURITY_LOG_LINES", "200"))
        status, _headers, payload = self.request_json(
            self.api_base,
            "GET",
            f"/admin/services/{urllib.parse.quote(service)}/logs?lines={max(1, min(lines, 500))}",
            token=self.api_key,
            allow_http_error=True,
        )
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        entries = payload.get("entries")
        self.assertIsInstance(entries, list)
        text = "\n".join(str(entry) for entry in entries)
        secret_values = [
            self.api_key,
            self.under_scoped_key,
            os.getenv("B1_SECURITY_BROWSER_PASSWORD", ""),
            os.getenv("B1_SECURITY_SECRET_CANARY", ""),
        ]
        for value in secret_values:
            if value:
                self.assertNotIn(value, text)
        self.assertNotIn("github_pat_", text)
        self.assertIsNone(re.search(r"Authorization:\s*Bearer\s+(?!<redacted>)[A-Za-z0-9._~+/=-]+", text, re.IGNORECASE))
        self.record_check("logs_redacted", service=service, http_status=status, line_count=len(entries))
        self.sample("service-logs-redacted", service=service, http_status=status, line_count=len(entries))


if __name__ == "__main__":
    unittest.main()
