from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.support.evidence import write_private_json  # noqa: E402


TERMINAL_STATES = {"completed", "cancelled", "failed", "expired", "recovery_required"}
ALLOW_INSECURE_HTTP_ENV = "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"
SMOKE_EVIDENCE_FORMAT = "b1-ai-hub-live-smoke/v1"
SMOKE_REQUIRED_CHECKS = (
    "healthz_ok",
    "open_webui_health_ok",
    "models_listed",
    "tts_media_job_completed",
    "tts_media_job_resolved_model_recorded",
    "tts_media_job_not_placeholder",
    "job_events_streamed",
    "job_events_terminal_state_observed",
    "artifact_downloaded",
    "artifact_metadata_verified",
)
MODEL_MEASUREMENT_RUN_FIELDS = (
    "id",
    "type",
    "status",
    "runtime",
    "model_alias",
    "resolved_model_version",
    "started_at",
    "completed_at",
    "duration_ms",
    "load_time_ms",
    "run_time_ms",
    "peak_vram_mib",
    "peak_ram_mib",
    "resource_estimate",
)
MEDIA_JOB_LINK_NAMES = ("self", "events", "artifacts", "cancel")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def media_job_link(job: dict[str, Any], link_name: str, fallback_suffix: str = "") -> str:
    links = job.get("links")
    if isinstance(links, dict):
        candidate = links.get(link_name)
        if isinstance(candidate, str) and candidate.startswith("/v1/media/jobs/"):
            return candidate
    job_id = quote(str(job.get("id") or ""), safe="")
    if not job_id:
        raise AssertionError("media job response did not include an id")
    return f"/v1/media/jobs/{job_id}{fallback_suffix}"


def assert_media_job_links(testcase: unittest.TestCase, job: dict[str, Any]) -> None:
    links = job.get("links")
    testcase.assertIsInstance(links, dict, job)
    for name in MEDIA_JOB_LINK_NAMES:
        value = links.get(name) if isinstance(links, dict) else None
        testcase.assertIsInstance(value, str, job)
        testcase.assertTrue(value.startswith("/v1/media/jobs/"), value)


def response_header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def downloaded_artifact_proof(artifact: dict[str, Any], headers: dict[str, str], content: bytes, *, index: int) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "artifact_index": index,
        "artifact_url": str(artifact.get("url") or ""),
        "artifact_id": str(artifact.get("id") or ""),
        "artifact_kind": str(artifact.get("kind") or ""),
        "artifact_mime_type": str(artifact.get("mime_type") or ""),
        "artifact_bytes": artifact.get("bytes"),
        "artifact_sha256": str(artifact.get("sha256") or ""),
        "download_bytes": len(content),
        "download_sha256": digest,
        "download_content_type": response_header(headers, "content-type"),
        "download_content_length": response_header(headers, "content-length"),
        "download_etag": response_header(headers, "etag"),
        "download_accept_ranges": response_header(headers, "accept-ranges"),
    }


def artifact_collection_proof(job_id: str, artifact_proofs: list[dict[str, Any]]) -> dict[str, Any]:
    first_proof = artifact_proofs[0]
    return {
        "job_id": job_id,
        "artifact_count": len(artifact_proofs),
        "verified_artifact_count": len(artifact_proofs),
        "total_downloaded_bytes": sum(int(proof["download_bytes"]) for proof in artifact_proofs),
        "artifact_proofs": artifact_proofs,
        "artifact_url": first_proof["artifact_url"],
        "artifact_id": first_proof["artifact_id"],
        "artifact_kind": first_proof["artifact_kind"],
        "artifact_mime_type": first_proof["artifact_mime_type"],
        "artifact_bytes": first_proof["artifact_bytes"],
        "artifact_sha256": first_proof["artifact_sha256"],
        "first_artifact_url": first_proof["artifact_url"],
        "first_artifact_bytes": first_proof["download_bytes"],
        "first_artifact_sha256": first_proof["download_sha256"],
        "first_artifact_mime_type": first_proof["artifact_mime_type"],
        "download_content_type": first_proof["download_content_type"],
        "download_content_length": first_proof["download_content_length"],
        "download_etag": first_proof["download_etag"],
        "download_accept_ranges": first_proof["download_accept_ranges"],
    }


