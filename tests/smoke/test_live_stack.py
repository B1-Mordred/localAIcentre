from __future__ import annotations

import json
import os
import ssl
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


TERMINAL_STATES = {"completed", "cancelled", "failed", "expired", "recovery_required"}
ALLOW_INSECURE_HTTP_ENV = "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"
SMOKE_EVIDENCE_FORMAT = "b1-ai-hub-live-smoke/v1"
SMOKE_REQUIRED_CHECKS = (
    "healthz_ok",
    "models_listed",
    "tts_media_job_completed",
    "job_events_streamed",
    "artifact_downloaded",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class LiveApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        host_header: str = "",
        timeout_seconds: float = 10.0,
        tls_verify: bool = True,
        ca_file: str = "",
        allow_insecure_http: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.host_header = host_header
        self.timeout_seconds = timeout_seconds
        self.allow_insecure_http = env_flag(ALLOW_INSECURE_HTTP_ENV) if allow_insecure_http is None else allow_insecure_http
        self.context = self.ssl_context(tls_verify, ca_file)

    def ssl_context(self, tls_verify: bool, ca_file: str) -> ssl.SSLContext | None:
        if not self.base_url.lower().startswith("https://"):
            return None
        if not tls_verify:
            return ssl._create_unverified_context()
        if ca_file:
            return ssl.create_default_context(cafile=ca_file)
        return ssl.create_default_context()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        require_auth: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        if self.host_header:
            request_headers["Host"] = self.host_header
        if self.api_key:
            request_headers["Authorization"] = f"Bearer {self.api_key}"
        elif require_auth:
            raise unittest.SkipTest("set B1_AI_HUB_API_KEY or B1_SMOKE_API_KEY for authenticated smoke checks")

        data: bytes | None
        if isinstance(body, dict):
            data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            data = body

        req = Request(urljoin(self.base_url, path.lstrip("/")), data=data, headers=request_headers, method=method)
        self.enforce_token_transport_security(req.full_url)
        try:
            with urlopen(req, timeout=self.timeout_seconds, context=self.context) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except URLError as exc:
            raise AssertionError(f"{method} {path} failed: {exc.reason}") from exc

    def enforce_token_transport_security(self, url: str) -> None:
        if not self.api_key:
            return
        parsed = urlsplit(url)
        if parsed.scheme == "https":
            return
        if parsed.scheme == "http" and self.allow_insecure_http:
            return
        raise RuntimeError(
            "refusing to send a B1 acceptance API key over plain HTTP; use HTTPS "
            f"or set {ALLOW_INSECURE_HTTP_ENV}=true only for an isolated development harness"
        )

    def json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        require_auth: bool = False,
    ) -> tuple[int, dict[str, str], Any]:
        status, response_headers, raw = self.request(method, path, body=body, headers=headers, require_auth=require_auth)
        if not raw:
            return status, response_headers, None
        try:
            return status, response_headers, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{method} {path} returned non-JSON body: {raw[:200]!r}") from exc


