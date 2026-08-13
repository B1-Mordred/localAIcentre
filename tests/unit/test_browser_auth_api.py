from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
    from app.auth import hash_session_token  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    hash_session_token = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeRequest:
    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        client_host: str = "127.0.0.1",
    ) -> None:
        self.method = method
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.url = SimpleNamespace(path=path)
        self.client = SimpleNamespace(host=client_host)


class FakeDatabase:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.api_clients: dict[str, dict[str, Any]] = {}
        self.modelhub_clients: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.login_marks: list[str] = []

    async def active_user_count(self, role: str | None = None) -> int:
        return sum(
            1
            for user in self.users.values()
            if user.get("disabled_at") is None and (role is None or user["role"] == role)
        )

    async def insert_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {**payload, "disabled_at": None}
        self.users[row["id"]] = row
        return row

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self.users.get(user_id)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        normalized = username.strip().casefold()
        for user in self.users.values():
            if user["username"].casefold() == normalized and user.get("disabled_at") is None:
                return user
        return None

    async def mark_user_login(self, user_id: str) -> None:
        self.login_marks.append(user_id)

    async def insert_browser_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sessions[payload["session_hash"]] = dict(payload)
        return dict(payload)

    async def get_browser_session_by_hash(self, session_hash: str) -> dict[str, Any] | None:
        return self.sessions.get(session_hash)

    async def revoke_browser_session_by_hash(self, session_hash: str) -> dict[str, Any] | None:
        row = self.sessions.get(session_hash)
        if row is not None:
            row["revoked_at"] = "now"
        return row

    async def insert_audit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.audit_events.append(payload)
        return {"id": f"audit_{len(self.audit_events)}", **payload}

    async def upsert_api_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.api_clients[payload["id"]] = dict(payload)
        return dict(payload)

    async def insert_api_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {**payload, "last_used_at": None, "revoked_at": None}
        self.api_clients[row["id"]] = row
        return dict(row)

    async def get_api_client_by_prefix(self, key_prefix: str) -> dict[str, Any] | None:
        for client in self.api_clients.values():
            if client.get("key_prefix") == key_prefix and client.get("revoked_at") is None:
                client["last_used_at"] = "now"
                return dict(client)
        return None

    async def update_api_client_cidr_allowlist(self, client_id: str, cidr_allowlist: list[str]) -> dict[str, Any] | None:
        row = self.api_clients.get(client_id)
        if row is None:
            return None
        if row.get("revoked_at") is None:
            row["cidr_allowlist"] = cidr_allowlist
        return dict(row)

    async def update_api_client_role_and_scopes(
        self,
        client_id: str,
        role: str,
        scopes: list[str],
    ) -> dict[str, Any] | None:
        row = self.api_clients.get(client_id)
        if row is None:
            return None
        if row.get("revoked_at") is None:
            row["role"] = role
            row["scopes"] = list(scopes)
        return dict(row)

    async def update_modelhub_client_cidr_allowlist(self, client_id: str, cidr_allowlist: list[str]) -> dict[str, Any] | None:
        row = self.modelhub_clients.get(client_id)
        if row is None:
            return None
        if row.get("revoked_at") is None:
            row["cidr_allowlist"] = cidr_allowlist
            api_row = self.api_clients.get(row["api_client_id"])
            if api_row is not None:
                api_row["cidr_allowlist"] = cidr_allowlist
        return dict(row)

    async def get_modelhub_client(self, client_id: str) -> dict[str, Any] | None:
        row = self.modelhub_clients.get(client_id)
        return dict(row) if row is not None else None

    async def get_network_policy_record(self) -> dict[str, Any] | None:
        row = getattr(self, "network_policy", None)
        return dict(row) if row is not None else None

    async def upsert_network_policy_record(self, payload: dict[str, Any], policy_id: str = "default") -> dict[str, Any]:
        row = {"id": policy_id, "created_at": "created", "updated_at": "updated", **payload}
        self.network_policy = row
        return dict(row)

    async def delete_network_policy_record(self, policy_id: str = "default") -> dict[str, Any] | None:
        row = getattr(self, "network_policy", None)
        self.network_policy = None
        return dict(row) if row is not None else None

    async def update_modelhub_client_policy(
        self,
        client_id: str,
        *,
        allowed_models: list[str],
        allow_downloads: bool,
    ) -> dict[str, Any] | None:
        row = self.modelhub_clients.get(client_id)
        if row is None:
            return None
        if row.get("revoked_at") is None:
            row["allowed_models"] = allowed_models
            row["allow_downloads"] = allow_downloads
        return dict(row)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class BrowserAuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_database = main.database
        self.original_settings = main.settings
        self.original_model_catalog = main.model_catalog
        self.original_network_policy_cache = main.network_policy_cache
        self.database = FakeDatabase()
        main.database = self.database
        main.settings = replace(
            main.settings,
            dev_auth_bypass=False,
            admin_bootstrap_key="setup-key",
            open_webui_api_key="",
            session_cookie_secure=False,
            session_ttl_seconds=3600,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            model_catalog_dir=str(ROOT / "model-catalog"),
        )
        main.model_catalog = None
        main.network_policy_cache = None

    def tearDown(self) -> None:
        main.database = self.original_database
        main.settings = self.original_settings
        main.model_catalog = self.original_model_catalog
        main.network_policy_cache = self.original_network_policy_cache

    def request_context(self, request: FakeRequest):
        token = main.current_request.set(request)
        self.addCleanup(lambda: main.current_request.reset(token))

    def response_json(self, response: Any) -> dict[str, Any]:
        return json.loads(response.body.decode("utf-8"))

    def patch_auth_context(self, auth: Any) -> None:
        original = main.authenticate

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        main.authenticate = fake_authenticate
        self.addCleanup(lambda: setattr(main, "authenticate", original))

    def setup_admin(self) -> tuple[str, dict[str, Any]]:
        self.request_context(FakeRequest(method="POST", path="/auth/setup", headers={"user-agent": "test"}))
        response = asyncio.run(
            main.auth_setup(
                main.AuthSetupRequest(
                    username="admin",
                    password="Correct-Horse-7",
                    bootstrap_key="setup-key",
                )
            )
        )
        body = self.response_json(response)
        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        return cookie, body

    def test_initial_admin_setup_issues_http_only_session(self) -> None:
        session_token, body = self.setup_admin()

        self.assertFalse(body["setup_required"])
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["role"], "admin")
        self.assertTrue(body["csrf_token"])
        self.assertIn(hash_session_token(session_token), self.database.sessions)
        self.assertIn("auth.initial_admin_created", [event["event_type"] for event in self.database.audit_events])

    def test_status_authenticates_with_browser_session_cookie(self) -> None:
        session_token, setup_body = self.setup_admin()
        self.request_context(FakeRequest(cookies={main.settings.session_cookie_name: session_token}))

        status = asyncio.run(main.auth_status())

        self.assertTrue(status["authenticated"])
        self.assertEqual(status["csrf_token"], setup_body["csrf_token"])

    def test_csrf_required_for_cookie_backed_mutations(self) -> None:
        session_token, setup_body = self.setup_admin()
        missing = FakeRequest(
            method="POST",
            path="/admin/models/install",
            cookies={main.settings.session_cookie_name: session_token},
        )
        valid = FakeRequest(
            method="POST",
            path="/admin/models/install",
            cookies={main.settings.session_cookie_name: session_token},
            headers={"X-B1-CSRF": setup_body["csrf_token"]},
        )

        failure = asyncio.run(main.csrf_failure_response(missing))
        success = asyncio.run(main.csrf_failure_response(valid))

        self.assertIsNotNone(failure)
        self.assertEqual(failure.status_code, 403)
        self.assertIsNone(success)

    def test_setup_requires_bootstrap_key_when_configured(self) -> None:
        self.request_context(FakeRequest(method="POST", path="/auth/setup"))

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.auth_setup(
                    main.AuthSetupRequest(
                        username="admin",
                        password="Correct-Horse-7",
                        bootstrap_key="wrong",
                    )
                )
            )

        self.assertEqual(caught.exception.status_code, 403)

    def test_credential_management_routes_require_admin_guard_in_source(self) -> None:
        source = (ROOT / "services" / "control-plane" / "app" / "main.py").read_text(encoding="utf-8")
        route_handlers = [
            "admin_api_clients",
            "admin_api_client_create",
            "admin_api_client_cidr_update",
            "admin_api_client_role_update",
            "admin_api_client_revoke",
            "modelhub_clients",
            "modelhub_client_create",
            "modelhub_client_cidr_update",
            "modelhub_client_policy_update",
            "modelhub_client_delete",
        ]
        for handler in route_handlers:
            with self.subTest(handler=handler):
                start = source.index(f"async def {handler}")
                end = source.find("\n@app.", start + 1)
                body = source[start:] if end == -1 else source[start:end]
                self.assertIn("require_credential_admin(auth)", body)

    def test_operator_cannot_manage_credential_clients_even_with_admin_scopes(self) -> None:
        self.patch_auth_context(
            main.AuthContext(
                subject_id="operator_1",
                role=main.Role.OPERATOR,
                scopes=frozenset({"admin:read", "admin:write"}),
            )
        )

        attempts = [
            lambda: main.admin_api_clients(),
            lambda: main.admin_api_client_create(main.ApiClientCreate(display_name="worker", role=main.Role.SERVICE)),
            lambda: main.modelhub_clients(),
            lambda: main.modelhub_client_create(main.ModelHubClientCreate(display_name="sync-client")),
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                with self.assertRaises(main.HTTPException) as caught:
                    asyncio.run(attempt())
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(caught.exception.detail, "credential management requires administrator role")

        self.assertEqual(self.database.api_clients, {})
        self.assertEqual(self.database.modelhub_clients, {})

    def test_open_webui_api_client_is_provisioned_from_generated_secret(self) -> None:
        main.settings = replace(main.settings, open_webui_api_key="b1k_openwebui.test-secret")

        row = asyncio.run(main.ensure_open_webui_api_client())

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], main.OPEN_WEBUI_CLIENT_ID)
        self.assertEqual(row["role"], "service")
        self.assertEqual(row["key_prefix"], "b1k_openwebui")
        self.assertEqual(
            set(row["scopes"]),
            {"models:read", "inference:write", "jobs:read", "jobs:write", "workflows:read"},
        )
        self.assertIn(main.OPEN_WEBUI_CLIENT_ID, self.database.api_clients)

    def test_api_client_create_validates_and_returns_cidr_allowlist(self) -> None:
        created = asyncio.run(
            main.admin_api_client_create(
                main.ApiClientCreate(
                    display_name="worker",
                    role=main.Role.SERVICE,
                    scopes=["models:read"],
                    cidr_allowlist=["192.168.2.44/24", "192.168.2.0/24"],
                ),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(created["cidr_allowlist"], ["192.168.2.0/24"])
        self.assertNotIn("key_hash", created)
        self.assertTrue(created["api_key"].startswith(created["key_prefix"] + "."))

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.admin_api_client_create(
                    main.ApiClientCreate(display_name="bad", cidr_allowlist=["not-a-network"]),
                    authorization="Bearer setup-key",
                )
            )
        self.assertEqual(caught.exception.status_code, 422)

    def test_api_client_cidr_allowlist_is_enforced_on_bearer_auth(self) -> None:
        prefix, api_key = main.generate_api_key()
        salt, digest = main.hash_api_key(api_key)
        self.database.api_clients["client_worker"] = {
            "id": "client_worker",
            "display_name": "worker",
            "role": "service",
            "scopes": ["models:read"],
            "key_prefix": prefix,
            "key_salt": salt,
            "key_hash": digest,
            "cidr_allowlist": ["192.168.2.0/24"],
            "revoked_at": None,
        }

        self.request_context(FakeRequest(headers={"x-forwarded-for": "192.168.2.44"}))
        auth = asyncio.run(main.authenticate(f"Bearer {api_key}"))
        self.assertEqual(auth.subject_id, "client_worker")

        self.request_context(FakeRequest(headers={"x-forwarded-for": "10.10.10.10"}))
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.authenticate(f"Bearer {api_key}"))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "API client is not permitted from this network")

    def test_api_client_cidr_allowlist_can_be_updated(self) -> None:
        self.database.api_clients["client_worker"] = {
            "id": "client_worker",
            "display_name": "worker",
            "role": "service",
            "scopes": ["models:read"],
            "key_prefix": "b1k_worker",
            "key_salt": "salt",
            "key_hash": "hash",
            "cidr_allowlist": [],
            "revoked_at": None,
        }

        updated = asyncio.run(
            main.admin_api_client_cidr_update(
                "client_worker",
                main.CidrAllowlistUpdateRequest(cidr_allowlist=["10.0.1.42/24", "10.0.1.0/24"]),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(updated["cidr_allowlist"], ["10.0.1.0/24"])
        self.assertEqual(self.database.api_clients["client_worker"]["cidr_allowlist"], ["10.0.1.0/24"])
        self.assertIn("api_client.cidr_allowlist_updated", [event["event_type"] for event in self.database.audit_events])

    def test_api_client_role_can_be_updated_without_exposing_credential(self) -> None:
        self.database.api_clients["client_dialecticore"] = {
            "id": "client_dialecticore",
            "display_name": "DialectiCore",
            "role": "service",
            "scopes": ["jobs:read", "jobs:write", "models:read"],
            "key_prefix": "b1k_dialecticore",
            "key_salt": "salt",
            "key_hash": "hash",
            "cidr_allowlist": [],
            "revoked_at": None,
        }

        updated = asyncio.run(
            main.admin_api_client_role_update(
                "client_dialecticore",
                main.ApiClientRoleUpdateRequest(role=main.Role.OPERATOR),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(updated["role"], "operator")
        self.assertIn("workflows:write", updated["scopes"])
        self.assertNotIn("key_hash", updated)
        self.assertNotIn("key_salt", updated)
        self.assertIn("api_client.role_updated", [event["event_type"] for event in self.database.audit_events])

    def test_modelhub_client_cidr_allowlist_update_syncs_backing_api_client(self) -> None:
        self.database.api_clients["client_hub"] = {
            "id": "client_hub",
            "display_name": "Model Hub: artist",
            "role": "service",
            "scopes": ["modelhub:read", "modelhub:sync"],
            "key_prefix": "b1k_hub",
            "key_salt": "salt",
            "key_hash": "hash",
            "cidr_allowlist": [],
            "revoked_at": None,
        }
        self.database.modelhub_clients["mhc_artist"] = {
            "id": "mhc_artist",
            "display_name": "artist",
            "owner_id": "admin_1",
            "api_client_id": "client_hub",
            "key_prefix": "b1k_hub",
            "allowed_models": ["image-default"],
            "cidr_allowlist": [],
            "allow_downloads": True,
            "revoked_at": None,
        }

        updated = asyncio.run(
            main.modelhub_client_cidr_update(
                "mhc_artist",
                main.CidrAllowlistUpdateRequest(cidr_allowlist=["192.168.8.4/24"]),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(updated["cidr_allowlist"], ["192.168.8.0/24"])
        self.assertEqual(self.database.api_clients["client_hub"]["cidr_allowlist"], ["192.168.8.0/24"])
        self.assertIn("modelhub_client.cidr_allowlist_updated", [event["event_type"] for event in self.database.audit_events])

    def test_modelhub_client_policy_can_be_updated(self) -> None:
        self.database.modelhub_clients["mhc_artist"] = {
            "id": "mhc_artist",
            "display_name": "artist",
            "owner_id": "admin_1",
            "api_client_id": "client_hub",
            "key_prefix": "b1k_hub",
            "allowed_models": ["image-default"],
            "cidr_allowlist": [],
            "allow_downloads": True,
            "revoked_at": None,
        }

        updated = asyncio.run(
            main.modelhub_client_policy_update(
                "mhc_artist",
                main.ModelHubClientPolicyUpdate(allowed_models=["chat-default", "image-default"], allow_downloads=False),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(updated["allowed_models"], ["chat-default", "image-default"])
        self.assertFalse(updated["allow_downloads"])
        self.assertEqual(self.database.modelhub_clients["mhc_artist"]["allowed_models"], ["chat-default", "image-default"])
        self.assertFalse(self.database.modelhub_clients["mhc_artist"]["allow_downloads"])
        self.assertIn("modelhub_client.policy_updated", [event["event_type"] for event in self.database.audit_events])

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.modelhub_client_policy_update(
                    "mhc_artist",
                    main.ModelHubClientPolicyUpdate(allowed_models=["missing-alias"], allow_downloads=True),
                    authorization="Bearer setup-key",
                )
            )
        self.assertEqual(caught.exception.status_code, 422)

    def test_revoked_modelhub_client_policy_cannot_be_updated(self) -> None:
        self.database.modelhub_clients["mhc_revoked"] = {
            "id": "mhc_revoked",
            "display_name": "revoked",
            "owner_id": "admin_1",
            "api_client_id": "client_hub",
            "key_prefix": "b1k_hub",
            "allowed_models": ["image-default"],
            "cidr_allowlist": [],
            "allow_downloads": True,
            "revoked_at": "now",
        }

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.modelhub_client_policy_update(
                    "mhc_revoked",
                    main.ModelHubClientPolicyUpdate(allowed_models=["chat-default"], allow_downloads=False),
                    authorization="Bearer setup-key",
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_network_policy_can_be_updated_and_reset(self) -> None:
        updated = asyncio.run(
            main.admin_network_policy_update(
                main.NetworkPolicyUpdateRequest(
                    cors_allow_origins=[
                        "https://control.ai.b1.germering/",
                        "https://CONTROL.ai.b1.germering",
                        "http://localhost:5173",
                    ],
                    trusted_proxy_cidrs=["10.0.0.44/24", "127.0.0.1/32"],
                ),
                authorization="Bearer setup-key",
            )
        )

        self.assertEqual(updated["source"], "database")
        self.assertEqual(updated["effective"]["cors_allow_origins"], ["https://control.ai.b1.germering", "http://localhost:5173"])
        self.assertEqual(updated["effective"]["trusted_proxy_cidrs"], ["10.0.0.0/24", "127.0.0.1/32"])
        self.assertEqual(main.network_policy_cache["cors_allow_origins"], ["https://control.ai.b1.germering", "http://localhost:5173"])
        self.assertIn("network_policy.updated", [event["event_type"] for event in self.database.audit_events])

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(
                main.admin_network_policy_update(
                    main.NetworkPolicyUpdateRequest(cors_allow_origins=["*"], trusted_proxy_cidrs=[]),
                    authorization="Bearer setup-key",
                )
            )
        self.assertEqual(caught.exception.status_code, 422)

        reset = asyncio.run(main.admin_network_policy_reset(authorization="Bearer setup-key"))
        self.assertEqual(reset["source"], "environment")
        self.assertIsNone(main.network_policy_cache)
        self.assertIn("network_policy.reset", [event["event_type"] for event in self.database.audit_events])

    def test_dynamic_cors_middleware_uses_network_policy(self) -> None:
        main.network_policy_cache = {
            "id": "default",
            "cors_allow_origins": ["https://control.ai.b1.germering"],
            "trusted_proxy_cidrs": ["127.0.0.1/32"],
        }

        allowed = FakeRequest(
            method="OPTIONS",
            headers={"origin": "https://control.ai.b1.germering", "access-control-request-method": "POST"},
        )
        blocked = FakeRequest(
            method="OPTIONS",
            headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
        )

        async def call_next(request: FakeRequest) -> Any:
            return main.Response("miss")

        allowed_response = asyncio.run(main.request_context_and_csrf_middleware(allowed, call_next))
        blocked_response = asyncio.run(main.request_context_and_csrf_middleware(blocked, call_next))

        self.assertEqual(allowed_response.status_code, 204)
        self.assertEqual(allowed_response.headers["Access-Control-Allow-Origin"], "https://control.ai.b1.germering")
        self.assertEqual(allowed_response.headers["Access-Control-Allow-Credentials"], "true")
        self.assertNotEqual(allowed_response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(blocked_response.status_code, 400)

    def test_network_policy_trusted_proxy_cidrs_drive_client_host(self) -> None:
        main.network_policy_cache = {
            "id": "default",
            "cors_allow_origins": [],
            "trusted_proxy_cidrs": ["10.0.0.0/8"],
        }
        trusted = FakeRequest(headers={"x-forwarded-for": "192.168.2.55"}, client_host="10.1.2.3")
        untrusted = FakeRequest(headers={"x-forwarded-for": "192.168.2.55"}, client_host="172.20.0.4")

        self.assertEqual(main.client_host(trusted), "192.168.2.55")
        self.assertEqual(main.client_host(untrusted), "172.20.0.4")


if __name__ == "__main__":
    unittest.main()