def sse_payloads(raw: bytes) -> list[dict[str, Any] | str]:
    text = raw.decode("utf-8", errors="replace")
    payloads: list[dict[str, Any] | str] = []
    for event_block in text.split("\n\n"):
        data_lines = []
        for line in event_block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            payloads.append(data)
        else:
            payloads.append(parsed if isinstance(parsed, dict) else data)
    return payloads


def terminal_job_event_payload(payloads: list[dict[str, Any] | str], job_id: str, state: str) -> dict[str, Any] | None:
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("id") == job_id and payload.get("state") == state:
            return payload
    return None


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


def compact_measurement_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run[key] for key in MODEL_MEASUREMENT_RUN_FIELDS if key in run}


def model_record_ref(record: dict[str, Any]) -> str:
    return f"{record.get('id')}@{record.get('version')}"


def latest_ok_measurement_run(manifest: dict[str, Any], resolved_model_version: str, alias: str) -> tuple[dict[str, Any] | None, int]:
    measurements = manifest.get("measurements") if isinstance(manifest.get("measurements"), dict) else {}
    runs = measurements.get("runs") if isinstance(measurements.get("runs"), list) else []
    ok_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("status") == "ok"
        and run.get("resolved_model_version") == resolved_model_version
        and (run.get("model_alias") == alias or alias in manifest.get("aliases", []))
    ]
    return (compact_measurement_run(ok_runs[-1]) if ok_runs else None, len(ok_runs))


def measured_model_alias(client: LiveApiClient, alias: str, *, expected_runtime: str | None = None) -> dict[str, Any]:
    status, _, payload = client.json_request("GET", "/admin/models", require_auth=True)
    if status == 403:
        raise unittest.SkipTest("provided acceptance key lacks admin model-read scope required for measurement evidence")
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError(f"GET /admin/models failed while resolving measured model {alias!r}: status={status} payload={payload!r}")
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
    alias_record = next((item for item in aliases if isinstance(item, dict) and item.get("id") == alias), None)
    if not isinstance(alias_record, dict):
        raise AssertionError(f"alias {alias!r} is not present in /admin/models")
    if alias_record.get("status") != "installed":
        raise AssertionError(f"alias {alias!r} is not backed by an installed model manifest: {alias_record}")
    resolved = alias_record.get("resolved_model")
    if not isinstance(resolved, dict) or not resolved.get("id") or not resolved.get("version"):
        raise AssertionError(f"alias {alias!r} lacks resolved immutable model evidence: {alias_record}")
    resolved_model_version = f"{resolved['id']}@{resolved['version']}"
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    model_record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("id") == resolved["id"]
            and item.get("version") == resolved["version"]
        ),
        None,
    )
    if not isinstance(model_record, dict):
        raise AssertionError(f"resolved model {resolved_model_version!r} for alias {alias!r} is absent from installed model records")
    manifest = model_record.get("manifest") if isinstance(model_record.get("manifest"), dict) else {}
    measurements = manifest.get("measurements") if isinstance(manifest.get("measurements"), dict) else {}
    latest_ok_run, ok_run_count = latest_ok_measurement_run(manifest, resolved_model_version, alias)
    runtime = str((latest_ok_run or {}).get("runtime") or alias_record.get("preferred_runtime") or model_record.get("preferred_runtime") or "")
    if expected_runtime and runtime != expected_runtime:
        raise AssertionError(
            f"alias {alias!r} latest measurement runtime {runtime!r} does not match expected runtime {expected_runtime!r}"
        )
    summary = {
        "alias": alias,
        "status": alias_record.get("status"),
        "modality": alias_record.get("modality"),
        "preferred_runtime": alias_record.get("preferred_runtime"),
        "runtime": runtime,
        "runtimes": alias_record.get("runtimes") or [],
        "resource_label": alias_record.get("resource_label") or model_record.get("resource_label"),
        "resolved_model_version": resolved_model_version,
        "model_id": resolved.get("id"),
        "model_version": resolved.get("version"),
        "display_name": resolved.get("display_name") or model_record.get("display_name"),
        "measurement_available": latest_ok_run is not None,
        "ok_run_count": ok_run_count,
        "measurements_updated_at": measurements.get("updated_at") if isinstance(measurements, dict) else "",
        "latest_resource_estimate": measurements.get("latest_resource_estimate") if isinstance(measurements, dict) else {},
        "latest_ok_run": latest_ok_run or {},
    }
    if latest_ok_run is None:
        raise AssertionError(
            f"alias {alias!r} resolved to {resolved_model_version}, but no persisted ok model smoke measurement exists; "
            "run the Control Center model smoke test after installing the model before creating handoff evidence"
        )
    return summary


