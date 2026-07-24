from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import ssl
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VOICEBOX_EVIDENCE_FORMAT = "b1-ai-hub-voicebox-remote-compatibility/v1"
VOICEBOX_REQUIRED_CHECKS = (
    "native_http_proxy_accessible",
    "profile_lifecycle_validated",
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


@unittest.skipUnless(os.getenv("B1_VOICEBOX_LIVE_TEST") == "1", "set B1_VOICEBOX_LIVE_TEST=1 to run live Voicebox compatibility tests")
class VoiceboxRemoteCompatibilityTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.voice_base = os.getenv("B1_VOICEBOX_BASE", "https://voice.ai.b1.germering").rstrip("/")
        cls.api_base = os.getenv("B1_VOICEBOX_API_BASE", "https://api.ai.b1.germering").rstrip("/")
        cls.api_key = os.getenv("B1_VOICEBOX_API_KEY", "").strip()
        cls.native_api_key = os.getenv("B1_VOICEBOX_NATIVE_API_KEY", cls.api_key).strip()
        cls.voice_host_header = os.getenv("B1_VOICEBOX_HOST_HEADER", "").strip()
        cls.api_host_header = os.getenv("B1_VOICEBOX_API_HOST_HEADER", "").strip()
        cls.timeout_seconds = float(os.getenv("B1_VOICEBOX_TIMEOUT_SECONDS", "180"))
        cls.upstream_version = os.getenv(
            "B1_VOICEBOX_UPSTREAM_VERSION",
            "Jamie Pine Voicebox v0.5.0 commit 2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
        )
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
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in VOICEBOX_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
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
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
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
                return int(getattr(response, "status", response.getcode())), dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if allow_http_error:
                return int(exc.code), dict(exc.headers.items()), body
            raise AssertionError(f"{method} {path} failed with HTTP {exc.code}: {body[:200]!r}") from exc

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

    def test_voicebox_remote_profile_speech_and_websocket(self) -> None:
        self.verify_native_http_proxy()
        self.verify_profile_lifecycle()
        self.verify_speech_or_limitation()
        self.verify_websocket_or_limitation()

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
            upstream_version=self.upstream_version,
        )
        self.samples.append({"label": "voicebox-native-http", "path": path, "http_status": status, "byte_count": len(body)})

    def verify_profile_lifecycle(self) -> None:
        display_name = f"B1 acceptance {uuid.uuid4().hex[:8]}"
        model_alias = os.getenv("B1_VOICEBOX_PROFILE_MODEL", "tts-quality")
        create_payload = {
            "display_name": display_name,
            "runtime": "voicebox",
            "engine": os.getenv("B1_VOICEBOX_PROFILE_ENGINE", "voicebox"),
            "model_alias": model_alias,
            "profile_type": os.getenv("B1_VOICEBOX_PROFILE_TYPE", "preset"),
            "status": "disabled",
            "visibility_roles": ["admin", "operator"],
            "metadata": {
                "acceptance": "voicebox-remote",
                "upstream_version": self.upstream_version,
            },
            "sample_artifacts": [],
        }
        created = self.request_json("POST", "/admin/voicebox/profiles", create_payload)
        profile_id = str(created.get("id") or "")
        self.assertTrue(profile_id.startswith("vp_"), f"unexpected Voicebox profile id: {profile_id}")
        try:
            fetched = self.request_json("GET", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}")
            exported = self.request_json("POST", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}/export")
        finally:
            deleted = self.request_json("DELETE", f"/admin/voicebox/profiles/{urllib.parse.quote(profile_id)}")
        self.assertEqual(fetched.get("id"), profile_id)
        self.assertEqual(exported.get("format"), "b1-ai-hub-voice-profile/v1")
        self.assertEqual(deleted.get("status"), "deleted")
        self.record_check(
            "profile_lifecycle_validated",
            profile_id=profile_id,
            model_alias=model_alias,
            profile_type=create_payload["profile_type"],
            export_format=exported.get("format"),
            deleted_status=deleted.get("status"),
        )
        self.samples.append({"label": "voice-profile-lifecycle", "profile_id": profile_id, "model_alias": model_alias})

    def verify_speech_or_limitation(self) -> None:
        if env_flag("B1_VOICEBOX_SKIP_SPEECH", False):
            self.assertTrue(self.speech_limitation, "B1_VOICEBOX_SKIP_SPEECH requires B1_VOICEBOX_SPEECH_LIMITATION")
            self.record_check(
                "speech_or_limitation_recorded",
                mode="upstream_limitation",
                upstream_version=self.upstream_version,
                limitation=self.speech_limitation,
            )
            self.samples.append({"label": "voicebox-speech-limitation", "upstream_version": self.upstream_version})
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
                upstream_version=self.upstream_version,
                limitation=self.speech_limitation,
            )
            self.samples.append({"label": "voicebox-speech-limitation", "upstream_version": self.upstream_version})
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
            content_type=content_type.split(";", 1)[0],
            byte_count=len(body),
            sha256=digest,
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
                upstream_version=self.upstream_version,
                limitation=self.websocket_limitation,
            )
            self.samples.append({"label": "voicebox-websocket-limitation", "upstream_version": self.upstream_version})
            return
        try:
            result = asyncio.run(self.websocket_connect_once())
        except AssertionError:
            if not self.websocket_limitation:
                raise
            self.record_check(
                "websocket_or_limitation_recorded",
                mode="upstream_limitation",
                upstream_version=self.upstream_version,
                limitation=self.websocket_limitation,
            )
            self.samples.append({"label": "voicebox-websocket-limitation", "upstream_version": self.upstream_version})
            return
        self.record_check(
            "websocket_or_limitation_recorded",
            mode="websocket_validated",
            path=os.getenv("B1_VOICEBOX_WEBSOCKET_PATH", "/ws"),
            received_type=result["received_type"],
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
