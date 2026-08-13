from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import ssl
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.support.evidence import write_private_json  # noqa: E402


VOICEBOX_EVIDENCE_FORMAT = "b1-ai-hub-voicebox-remote-compatibility/v1"
VOICEBOX_REQUIRED_CHECKS = (
    "proxy_build_info_validated",
    "native_http_proxy_accessible",
    "profile_lifecycle_validated",
    "sample_artifact_protected",
    "profile_export_validated",
    "profile_delete_audited",
    "speech_or_limitation_recorded",
    "websocket_or_limitation_recorded",
)

ALLOW_INSECURE_HTTP_ENV = "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def bounded_text(value: str, limit: int = 1000) -> str:
    return value.strip()[:limit]


def response_content_type(headers: dict[str, str]) -> str:
    return (headers.get("Content-Type") or headers.get("content-type") or "").split(";", 1)[0]


def acceptance_wav_bytes() -> bytes:
    inline = os.getenv("B1_VOICEBOX_SAMPLE_WAV_BASE64", "").strip()
    if inline:
        return base64.b64decode(inline, validate=True)
    # 100 ms of mono 16-bit PCM silence at 16 kHz.
    sample_rate = 16000
    sample_count = sample_rate // 10
    data_size = sample_count * 2
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 2).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return header + (b"\x00" * data_size)


