from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class RuntimeAdminActionApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(
        self,
        agent_result: dict[str, Any],
        graceful_result: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        runtime_states: list[dict[str, Any]] = []
        audit_events: list[dict[str, Any]] = []
        agent_calls: list[dict[str, Any]] = []

        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"runtimes:write"}))

        async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any], None]:
            agent_calls.append({"path": path, "payload": payload, "timeout_seconds": timeout_seconds})
            return agent_result, None

        async def admin_graceful_runtime_unload(runtime: str) -> dict[str, Any] | None:
            agent_calls.append({"graceful_runtime": runtime})
            return graceful_result

        async def record_audit_event(auth: Any, event_type: str, **metadata: Any) -> None:
            audit_events.append({"subject_id": auth.subject_id, "event_type": event_type, **metadata})

        class FakeDatabase:
            async def upsert_runtime_state(self, payload: dict[str, Any]) -> dict[str, Any]:
                runtime_states.append(dict(payload))
                return dict(payload)

        class FakeRegistry:
            def adapter(self, runtime: str) -> Any:
                if runtime == "localai":
                    return SimpleNamespace(name="localai", external=False)
                return None

        self.patch_attr("authenticate", authenticate)
        self.patch_attr("runtime_agent_post", runtime_agent_post)
        if graceful_result is not None:
            self.patch_attr("admin_graceful_runtime_unload", admin_graceful_runtime_unload)
        self.patch_attr("record_audit_event", record_audit_event)
        self.patch_attr("runtime_registry_snapshot", lambda: FakeRegistry())
        self.patch_attr("database", FakeDatabase())
        return runtime_states, audit_events, agent_calls

    def test_confirmed_unload_clears_runtime_state(self) -> None:
        runtime_states, audit_events, agent_calls = self.patch_common(
            {"status": "ok", "service": "localai", "action": "unload", "strategy": "backend_shutdown", "verbose": "not persisted"}
        )

        result = asyncio.run(
            main.admin_runtime_unload(
                "localai",
                main.RuntimeActionRequest(reason="operator unload", timeout_seconds=12),
                authorization="Bearer test",
            )
        )

        self.assertEqual(result["runtime"], "localai")
        self.assertEqual(result["runtime_agent"]["status"], "ok")
        self.assertEqual(agent_calls[0]["path"], "/v1/runtime-actions/localai/unload")
        self.assertEqual(agent_calls[0]["payload"]["reason"], "operator unload")
        self.assertEqual(runtime_states, [
            {
                "runtime": "localai",
                "status": "unload_ok",
                "stage": "idle_unloaded",
                "active_model": None,
                "model_alias": None,
                "resolved_model_version": None,
                "job_id": None,
                "details": {
                    "source": "admin_runtime_action",
                    "requested_by": "admin_1",
                    "reason": "operator unload",
                    "hook": {
                        "status": "ok",
                        "service": "localai",
                        "action": "unload",
                        "strategy": "backend_shutdown",
                    },
                },
            }
        ])
        self.assertEqual(audit_events[0]["event_type"], "runtime.unload_requested")

    def test_confirmed_graceful_unload_skips_runtime_agent_fallback(self) -> None:
        runtime_states, audit_events, agent_calls = self.patch_common(
            {"status": "ok", "service": "localai", "action": "unload", "strategy": "restart_service"},
            graceful_result={"status": "ok", "runtime": "localai", "action": "unload", "strategy": "backend_shutdown"},
        )

        result = asyncio.run(
            main.admin_runtime_unload(
                "localai",
                main.RuntimeActionRequest(reason="operator unload", timeout_seconds=12),
                authorization="Bearer test",
            )
        )

        self.assertEqual(agent_calls, [{"graceful_runtime": "localai"}])
        self.assertEqual(result["runtime_agent"]["strategy"], "backend_shutdown")
        self.assertEqual(result["graceful_runtime"]["status"], "ok")
        self.assertEqual(runtime_states[0]["status"], "unload_ok")
        self.assertEqual(runtime_states[0]["details"]["hook"]["strategy"], "backend_shutdown")
        self.assertEqual(audit_events[0]["metadata"]["graceful_runtime_status"], "ok")

    def test_unconfirmed_unload_leaves_runtime_state_visible(self) -> None:
        runtime_states, audit_events, _ = self.patch_common({"status": "unsupported", "service": "localai", "action": "unload"})

        result = asyncio.run(
            main.admin_runtime_unload(
                "localai",
                main.RuntimeActionRequest(reason="operator unload", timeout_seconds=12),
                authorization="Bearer test",
            )
        )

        self.assertEqual(result["runtime_agent"]["status"], "unsupported")
        self.assertEqual(runtime_states, [])
        self.assertEqual(audit_events[0]["event_type"], "runtime.unload_requested")


if __name__ == "__main__":
    unittest.main()