@unittest.skipUnless(os.getenv("B1_SMOKE_LIVE_TEST") == "1", "set B1_SMOKE_LIVE_TEST=1 to run live stack smoke tests")
class LiveStackSmokeTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        tls_verify = os.getenv("B1_SMOKE_TLS_VERIFY", "1").strip().lower() not in {"0", "false", "no"}
        cls.client = LiveApiClient(
            os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=os.getenv("B1_SMOKE_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or "",
            host_header=os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "10")),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.job_timeout_seconds = float(os.getenv("B1_SMOKE_JOB_TIMEOUT_SECONDS", "120"))

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_SMOKE_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in SMOKE_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": SMOKE_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.client.base_url,
                    "status": status,
                    "required_checks": list(SMOKE_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
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

    def assert_json_status(self, status: int, payload: Any, expected: int = 200) -> None:
        self.assertEqual(status, expected, payload)
        self.assertIsInstance(payload, dict)

    def test_healthz(self) -> None:
        status, _, payload = self.client.json_request("GET", "/healthz")
        self.assert_json_status(status, payload)
        self.assertEqual(payload.get("status"), "ok")
        self.samples.append({"label": "healthz", "status": payload.get("status")})
        self.record_check("healthz_ok")

    def test_models_endpoint_requires_real_auth_path(self) -> None:
        status, _, payload = self.client.json_request("GET", "/v1/models", require_auth=True)
        self.assert_json_status(status, payload)
        self.assertEqual(payload.get("object"), "list")
        self.assertIsInstance(payload.get("data"), list)
        model_count = len(payload.get("data") or [])
        self.samples.append({"label": "models", "model_count": model_count})
        self.record_check("models_listed", model_count=model_count)

    def test_tts_media_job_reaches_terminal_state_and_serves_artifact(self) -> None:
        model = os.getenv("B1_SMOKE_TTS_MODEL", "tts-fast")
        body = {
            "modality": "tts",
            "operation": "speech",
            "model": model,
            "runtime_policy": os.getenv("B1_SMOKE_TTS_RUNTIME_POLICY", "non_comfy_only"),
            "input": {
                "text": "B1 AI Hub live smoke test.",
                "voice": os.getenv("B1_SMOKE_TTS_VOICE", "default"),
            },
        }
        status, _, job = self.client.json_request(
            "POST",
            "/v1/media/jobs",
            body=body,
            headers={"Idempotency-Key": f"smoke-tts-{uuid.uuid4().hex}"},
            require_auth=True,
        )
        self.assert_json_status(status, job)
        job_id = job["id"]
        final_job = self.wait_for_terminal_job(job_id)
        self.assertEqual(final_job.get("state"), "completed", final_job)
        self.samples.append({"label": "tts-job", "job_id": job_id, "state": final_job.get("state"), "model": model})
        self.record_check("tts_media_job_completed", job_id=job_id, model=model)

        status, _, events = self.client.request("GET", f"/v1/media/jobs/{job_id}/events", require_auth=True)
        self.assertEqual(status, 200, events[:500])
        self.assertIn(b"event: job", events)
        self.samples.append({"label": "job-events", "job_id": job_id, "bytes": len(events)})
        self.record_check("job_events_streamed", job_id=job_id, bytes=len(events))

        status, _, artifact_payload = self.client.json_request("GET", f"/v1/media/jobs/{job_id}/artifacts", require_auth=True)
        self.assert_json_status(status, artifact_payload)
        artifacts = artifact_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0, artifact_payload)
        artifact_url = artifacts[0].get("url")
        self.assertIsInstance(artifact_url, str)

        status, headers, content = self.client.request("GET", artifact_url, headers={"Accept": "*/*"}, require_auth=True)
        self.assertEqual(status, 200, content[:200])
        self.assertGreater(len(content), 0)
        self.assertIn("content-length", {key.lower() for key in headers})
        self.samples.append({"label": "artifact-download", "job_id": job_id, "bytes": len(content)})
        self.record_check("artifact_downloaded", job_id=job_id, bytes=len(content))

    def test_admin_self_test_when_key_has_scope(self) -> None:
        api_key = os.getenv("B1_SMOKE_ADMIN_API_KEY") or self.client.api_key
        if not api_key:
            self.skipTest("set B1_SMOKE_ADMIN_API_KEY to exercise /admin/self-test")
        admin_client = LiveApiClient(
            self.client.base_url,
            api_key=api_key,
            host_header=self.client.host_header,
            timeout_seconds=self.client.timeout_seconds,
            tls_verify=os.getenv("B1_SMOKE_TLS_VERIFY", "1").strip().lower() not in {"0", "false", "no"},
            ca_file=os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        status, _, payload = admin_client.json_request("GET", "/admin/self-test", require_auth=True)
        if status == 403:
            self.skipTest("provided smoke key does not have admin self-test scope")
        self.assert_json_status(status, payload)
        self.assertIn(payload.get("status"), {"ok", "degraded"})
        self.assertIsInstance(payload.get("checks"), list)

    def wait_for_terminal_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.job_timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status, _, payload = self.client.json_request("GET", f"/v1/media/jobs/{job_id}", require_auth=True)
            self.assert_json_status(status, payload)
            last = payload
            if payload.get("state") in TERMINAL_STATES:
                return payload
            time.sleep(1)
        raise AssertionError(f"job {job_id} did not reach a terminal state before timeout; last={last}")


if __name__ == "__main__":
    unittest.main()
