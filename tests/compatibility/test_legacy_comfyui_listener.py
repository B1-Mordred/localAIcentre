from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import unittest
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.support.evidence import write_private_json  # noqa: E402


LEGACY_COMFYUI_EVIDENCE_FORMAT = "b1-ai-hub-legacy-comfyui-listener/v1"
LEGACY_COMFYUI_REQUIRED_CHECKS = (
    "object_info_without_auth",
    "system_stats_without_auth",
    "websocket_without_auth",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def split_words(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


@unittest.skipUnless(os.getenv("B1_LEGACY_COMFY_LIVE_TEST") == "1", "set B1_LEGACY_COMFY_LIVE_TEST=1 to run live legacy ComfyUI tests")
class LegacyComfyUiListenerTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import websockets  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on operator test host
            raise unittest.SkipTest("install the websockets package to verify legacy ComfyUI /ws compatibility") from exc
        cls.checks = {}
        cls.samples = []
        cls.base_url = os.getenv("B1_LEGACY_COMFY_BASE", "http://127.0.0.1:8188").rstrip("/")
        cls.host_header = os.getenv("B1_LEGACY_COMFY_HOST_HEADER", "").strip()
        cls.client_id = os.getenv("B1_LEGACY_COMFY_CLIENT_ID", f"b1-legacy-comfyui-{uuid.uuid4().hex}")
        cls.timeout_seconds = float(os.getenv("B1_LEGACY_COMFY_TIMEOUT_SECONDS", "30"))
        cls.publish_mapping = os.getenv("B1_LEGACY_COMFY_PUBLISH", "8188:8188").strip()
        cls.bind_host = os.getenv("B1_LEGACY_COMFY_BIND", "").strip()
        cls.listen_port = os.getenv("B1_LEGACY_COMFY_PORT", "8188").strip()
        cls.allow_cidrs = split_words(os.getenv("B1_LEGACY_COMFY_ALLOW_CIDRS", "192.168.2.0/24 100.64.0.0/10"))

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_LEGACY_COMFY_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in LEGACY_COMFYUI_REQUIRED_CHECKS) else "incomplete"
        write_private_json(
            path,
            {
                "format": LEGACY_COMFYUI_EVIDENCE_FORMAT,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "base_url": cls.base_url,
                "status": status,
                "required_checks": list(LEGACY_COMFYUI_REQUIRED_CHECKS),
                "listener_policy": cls.listener_policy(),
                "checks": cls.checks,
                "samples": cls.samples,
            },
        )

    @classmethod
    def listener_policy(cls) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(cls.base_url)
        listen_port = int(cls.listen_port) if cls.listen_port.isdigit() else 8188
        return {
            "compose_profile": "legacy-comfy",
            "base_url": cls.base_url,
            "scheme": parsed.scheme,
            "port": parsed.port or listen_port,
            "bind_host": cls.bind_host,
            "publish_mapping": cls.publish_mapping,
            "listen_port": cls.listen_port,
            "allow_cidrs": cls.allow_cidrs,
            "compatibility_marker": "comfyui-legacy-8188",
            "gateway_target": "control-plane:8000",
            "direct_comfyui_backend": False,
            "authorization_headers_stripped": True,
            "cookie_headers_stripped": True,
            "csrf_headers_stripped": True,
            "bearer_auth_required": False,
        }

    @classmethod
    def ssl_context(cls) -> ssl.SSLContext | None:
        parsed = urllib.parse.urlparse(cls.base_url)
        if parsed.scheme != "https":
            return None
        if not env_flag("B1_LEGACY_COMFY_TLS_VERIFY", True):
            return ssl._create_unverified_context()
        ca_file = os.getenv("B1_LEGACY_COMFY_CA_FILE", "").strip()
        if ca_file:
            return ssl.create_default_context(cafile=ca_file)
        return ssl.create_default_context()

    @classmethod
    def headers(cls) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if cls.host_header:
            headers["Host"] = cls.host_header
        return headers

    @classmethod
    def url(cls, path: str) -> str:
        return urllib.parse.urljoin(cls.base_url + "/", path.lstrip("/"))

    @classmethod
    def websocket_url(cls) -> str:
        parsed = urllib.parse.urlparse(cls.url(f"/ws?clientId={urllib.parse.quote(cls.client_id)}"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urllib.parse.urlunparse(parsed._replace(scheme=scheme))

    @classmethod
    def request_json(cls, method: str, path: str) -> dict[str, Any] | list[Any]:
        request = urllib.request.Request(cls.url(path), headers=cls.headers(), method=method)
        with urllib.request.urlopen(request, timeout=cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, (dict, list)):
            raise AssertionError(f"{path} did not return a JSON object or array")
        return decoded

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def record_metadata_check(self, name: str, path: str) -> None:
        payload = self.request_json("GET", path)
        if isinstance(payload, dict):
            sample = {"keys": sorted(str(key) for key in payload.keys())[:20], "type": "object"}
        else:
            sample = {"length": len(payload), "type": "array"}
        parsed = urllib.parse.urlparse(self.url(path))
        self.record_check(
            name,
            path=path,
            url=urllib.parse.urlunparse(parsed._replace(query="", fragment="")),
            authorization_header_sent=False,
            cookie_header_sent=False,
            csrf_header_sent=False,
            host_header=self.host_header,
            **sample,
        )
        self.samples.append({"label": path.strip("/") or "root", **sample})

    async def verify_websocket(self) -> None:
        import websockets

        kwargs: dict[str, Any] = {"ssl": self.ssl_context()} if self.websocket_url().startswith("wss://") else {}
        async with websockets.connect(self.websocket_url(), **kwargs) as websocket:
            event_type = ""
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=min(5.0, self.timeout_seconds))
            except asyncio.TimeoutError:
                message = None
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = {"type": "text"}
                if isinstance(payload, dict):
                    event_type = str(payload.get("type") or "")
            self.record_check(
                "websocket_without_auth",
                websocket_url=self.websocket_url(),
                websocket_path=urllib.parse.urlparse(self.websocket_url()).path,
                websocket_scheme=urllib.parse.urlparse(self.websocket_url()).scheme,
                client_id=self.client_id,
                authorization_header_sent=False,
                cookie_header_sent=False,
                csrf_header_sent=False,
                received_initial_message=message is not None,
                initial_event_type=event_type,
            )
            self.samples.append({"label": "websocket", "client_id": self.client_id, "initial_event_type": event_type})

    def test_legacy_rest_and_websocket_without_bearer_auth(self) -> None:
        self.record_metadata_check("object_info_without_auth", "/object_info")
        self.record_metadata_check("system_stats_without_auth", "/system_stats")
        asyncio.run(self.verify_websocket())

        for name in LEGACY_COMFYUI_REQUIRED_CHECKS:
            self.assertEqual(self.checks.get(name, {}).get("status"), "ok", name)