@unittest.skipUnless(os.getenv("B1_VOICEBOX_LIVE_TEST") == "1", "set B1_VOICEBOX_LIVE_TEST=1 to run live Voicebox compatibility tests")
class VoiceboxRemoteCompatibilityTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    build_info: dict[str, Any] = {}
    last_http: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.build_info = {}
        cls.last_http = {}
        cls.voice_base = os.getenv("B1_VOICEBOX_BASE", "https://voice.ai.b1.germering").rstrip("/")
        cls.api_base = os.getenv("B1_VOICEBOX_API_BASE", "https://api.ai.b1.germering").rstrip("/")
        cls.api_key = os.getenv("B1_VOICEBOX_API_KEY", "").strip()
        cls.native_api_key = os.getenv("B1_VOICEBOX_NATIVE_API_KEY", cls.api_key).strip()
        cls.voice_host_header = os.getenv("B1_VOICEBOX_HOST_HEADER", "").strip()
        cls.api_host_header = os.getenv("B1_VOICEBOX_API_HOST_HEADER", "").strip()
        cls.timeout_seconds = float(os.getenv("B1_VOICEBOX_TIMEOUT_SECONDS", "180"))
        cls.expected_proxy_version = os.getenv("B1_VOICEBOX_EXPECTED_PROXY_VERSION", "b1-voicebox-proxy/v0.5.0-b1").strip()
        cls.expected_upstream_version = os.getenv("B1_VOICEBOX_EXPECTED_VERSION", "v0.5.0").strip()
        cls.expected_upstream_commit = os.getenv(
            "B1_VOICEBOX_EXPECTED_COMMIT",
            "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
        ).strip().lower()
        cls.expected_source_archive_sha256 = os.getenv(
            "B1_VOICEBOX_EXPECTED_SOURCE_ARCHIVE_SHA256",
            "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
        ).strip().lower()
        cls.speech_limitation = bounded_text(os.getenv("B1_VOICEBOX_SPEECH_LIMITATION", ""))
        cls.websocket_limitation = bounded_text(os.getenv("B1_VOICEBOX_WEBSOCKET_LIMITATION", ""))
        if not cls.api_key:
            raise unittest.SkipTest("set B1_VOICEBOX_API_KEY to an admin/operator API key with runtimes and inference scopes")

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_VOICEBOX_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in VOICEBOX_REQUIRED_CHECKS) else "incomplete"
        write_private_json(
            path,
            {
                "format": VOICEBOX_EVIDENCE_FORMAT,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "base_url": cls.voice_base,
                "api_base_url": cls.api_base,
                "status": status,
                "required_checks": list(VOICEBOX_REQUIRED_CHECKS),
                "checks": cls.checks,
                "samples": cls.samples,
            },
        )

    @classmethod
    def ssl_context(cls) -> ssl.SSLContext | None:
        if not (cls.voice_base.startswith("https://") or cls.api_base.startswith("https://")):
            return None
        if not env_flag("B1_VOICEBOX_TLS_VERIFY", True):
            return ssl._create_unverified_context()
        ca_file = os.getenv("B1_VOICEBOX_CA_FILE", "").strip()
        if ca_file:
            return ssl.create_default_context(cafile=ca_file)
        return ssl.create_default_context()

    @classmethod
    def build_url(cls, base_url: str, path: str) -> str:
        return urllib.parse.urljoin(base_url + "/", path.lstrip("/"))

    @classmethod
    def enforce_token_transport_security(cls, url: str, *, token: str) -> None:
        if not token:
            return
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"https", "wss"}:
            return
        if parsed.scheme in {"http", "ws"} and env_flag(ALLOW_INSECURE_HTTP_ENV):
            return
        raise RuntimeError(
            "refusing to send a B1 Voicebox API key over plain HTTP/WebSocket; use HTTPS/WSS "
            f"or set {ALLOW_INSECURE_HTTP_ENV}=true only for an isolated development harness"
        )

    @classmethod
    def headers(cls, *, native: bool = False, accept: str = "application/json") -> dict[str, str]:
        token = cls.native_api_key if native else cls.api_key
        headers = {"Accept": accept}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        host = cls.voice_host_header if native else cls.api_host_header
        if host:
            headers["Host"] = host
        return headers

    @classmethod
    def request_raw(
        cls,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        native: bool = False,
        accept: str = "application/json",
        allow_http_error: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None
        headers = cls.headers(native=native, accept=accept)
        if payload is not None:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            headers["Content-Type"] = "application/json"
        token = cls.native_api_key if native else cls.api_key
        url = cls.build_url(base_url, path)
        cls.enforce_token_transport_security(url, token=token)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=cls.timeout_seconds, context=cls.ssl_context()) as response:
                status = int(getattr(response, "status", response.getcode()))
                response_headers = dict(response.headers.items())
                body = response.read()
                cls.last_http = {
                    "method": method,
                    "path": path,
                    "url": url,
                    "http_status": status,
                    "content_type": response_content_type(response_headers),
                    "byte_count": len(body),
                }
                return status, response_headers, body
        except urllib.error.HTTPError as exc:
            body = exc.read()
            headers = dict(exc.headers.items())
            cls.last_http = {
                "method": method,
                "path": path,
                "url": url,
                "http_status": int(exc.code),
                "content_type": response_content_type(headers),
                "byte_count": len(body),
            }
            if allow_http_error:
                return int(exc.code), headers, body
            content_type = response_content_type(headers) or "<none>"
            raise AssertionError(
                f"{method} {path} failed with http_status={exc.code} content_type={content_type} "
                f"url={url}: {body[:200]!r}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cls.last_http = {
                "method": method,
                "path": path,
                "url": url,
                "http_status": None,
                "content_type": None,
                "byte_count": 0,
                "error_type": exc.__class__.__name__,
            }
            raise AssertionError(
                f"{method} {path} failed before an HTTP response; http_status=<none> "
                f"content_type=<none> url={url} error={exc.__class__.__name__}: {bounded_text(str(exc), 300)}"
            ) from exc

    @classmethod
    def request_bytes(
        cls,
        method: str,
        path: str,
        body: bytes,
        *,
        content_type: str,
        filename: str | None = None,
    ) -> dict[str, Any]:
        headers = cls.headers(accept="application/json")
        headers["Content-Type"] = content_type
        if filename:
            headers["X-B1-Filename"] = filename
        url = cls.build_url(cls.api_base, path)
        cls.enforce_token_transport_security(url, token=cls.api_key)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=cls.timeout_seconds, context=cls.ssl_context()) as response:
                status = int(getattr(response, "status", response.getcode()))
                headers = dict(response.headers.items())
                raw = response.read()
                cls.last_http = {
                    "method": method,
                    "path": path,
                    "url": url,
                    "http_status": status,
                    "content_type": response_content_type(headers),
                    "byte_count": len(raw),
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = dict(exc.headers.items())
            content_type = response_content_type(headers) or "<none>"
            cls.last_http = {
                "method": method,
                "path": path,
                "url": url,
                "http_status": int(exc.code),
                "content_type": response_content_type(headers),
                "byte_count": len(raw),
            }
            raise AssertionError(
                f"{method} {path} failed with http_status={exc.code} content_type={content_type} "
                f"url={url}: {raw[:200]!r}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cls.last_http = {
                "method": method,
                "path": path,
                "url": url,
                "http_status": None,
                "content_type": None,
                "byte_count": 0,
                "error_type": exc.__class__.__name__,
            }
            raise AssertionError(
                f"{method} {path} failed before an HTTP response; http_status=<none> "
                f"content_type=<none> url={url} error={exc.__class__.__name__}: {bounded_text(str(exc), 300)}"
            ) from exc
        if status < 200 or status >= 300:
            raise AssertionError(
                f"{method} {path} returned http_status={status} "
                f"content_type={response_content_type(headers) or '<none>'} url={url}"
            )
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AssertionError(f"{method} {path} did not return a JSON object")
        return decoded

    def last_http_fields(self, prefix: str = "") -> dict[str, Any]:
        fields = self.__class__.last_http
        key_prefix = f"{prefix}_" if prefix else ""
        return {
            f"{key_prefix}http_status": fields.get("http_status"),
            f"{key_prefix}content_type": fields.get("content_type"),
            f"{key_prefix}byte_count": fields.get("byte_count"),
            f"{key_prefix}path": fields.get("path"),
        }

    @classmethod
    def request_json(cls, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, _headers, body = cls.request_raw(cls.api_base, method, path, payload)
        if status < 200 or status >= 300:
            raise AssertionError(f"{method} {path} returned HTTP {status}")
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AssertionError(f"{method} {path} did not return a JSON object")
        return decoded

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def build_identity_fields(self) -> dict[str, str]:
        return {
            "proxy_version": str(self.build_info.get("proxy_version") or ""),
            "upstream_repository": str(self.build_info.get("upstream_repository") or ""),
            "upstream_version": str(self.build_info.get("upstream_version") or ""),
            "upstream_commit": str(self.build_info.get("upstream_commit") or ""),
            "source_archive_sha256": str(self.build_info.get("source_archive_sha256") or ""),
        }

    def test_voicebox_remote_profile_speech_and_websocket(self) -> None:
        self.verify_proxy_build_info()
        self.verify_native_http_proxy()
        self.verify_profile_lifecycle()
        self.verify_speech_or_limitation()
        self.verify_websocket_or_limitation()

    def verify_proxy_build_info(self) -> None:
        status, headers, body = self.request_raw(self.voice_base, "GET", "/b1/runtime/build-info", native=True)
        self.assertGreaterEqual(status, 200)
        self.assertLess(status, 300)
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        decoded = json.loads(body.decode("utf-8"))
        self.assertIsInstance(decoded, dict)
        self.assertEqual(decoded.get("status"), "ok")
        self.assertEqual(decoded.get("runtime"), "voicebox")
        self.assertEqual(decoded.get("action"), "build-info")
        self.assertEqual(decoded.get("proxy"), "b1-voicebox-proxy")
        self.assertEqual(decoded.get("proxy_version"), self.expected_proxy_version)
        self.assertEqual(decoded.get("upstream_repository"), "jamiepine/voicebox")
        self.assertEqual(decoded.get("upstream_version"), self.expected_upstream_version)
        self.assertEqual(decoded.get("upstream_commit"), self.expected_upstream_commit)
        self.assertEqual(decoded.get("source_archive_sha256"), self.expected_source_archive_sha256)
        self.assertIs(decoded.get("pinned"), True)
        self.__class__.build_info = decoded
        self.record_check(
            "proxy_build_info_validated",
            runtime=decoded.get("runtime"),
            action=decoded.get("action"),
            proxy=decoded.get("proxy"),
            proxy_version=decoded.get("proxy_version"),
            upstream_repository=decoded.get("upstream_repository"),
            upstream_version=decoded.get("upstream_version"),
            upstream_commit=decoded.get("upstream_commit"),
            source_archive_sha256=decoded.get("source_archive_sha256"),
            pinned=decoded.get("pinned"),
            http_status=status,
            content_type=content_type.split(";", 1)[0],
            byte_count=len(body),
        )
        self.samples.append(
            {
                "label": "voicebox-proxy-build-info",
                "proxy_version": decoded.get("proxy_version"),
                "upstream_version": decoded.get("upstream_version"),
                "upstream_commit": decoded.get("upstream_commit"),
            }
        )

    def verify_native_http_proxy(self) -> None:
        path = os.getenv("B1_VOICEBOX_NATIVE_HTTP_PATH", "/")
        status, headers, body = self.request_raw(self.voice_base, "GET", path, native=True, accept="*/*", allow_http_error=True)
        self.assertLess(status, 500, f"Voicebox native proxy returned HTTP {status}")
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        self.record_check(
            "native_http_proxy_accessible",
            path=path,
            http_status=status,
            content_type=content_type.split(";", 1)[0],
            byte_count=len(body),
            **self.build_identity_fields(),
        )
        self.samples.append({"label": "voicebox-native-http", "path": path, "http_status": status, "byte_count": len(body)})

    def verify_profile_lifecycle(self) -> None:
        display_name = f"B1 acceptance {uuid.uuid4().hex[:8]}"
        model_alias = os.getenv("B1_VOICEBOX_PROFILE_MODEL", "tts-quality")
        sample_bytes = acceptance_wav_bytes()
        sample_response = self.request_bytes(
            "POST",
            "/admin/voicebox/sample-artifacts",
            sample_bytes,
            content_type="audio/wav",
            filename="b1-voicebox-acceptance.wav",
        )
        sample_upload_http = self.last_http_fields("sample_upload")
        self.assertEqual(sample_response.get("object"), "voicebox.sample_artifact")
        sample_artifact = sample_response.get("artifact")
        self.assertIsInstance(sample_artifact, dict)
        sample_url = str(sample_artifact.get("url") or "")
        sample_id = str(sample_artifact.get("sample_id") or "")
        sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
        self.assertTrue(sample_url.startswith("/artifacts/voicebox/references/"), sample_artifact)
        self.assertTrue(sample_id.startswith("sample_"), sample_artifact)
        self.assertEqual(sample_artifact.get("mime_type"), "audio/wav")
        self.assertEqual(sample_artifact.get("sha256"), sample_sha256)
        create_payload = {
            "display_name": display_name,
            "runtime": "voicebox",
            "engine": os.getenv("B1_VOICEBOX_PROFILE_ENGINE", "voicebox"),
            "model_alias": model_alias,
            "profile_type": os.getenv("B1_VOICEBOX_PROFILE_TYPE", "reference"),
            "status": "disabled",
            "visibility_roles": ["admin", "operator"],
            "metadata": {
                "acceptance": "voicebox-remote",
                **self.build_identity_fields(),
            },
            "sample_artifacts": [sample_artifact],
        }
        created = self.request_json("POST", "/admin/voicebox/profiles", create_payload)
        create_http = self.last_http_fields("create")
        profile_id = str(created.get("id") or "")
        self.assertTrue(profile_id.startswith("vp_"), f"unexpected Voicebox profile id: {profile_id}")
        try:
            fetched = self.request_json("GET", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}")
            fetch_http = self.last_http_fields("fetch")
            exported = self.request_json("POST", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}/export")
            export_http = self.last_http_fields("export")
        finally:
            deleted = self.request_json("DELETE", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}")
            delete_http = self.last_http_fields("delete")
        self.assertEqual(fetched.get("id"), profile_id)
        fetched_samples = fetched.get("sample_artifacts")
        self.assertIsInstance(fetched_samples, list)
        self.assertEqual(len(fetched_samples), 1)
        self.assertEqual(fetched_samples[0].get("url"), sample_url)
        self.assertEqual(fetched_samples[0].get("sha256"), sample_sha256)
        self.assertNotIn("sample_artifacts", fetched.get("metadata") or {})
        self.assertEqual(exported.get("format"), "b1-ai-hub-voice-profile/v1")
        self.assertIs(exported.get("contains_sensitive_data"), True)
        self.assertIn("raw voice sample bytes are exported only by the backup/artifact workflow", str(exported.get("note") or ""))
        exported_profile = exported.get("profile")
        self.assertIsInstance(exported_profile, dict)
        exported_samples = exported_profile.get("sample_artifacts") or []
        self.assertIsInstance(exported_samples, list)
        self.assertEqual(len(exported_samples), 1)
        self.assertEqual(exported_samples[0].get("url"), sample_url)
        self.assertEqual(exported_samples[0].get("sha256"), sample_sha256)
        self.assertEqual(deleted.get("status"), "deleted")
        audit_proof = self.verify_voicebox_audit_events(profile_id, sample_id, sample_bytes=sample_bytes, sample_sha256=sample_sha256)
        self.record_check(
            "profile_lifecycle_validated",
            profile_id=profile_id,
            model_alias=model_alias,
            profile_type=create_payload["profile_type"],
            sample_artifact_count=len(fetched_samples),
            sample_artifact_url=sample_url,
            sample_artifact_sha256=sample_sha256,
            fetched_sample_artifact_count=len(fetched_samples),
            fetched_sample_artifact_url=fetched_samples[0].get("url"),
            fetched_sample_artifact_sha256=fetched_samples[0].get("sha256"),
            **sample_upload_http,
            **create_http,
            **fetch_http,
        )
        self.record_check(
            "sample_artifact_protected",
            sample_id=sample_id,
            sample_url_prefix="/artifacts/voicebox/references/",
            sample_artifact_url=sample_url,
            sample_artifact_bytes=sample_artifact.get("bytes"),
            sample_artifact_sha256=sample_artifact.get("sha256"),
            sample_artifact_mime_type=sample_artifact.get("mime_type"),
            profile_metadata_has_sample_payload=False,
            export_contains_raw_sample_bytes=False,
            **sample_upload_http,
        )
        self.record_check(
            "profile_export_validated",
            profile_id=profile_id,
            export_format=exported.get("format"),
            contains_sensitive_data=exported.get("contains_sensitive_data"),
            sample_artifact_count=len(exported_samples),
            exported_sample_artifact_url=exported_samples[0].get("url"),
            exported_sample_artifact_sha256=exported_samples[0].get("sha256"),
            **export_http,
        )
        self.record_check(
            "profile_delete_audited",
            profile_id=profile_id,
            deleted_status=deleted.get("status"),
            **delete_http,
            **audit_proof,
        )
        self.samples.append({"label": "voice-profile-lifecycle", "profile_id": profile_id, "model_alias": model_alias})
        self.samples.append(
            {
                "label": "voice-sample-artifact",
                "profile_id": profile_id,
                "sample_id": sample_id,
                "byte_count": sample_artifact.get("bytes"),
                "sha256": sample_sha256,
            }
        )

    def verify_voicebox_audit_events(self, profile_id: str, sample_id: str, *, sample_bytes: bytes, sample_sha256: str) -> dict[str, Any]:
        event_types = {"voice_profile.sample_uploaded", "voice_profile.exported", "voice_profile.deleted"}
        seen: dict[str, dict[str, Any]] = {}
        for event_type in sorted(event_types):
            response = self.request_json("GET", f"/admin/audit-log?event_type={urllib.parse.quote(event_type)}&limit=50")
            rows = response.get("data")
            self.assertIsInstance(rows, list)
            for row in rows:
                if not isinstance(row, dict) or row.get("event_type") != event_type:
                    continue
                if event_type == "voice_profile.sample_uploaded" and row.get("target_id") == sample_id:
                    seen[event_type] = row
                    break
                if event_type in {"voice_profile.exported", "voice_profile.deleted"} and row.get("target_id") == profile_id:
                    seen[event_type] = row
                    break
        missing = sorted(event_types.difference(seen))
        self.assertFalse(missing, f"missing Voicebox audit events: {missing}")
        upload_metadata = seen["voice_profile.sample_uploaded"].get("metadata") or {}
        exported_metadata = seen["voice_profile.exported"].get("metadata") or {}
        deleted_metadata = seen["voice_profile.deleted"].get("metadata") or {}
        self.assertNotIn("sample_artifacts", exported_metadata)
        self.assertNotIn("sample_artifacts", deleted_metadata)
        self.assertEqual(upload_metadata.get("bytes"), len(sample_bytes))
        self.assertEqual(upload_metadata.get("sha256"), sample_sha256)
        return {
            "audit_event_types": sorted(seen),
            "sample_upload_audit_target_id": seen["voice_profile.sample_uploaded"].get("target_id"),
            "profile_export_audit_target_id": seen["voice_profile.exported"].get("target_id"),
            "profile_delete_audit_target_id": seen["voice_profile.deleted"].get("target_id"),
            "sample_upload_audit_bytes": upload_metadata.get("bytes"),
            "sample_upload_audit_sha256": upload_metadata.get("sha256"),
            "audit_metadata_redacted": "sample_artifacts" not in exported_metadata and "sample_artifacts" not in deleted_metadata,
        }

    def verify_speech_or_limitation(self) -> None:
        if env_flag("B1_VOICEBOX_SKIP_SPEECH", False):
            self.assertTrue(self.speech_limitation, "B1_VOICEBOX_SKIP_SPEECH requires B1_VOICEBOX_SPEECH_LIMITATION")
            self.record_check(
                "speech_or_limitation_recorded",
                mode="upstream_limitation",
                limitation=self.speech_limitation,
                **self.build_identity_fields(),
            )
            self.samples.append({"label": "voicebox-speech-limitation", **self.build_identity_fields()})
            return
        payload = {
            "model": os.getenv("B1_VOICEBOX_SPEECH_MODEL", "tts-quality"),
            "voice": os.getenv("B1_VOICEBOX_SPEECH_VOICE", "default"),
            "input": os.getenv("B1_VOICEBOX_SPEECH_TEXT", "B1 AI Hub Voicebox acceptance test."),
            "response_format": os.getenv("B1_VOICEBOX_SPEECH_FORMAT", "wav"),
        }
        try:
            status, headers, body = self.request_raw(self.api_base, "POST", "/v1/audio/speech", payload, accept="audio/*")
        except AssertionError:
            if not self.speech_limitation:
                raise
            self.record_check(
                "speech_or_limitation_recorded",
                mode="upstream_limitation",
                limitation=self.speech_limitation,
                **self.build_identity_fields(),
            )
            self.samples.append({"label": "voicebox-speech-limitation", **self.build_identity_fields()})
            return
        self.assertGreater(len(body), 0, "Voicebox speech returned an empty body")
        self.assertGreaterEqual(status, 200)
        self.assertLess(status, 300)
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        digest = hashlib.sha256(body).hexdigest()
        self.record_check(
            "speech_or_limitation_recorded",
            mode="speech_validated",
            model=payload["model"],
            voice=payload["voice"],
            response_format=payload["response_format"],
            http_status=status,
            content_type=content_type.split(";", 1)[0],
            byte_count=len(body),
            sha256=digest,
            **self.build_identity_fields(),
        )
        self.samples.append(
            {
                "label": "voicebox-speech",
                "model": payload["model"],
                "voice": payload["voice"],
                "byte_count": len(body),
                "sha256": digest,
            }
        )

    def websocket_url(self) -> str:
        path = os.getenv("B1_VOICEBOX_WEBSOCKET_PATH", "/ws")
        parsed = urllib.parse.urlparse(self.build_url(self.voice_base, path))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urllib.parse.urlunparse(parsed._replace(scheme=scheme))

    async def websocket_connect_once(self) -> dict[str, Any]:
        try:
            import websockets
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on operator test host
            raise AssertionError("install the websockets package or record B1_VOICEBOX_WEBSOCKET_LIMITATION") from exc
        headers = self.headers(native=True, accept="*/*")
        headers.pop("Accept", None)
        self.enforce_token_transport_security(self.websocket_url(), token=self.native_api_key)
        kwargs: dict[str, Any] = {"open_timeout": 10.0, "max_size": None}
        if self.websocket_url().startswith("wss://"):
            kwargs["ssl"] = self.ssl_context()
        header_param = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
        if headers:
            kwargs[header_param] = headers
        async with websockets.connect(self.websocket_url(), **kwargs) as websocket:
            send_json = os.getenv("B1_VOICEBOX_WEBSOCKET_SEND_JSON", "").strip()
            if send_json:
                await websocket.send(send_json)
            received_type = "none"
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=float(os.getenv("B1_VOICEBOX_WEBSOCKET_RECV_TIMEOUT", "2")))
            except asyncio.TimeoutError:
                message = None
            except websockets.exceptions.ConnectionClosedOK:
                message = None
                received_type = "closed_ok"
            if isinstance(message, bytes):
                received_type = "bytes"
            elif isinstance(message, str):
                received_type = "text"
            return {"url": self.websocket_url(), "received_type": received_type}

    def verify_websocket_or_limitation(self) -> None:
        if env_flag("B1_VOICEBOX_SKIP_WEBSOCKET", False):
            self.assertTrue(self.websocket_limitation, "B1_VOICEBOX_SKIP_WEBSOCKET requires B1_VOICEBOX_WEBSOCKET_LIMITATION")
            self.record_check(
                "websocket_or_limitation_recorded",
                mode="upstream_limitation",
                limitation=self.websocket_limitation,
                **self.build_identity_fields(),
            )
            self.samples.append({"label": "voicebox-websocket-limitation", **self.build_identity_fields()})
            return
        try:
            result = asyncio.run(self.websocket_connect_once())
        except AssertionError:
            if not self.websocket_limitation:
                raise
            self.record_check(
                "websocket_or_limitation_recorded",
                mode="upstream_limitation",
                limitation=self.websocket_limitation,
                **self.build_identity_fields(),
            )
            self.samples.append({"label": "voicebox-websocket-limitation", **self.build_identity_fields()})
            return
        self.record_check(
            "websocket_or_limitation_recorded",
            mode="websocket_validated",
            path=os.getenv("B1_VOICEBOX_WEBSOCKET_PATH", "/ws"),
            received_type=result["received_type"],
            **self.build_identity_fields(),
        )
        self.samples.append(
            {
                "label": "voicebox-websocket",
                "path": os.getenv("B1_VOICEBOX_WEBSOCKET_PATH", "/ws"),
                "received_type": result["received_type"],
            }
        )


if __name__ == "__main__":
    unittest.main()
