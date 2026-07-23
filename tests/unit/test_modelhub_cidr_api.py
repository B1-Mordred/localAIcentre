from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
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
        main.settings = replace(self.original_settings, trusted_proxy_cidrs=("172.18.0.0/16", "127.0.0.1/32"))

    def tearDown(self) -> None:
        main.settings = self.original_settings

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


if __name__ == "__main__":
    unittest.main()