@unittest.skipUnless(os.getenv("B1_SMOKE_LIVE_TEST") == "1", "set B1_SMOKE_LIVE_TEST=1 to run live stack smoke tests")
class LiveStackSmokeTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        tls_verify = os.getenv("B1_SMOKE_TLS_VERIFY", "1").strip().lower() not in {"0", "false", "no"}
        ca_file = os.getenv("B1_SMOKE_CA_FILE", "")
        timeout_seconds = float(os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "10"))
        cls.client = LiveApiClient(
            os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=os.getenv("B1_SMOKE_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or "",
            host_header=os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=timeout_seconds,
            tls_verify=tls_verify,
            ca_file=ca_file,
        )
        cls.open_webui_client = LiveApiClient(
            os.getenv("B1_SMOKE_OPEN_WEBUI_BASE") or "https://ai.b1.germering",
            host_header=os.getenv("B1_SMOKE_OPEN_WEBUI_HOST_HEADER", ""),
            timeout_seconds=timeout_seconds,
            tls_verify=tls_verify,
            ca_file=ca_file,
        )
        cls.job_timeout_seconds = float(os.getenv("B1_SMOKE_JOB_TIMEOUT_SECONDS", "120"))
        cls.allow_placeholder = env_flag("B1_SMOKE_ALLOW_PLACEHOLDER", False)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_SMOKE_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in SMOKE_REQUIRED_CHECKS) else "incomplete"
        write_private_json(
            path,
            {
                "format": SMOKE_EVIDENCE_FORMAT,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "base_url": cls.client.base_url,
                "open_webui_base_url": cls.open_webui_client.base_url,
                "status": status,
                "required_checks": list(SMOKE_REQUIRED_CHECKS),
                "checks": cls.checks,
                "samples": cls.samples,
            },
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

    def test_open_webui_health_through_gateway(self) -> None:
        status, headers, payload = self.open_webui_client.json_request("GET", "/health")
        self.assert_json_status(status, payload)
        self.assertIs(payload.get("status"), True)
        permissions_policy = response_header(headers, "permissions-policy")
        strict_transport_security = response_header(headers, "strict-transport-security")
        x_content_type_options = response_header(headers, "x-content-type-options")
        self.samples.append(
            {
                "label": "open-webui-health",
                "base_url": self.open_webui_client.base_url,
                "status_code": status,
                "response_status": payload.get("status"),
            }
        )
        self.record_check(
            "open_webui_health_ok",
            base_url=self.open_webui_client.base_url,
            host_header=self.open_webui_client.host_header,
            status_code=status,
            response_status=payload.get("status"),
            permissions_policy=permissions_policy,
            strict_transport_security=strict_transport_security,
            x_content_type_options=x_content_type_options,
        )

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
        self.assert_json_status(status, job, expected=202)
        assert_media_job_links(self, job)
        job_id = job["id"]
        final_job = self.wait_for_terminal_job(job)
        assert_media_job_links(self, final_job)
        self.assertEqual(final_job.get("state"), "completed", final_job)
        resolved_model_version = final_job.get("resolved_model_version")
        runtime = final_job.get("runtime")
        self.assertIsInstance(resolved_model_version, str, final_job)
        self.assertRegex(resolved_model_version, r"^[^@]+@[^@]+$", final_job)
        self.assertIsInstance(runtime, str, final_job)
        self.assertTrue(runtime, final_job)
        self.samples.append(
            {
                "label": "tts-job",
                "job_id": job_id,
                "state": final_job.get("state"),
                "model": model,
                "runtime": runtime,
                "resolved_model_version": resolved_model_version,
                "link_keys": list(MEDIA_JOB_LINK_NAMES),
            }
        )
        self.record_check("tts_media_job_completed", job_id=job_id, model=model)
        self.record_check(
            "tts_media_job_resolved_model_recorded",
            job_id=job_id,
            model=model,
            runtime=runtime,
            resolved_model_version=resolved_model_version,
        )

        status, _, events = self.client.request("GET", media_job_link(final_job, "events", "/events"), require_auth=True)
        self.assertEqual(status, 200, events[:500])
        self.assertIn(b"event: job", events)
        parsed_events = sse_payloads(events)
        terminal_event = terminal_job_event_payload(parsed_events, job_id, "completed")
        self.assertIsNotNone(terminal_event, events[:1000])
        self.samples.append({"label": "job-events", "job_id": job_id, "bytes": len(events), "event_count": len(parsed_events)})
        self.record_check("job_events_streamed", job_id=job_id, bytes=len(events), link=media_job_link(final_job, "events", "/events"))
        self.record_check(
            "job_events_terminal_state_observed",
            job_id=job_id,
            state="completed",
            event_count=len(parsed_events),
        )

        status, _, artifact_payload = self.client.json_request("GET", media_job_link(final_job, "artifacts", "/artifacts"), require_auth=True)
        self.assert_json_status(status, artifact_payload)
        artifacts = artifact_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0, artifact_payload)
        placeholder_proofs = []
        for index, artifact in enumerate(artifacts):
            self.assertIsInstance(artifact, dict)
            artifact_placeholder = artifact.get("b1_placeholder")
            cpu_audio_engine = artifact.get("b1_cpu_audio_engine")
            reasons = []
            if artifact_placeholder is True:
                reasons.append("explicit_placeholder_marker")
            if runtime == "audio-cpu" and artifact_placeholder is not False:
                reasons.append("audio_cpu_non_placeholder_marker_missing")
            if runtime == "audio-cpu" and cpu_audio_engine == "scaffold":
                reasons.append("scaffold_cpu_audio_engine")
            placeholder_proofs.append(
                {
                    "artifact_index": index,
                    "placeholder": artifact_placeholder,
                    "cpu_audio_engine": cpu_audio_engine,
                    "placeholder_failure": bool(reasons),
                    "reasons": reasons,
                }
            )
        placeholder_failures = [proof for proof in placeholder_proofs if proof["placeholder_failure"]]
        first_placeholder = placeholder_proofs[0]
        if placeholder_failures:
            self.record_check(
                "tts_media_job_not_placeholder",
                "incomplete",
                job_id=job_id,
                model=model,
                runtime=runtime,
                resolved_model_version=resolved_model_version,
                artifact_count=len(artifacts),
                artifact_placeholders=placeholder_proofs,
                placeholder_failure_count=len(placeholder_failures),
                placeholder=first_placeholder.get("placeholder"),
                placeholder_allowed=self.allow_placeholder,
                cpu_audio_engine=first_placeholder.get("cpu_audio_engine"),
            )
            if not self.allow_placeholder:
                raise AssertionError(
                    "live smoke TTS returned placeholder or unproven audio-cpu output; install a real TTS model/runtime before handoff "
                    "or set B1_SMOKE_ALLOW_PLACEHOLDER=1 only for a labelled development dry run"
                )
        else:
            self.record_check(
                "tts_media_job_not_placeholder",
                job_id=job_id,
                model=model,
                runtime=runtime,
                resolved_model_version=resolved_model_version,
                artifact_count=len(artifacts),
                artifact_placeholders=placeholder_proofs,
                placeholder_failure_count=0,
                placeholder=False,
                cpu_audio_engine=first_placeholder.get("cpu_audio_engine"),
            )

        artifact_proofs = [self.verify_artifact_download(artifact, index) for index, artifact in enumerate(artifacts)]
        artifact_collection = artifact_collection_proof(job_id, artifact_proofs)
        first_proof = artifact_proofs[0]
        self.samples.append(
            {
                "label": "artifact-download",
                "job_id": job_id,
                "artifact_count": len(artifact_proofs),
                "verified_artifact_count": len(artifact_proofs),
                "total_downloaded_bytes": artifact_collection["total_downloaded_bytes"],
                "first_artifact_bytes": first_proof["download_bytes"],
                "first_artifact_mime_type": first_proof["artifact_mime_type"],
                "first_artifact_sha256": first_proof["download_sha256"],
            }
        )
        self.record_check("artifact_downloaded", **artifact_collection)
        self.record_check(
            "artifact_metadata_verified",
            **artifact_collection,
        )

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

    def wait_for_terminal_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        deadline = time.monotonic() + self.job_timeout_seconds
        last: dict[str, Any] | None = job
        while time.monotonic() < deadline:
            status, _, payload = self.client.json_request("GET", media_job_link(last or job, "self"), require_auth=True)
            self.assert_json_status(status, payload)
            last = payload
            if payload.get("state") in TERMINAL_STATES:
                return payload
            time.sleep(1)
        raise AssertionError(f"job {job_id} did not reach a terminal state before timeout; last={last}")

    def verify_artifact_download(self, artifact: dict[str, Any], index: int) -> dict[str, Any]:
        artifact_url = artifact.get("url")
        self.assertIsInstance(artifact_url, str)
        self.assertTrue(artifact_url.startswith("/artifacts/"), artifact)
        artifact_mime_type = artifact.get("mime_type")
        artifact_bytes = artifact.get("bytes")
        artifact_sha256 = artifact.get("sha256")
        self.assertIsInstance(artifact_mime_type, str, artifact)
        self.assertTrue(artifact_mime_type, artifact)
        self.assertIsInstance(artifact_bytes, int, artifact)
        self.assertGreater(artifact_bytes, 0, artifact)
        self.assertIsInstance(artifact_sha256, str, artifact)
        self.assertRegex(artifact_sha256, SHA256_RE, artifact)
        status, headers, content = self.client.request("GET", artifact_url, headers={"Accept": "*/*"}, require_auth=True)
        self.assertEqual(status, 200, content[:200])
        self.assertGreater(len(content), 0)
        content_length = response_header(headers, "content-length")
        self.assertEqual(content_length, str(len(content)), headers)
        self.assertEqual(artifact_bytes, len(content), artifact)
        downloaded_sha256 = hashlib.sha256(content).hexdigest()
        self.assertEqual(artifact_sha256, downloaded_sha256, artifact)
        content_type = response_header(headers, "content-type")
        etag = response_header(headers, "etag")
        accept_ranges = response_header(headers, "accept-ranges")
        self.assertTrue(content_type, headers)
        self.assertTrue(etag, headers)
        self.assertEqual(accept_ranges.lower(), "bytes", headers)
        return downloaded_artifact_proof(artifact, headers, content, index=index)


if __name__ == "__main__":
    unittest.main()
