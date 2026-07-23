from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ServiceLogsApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, auth: Any) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", authenticate)

    def test_operator_can_fetch_bounded_redacted_service_logs(self) -> None:
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"runtimes:read"})))
        calls: list[str] = []

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            calls.append(path)
            return {
                "service": "control-plane",
                "lines": 50,
                "entries": [
                    "ok",
                    "Authorization: Bearer provider-token",
                    "key b1k_public.secret and b1adm_adminsecret",
                    "x" * 5000,
                ],
            }, None

        self.patch_attr("runtime_agent_get", runtime_agent_get)

        result = asyncio.run(main.admin_service_logs("CONTROL-PLANE", authorization="Bearer key", lines=50))

        self.assertEqual(calls, ["/v1/services/control-plane/logs?lines=50"])
        self.assertEqual(result["service"], "control-plane")
        self.assertEqual(result["lines"], 50)
        joined = "\n".join(result["entries"])
        self.assertIn("Authorization: Bearer <redacted>", joined)
        self.assertNotIn("provider-token", joined)
        self.assertNotIn("b1k_public.secret", joined)
        self.assertNotIn("b1adm_adminsecret", joined)
        self.assertIn("...<truncated>", result["entries"][-1])

    def test_service_logs_reject_bad_service_and_non_operator(self) -> None:
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"runtimes:read"})))

        with self.assertRaises(HTTPException) as bad_service:
            asyncio.run(main.admin_service_logs("../postgres", authorization="Bearer key"))
        self.assertEqual(bad_service.exception.status_code, 404)

        self.patch_auth(AuthContext(subject_id="service_1", role=Role.SERVICE, scopes=frozenset({"runtimes:read"})))
        with self.assertRaises(HTTPException) as bad_role:
            asyncio.run(main.admin_service_logs("control-plane", authorization="Bearer key"))
        self.assertEqual(bad_role.exception.status_code, 403)

    def test_service_logs_propagates_runtime_agent_errors(self) -> None:
        self.patch_auth(AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"runtimes:read"})))

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            return None, "runtime-agent /logs returned HTTP 502"

        self.patch_attr("runtime_agent_get", runtime_agent_get)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_service_logs("gateway", authorization="Bearer key"))
        self.assertEqual(caught.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
