from __future__ import annotations

import json
import os
import sys
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from test_live_stack import LiveApiClient  # noqa: E402


GPU_RUNTIMES = {"localai", "comfyui", "voicebox"}
REQUIRED_CHECKS = ("streaming_chat_completed", "single_backend_enforced", "graceful_unload_verified")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(
    os.getenv("B1_LOCALAI_ACCEPTANCE_LIVE_TEST") == "1",
    "set B1_LOCALAI_ACCEPTANCE_LIVE_TEST=1 to run live LocalAI runtime acceptance",
)
class LiveLocalAiRuntimeAcceptanceTests(unittest.TestCase):
    evidence: list[dict[str, Any]] = []
    checks: dict[str, dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = []
        cls.checks = {}
        api_key = os.getenv("B1_LOCALAI_ACCEPTANCE_API_KEY") or os.getenv("B1_SMOKE_ADMIN_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or ""
        if not api_key:
            raise unittest.SkipTest("set B1_LOCALAI_ACCEPTANCE_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
        tls_verify = os.getenv("B1_LOCALAI_ACCEPTANCE_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        cls.client = LiveApiClient(
            os.getenv("B1_LOCALAI_ACCEPTANCE_API_BASE") or os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_LOCALAI_ACCEPTANCE_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_LOCALAI_ACCEPTANCE_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "120"))),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_LOCALAI_ACCEPTANCE_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.model = os.getenv("B1_LOCALAI_ACCEPTANCE_CHAT_MODEL", "chat-default")
        cls.unload_timeout_seconds = int(os.getenv("B1_LOCALAI_ACCEPTANCE_UNLOAD_TIMEOUT_SECONDS", "30"))
        cls.unload_settle_seconds = float(os.getenv("B1_LOCALAI_ACCEPTANCE_UNLOAD_SETTLE_SECONDS", "20"))
        cls.require_production = env_flag("B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION", True)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_LOCALAI_ACCEPTANCE_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": "b1-ai-hub-localai-runtime-acceptance/v1",
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.client.base_url,
                    "status": status,
                    "required_checks": list(REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.evidence,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def json_request(self, method: str, path: str, *, body: dict[str, Any] | None = None, expected: int = 200) -> dict[str, Any]:
        status, _, payload = self.client.json_request(method, path, body=body, require_auth=True)
        if status == 403:
            self.skipTest(f"provided acceptance key lacks scope for {method} {path}")
        self.assertEqual(status, expected, payload)
        self.assertIsInstance(payload, dict)
        return payload

    def admin_runtimes(self) -> dict[str, Any]:
        return self.json_request("GET", "/admin/runtimes")

    def localai_runtime_state(self) -> dict[str, Any]:
        runtimes = self.admin_runtimes()
        readiness = runtimes.get("readiness")
        if self.require_production:
            self.assertEqual(runtimes.get("runtime_deployment_mode"), "production", runtimes)
            self.assertIsInstance(readiness, dict)
            self.assertEqual(readiness.get("status"), "ok", readiness)
        states = runtimes.get("runtime_states")
        self.assertIsInstance(states, dict)
        localai = states.get("localai")
        self.assertIsInstance(localai, dict)
        return localai

    def active_gpu_runtime_names(self) -> list[str]:
        runtimes = self.admin_runtimes()
        states = runtimes.get("runtime_states")
        self.assertIsInstance(states, dict)
        active = []
        for runtime in sorted(GPU_RUNTIMES):
            state = states.get(runtime)
            if isinstance(state, dict) and (state.get("active_model") or state.get("resolved_model_version")):
                active.append(runtime)
        self.evidence.append({"label": "active-gpu-runtime-states", "active_gpu_runtimes": active, "runtime_states": states})
        return active

    def parse_sse_data(self, raw: bytes) -> list[Any]:
        events: list[Any] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            data = line.split(":", 1)[1].strip()
            if not data or data == "[DONE]":
                continue
            try:
                events.append(json.loads(data))
            except json.JSONDecodeError:
                events.append(data)
        return events

    def stream_chat(self) -> list[Any]:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": os.getenv("B1_LOCALAI_ACCEPTANCE_PROMPT", "Reply with the word ready.")}],
            "max_tokens": int(os.getenv("B1_LOCALAI_ACCEPTANCE_MAX_TOKENS", "16")),
            "temperature": 0,
            "stream": True,
            "runtime_policy": "non_comfy_only",
        }
        status, headers, raw = self.client.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                "Accept": "text/event-stream",
                "Idempotency-Key": f"localai-stream-{uuid.uuid4().hex}",
            },
            require_auth=True,
        )
        if status == 403:
            self.skipTest("provided acceptance key lacks chat/runtimes scope")
        self.assertEqual(status, 200, raw[:1000])
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        self.assertIn("text/event-stream", content_type.lower())
        self.assertIn(b"data:", raw)
        self.assertIn(b"[DONE]", raw)
        events = self.parse_sse_data(raw)
        self.assertGreater(len(events), 0, raw[:1000])
        errors = [event for event in events if isinstance(event, dict) and event.get("error")]
        self.assertEqual(errors, [])
        self.evidence.append(
            {
                "label": "localai-stream-chat",
                "model": self.model,
                "event_count": len(events),
                "content_type": content_type,
                "bytes": len(raw),
            }
        )
        self.record_check("streaming_chat_completed", model=self.model, event_count=len(events), bytes=len(raw))
        return events

    def verify_single_localai_backend(self) -> None:
        state = self.localai_runtime_state()
        active = self.active_gpu_runtime_names()
        self.assertEqual(active, ["localai"], f"expected only LocalAI active after streamed chat, got {active}")
        self.assertTrue(state.get("active_model") or state.get("resolved_model_version"), state)
        self.record_check(
            "single_backend_enforced",
            active_gpu_runtimes=active,
            stage=state.get("stage"),
            status=state.get("status"),
            model_alias=state.get("model_alias"),
            resolved_model_version=state.get("resolved_model_version"),
        )

    def unload_localai_and_verify_state(self) -> None:
        result = self.json_request(
            "POST",
            "/admin/runtimes/localai/unload",
            body={"reason": "LocalAI runtime acceptance unload verification", "timeout_seconds": self.unload_timeout_seconds},
        )
        agent = result.get("runtime_agent")
        self.assertIsInstance(agent, dict)
        self.assertEqual(agent.get("status"), "ok", result)
        deadline = time.monotonic() + self.unload_settle_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            state = self.localai_runtime_state()
            last = state
            if not state.get("active_model") and not state.get("resolved_model_version") and state.get("stage") == "idle_unloaded":
                self.evidence.append({"label": "localai-unload", "runtime_agent": agent, "runtime_state": state})
                self.record_check(
                    "graceful_unload_verified",
                    runtime_agent_status=agent.get("status"),
                    strategy=agent.get("strategy"),
                    state_status=state.get("status"),
                    state_stage=state.get("stage"),
                )
                return
            time.sleep(1)
        raise AssertionError(f"LocalAI unload did not clear runtime state before timeout; last={last}")

    def test_streaming_chat_single_backend_and_unload(self) -> None:
        self.stream_chat()
        self.verify_single_localai_backend()
        self.unload_localai_and_verify_state()


if __name__ == "__main__":
    unittest.main()
