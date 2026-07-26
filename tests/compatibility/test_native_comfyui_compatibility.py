from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import ssl
import sys
import time
import unittest
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


NATIVE_COMFYUI_EVIDENCE_FORMAT = "b1-ai-hub-native-comfyui-compatibility/v1"
NATIVE_COMFYUI_REQUIRED_CHECKS = (
    "object_info_accessible",
    "object_info_node_accessible",
    "system_stats_accessible",
    "models_accessible",
    "queue_accessible",
    "upload_image_accessible",
    "upload_mask_accessible",
    "prompt_submission",
    "prompt_idempotency_replay",
    "websocket_events",
    "history_listing_accessible",
    "history_available",
    "durable_job_observable",
    "native_summary_observable",
    "durable_artifacts_observable",
    "queue_delete_accessible",
    "interrupt_accessible",
    "view_artifact_accessible",
)

ALLOW_INSECURE_HTTP_ENV = "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"
COMFYUI_OUTPUT_KEYS = ("images", "videos", "gifs", "audio")
TINY_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8ffff3f0005fe02fea7f3c553"
    "0000000049454e44ae426082"
)


def multipart_form_data(fields: dict[str, str], files: dict[str, tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"b1-comfyui-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content_type, body) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("ascii"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                body,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def response_header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def downloaded_b1_artifact_proof(artifact: dict[str, Any], headers: dict[str, str], content: bytes, *, index: int) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "artifact_index": index,
        "artifact_url": str(artifact.get("url") or ""),
        "artifact_id": str(artifact.get("id") or ""),
        "artifact_kind": str(artifact.get("kind") or ""),
        "artifact_source": str(artifact.get("source") or ""),
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


def b1_artifact_collection_proof(job_id: str, artifact_proofs: list[dict[str, Any]]) -> dict[str, Any]:
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
        "source": first_proof["artifact_source"],
        "byte_count": first_proof["download_bytes"],
        "content_type": first_proof["download_content_type"],
        "first_artifact_url": first_proof["artifact_url"],
        "first_artifact_bytes": first_proof["download_bytes"],
        "first_artifact_sha256": first_proof["download_sha256"],
        "first_artifact_mime_type": first_proof["artifact_mime_type"],
        "download_content_type": first_proof["download_content_type"],
        "download_content_length": first_proof["download_content_length"],
        "download_etag": first_proof["download_etag"],
        "download_accept_ranges": first_proof["download_accept_ranges"],
    }


def load_prompt_payload() -> dict[str, Any]:
    raw = os.getenv("B1_NATIVE_COMFYUI_PROMPT_JSON", "").strip()
    file_path = os.getenv("B1_NATIVE_COMFYUI_PROMPT_FILE", "").strip()
    if raw and file_path:
        raise unittest.SkipTest("set only one of B1_NATIVE_COMFYUI_PROMPT_JSON or B1_NATIVE_COMFYUI_PROMPT_FILE")
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8")
    if not raw:
        raise unittest.SkipTest("set B1_NATIVE_COMFYUI_PROMPT_FILE or B1_NATIVE_COMFYUI_PROMPT_JSON to a real native API prompt")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise unittest.SkipTest("native ComfyUI prompt must decode to a JSON object")
    if "prompt" not in payload:
        payload = {"prompt": payload}
    if not isinstance(payload.get("prompt"), dict) or not payload["prompt"]:
        raise unittest.SkipTest("native ComfyUI prompt payload must contain a non-empty prompt object")
    return payload


@unittest.skipUnless(os.getenv("B1_NATIVE_COMFYUI_LIVE_TEST") == "1", "set B1_NATIVE_COMFYUI_LIVE_TEST=1 to run live native ComfyUI tests")
class NativeComfyUiCompatibilityTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import websockets  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on operator test host
            raise unittest.SkipTest("install the websockets package to verify native ComfyUI /ws compatibility") from exc
        cls.checks = {}
        cls.samples = []
        cls.base_url = os.getenv("B1_NATIVE_COMFYUI_BASE", "https://comfy.ai.b1.germering").rstrip("/")
        cls.api_base_url = os.getenv("B1_NATIVE_COMFYUI_API_BASE", "https://api.ai.b1.germering").rstrip("/")
        cls.api_key = os.getenv("B1_NATIVE_COMFYUI_API_KEY", "").strip()
        cls.host_header = os.getenv("B1_NATIVE_COMFYUI_HOST_HEADER", "").strip()
        cls.api_host_header = os.getenv("B1_NATIVE_COMFYUI_API_HOST_HEADER", "").strip()
        cls.client_id = os.getenv("B1_NATIVE_COMFYUI_CLIENT_ID", f"b1-native-comfyui-{uuid.uuid4().hex}")
        cls.timeout_seconds = float(os.getenv("B1_NATIVE_COMFYUI_TIMEOUT_SECONDS", "300"))
        cls.prompt_payload = load_prompt_payload()

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_NATIVE_COMFYUI_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in NATIVE_COMFYUI_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": NATIVE_COMFYUI_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.base_url,
                    "api_base_url": cls.api_base_url,
                    "status": status,
                    "required_checks": list(NATIVE_COMFYUI_REQUIRED_CHECKS),
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
        parsed = urllib.parse.urlparse(cls.base_url)
        if parsed.scheme != "https":
            return None
        if not env_flag("B1_NATIVE_COMFYUI_TLS_VERIFY", True):
            return ssl._create_unverified_context()
        ca_file = os.getenv("B1_NATIVE_COMFYUI_CA_FILE", "").strip()
        if ca_file:
            return ssl.create_default_context(cafile=ca_file)
        return ssl.create_default_context()

    @classmethod
    def headers(cls) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if cls.api_key:
            headers["Authorization"] = f"Bearer {cls.api_key}"
        if cls.host_header:
            headers["Host"] = cls.host_header
        return headers

    @classmethod
    def api_headers(cls, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if cls.api_key:
            headers["Authorization"] = f"Bearer {cls.api_key}"
        if cls.api_host_header:
            headers["Host"] = cls.api_host_header
        return headers

    @classmethod
    def url(cls, path: str) -> str:
        return urllib.parse.urljoin(cls.base_url + "/", path.lstrip("/"))

    @classmethod
    def api_url(cls, path_or_url: str) -> str:
        parsed = urllib.parse.urlparse(path_or_url)
        if parsed.scheme:
            api_origin = urllib.parse.urlparse(cls.api_base_url)
            if parsed.scheme != api_origin.scheme or parsed.netloc != api_origin.netloc:
                raise AssertionError(f"refusing to send B1 API credentials to artifact URL outside API origin: {path_or_url}")
            return path_or_url
        return urllib.parse.urljoin(cls.api_base_url + "/", path_or_url.lstrip("/"))

    @classmethod
    def enforce_token_transport_security(cls, url: str) -> None:
        if not cls.api_key:
            return
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"https", "wss"}:
            return
        if parsed.scheme in {"http", "ws"} and env_flag(ALLOW_INSECURE_HTTP_ENV):
            return
        raise RuntimeError(
            "refusing to send a B1 native ComfyUI API key over plain HTTP/WebSocket; use HTTPS/WSS "
            f"or set {ALLOW_INSECURE_HTTP_ENV}=true only for an isolated development harness"
        )

    @classmethod
    def websocket_url(cls) -> str:
        parsed = urllib.parse.urlparse(cls.url(f"/ws?clientId={urllib.parse.quote(cls.client_id)}"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urllib.parse.urlunparse(parsed._replace(scheme=scheme))

    @classmethod
    def request_json(
        cls,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        data = None
        headers = cls.headers()
        headers.update(extra_headers or {})
        if payload is not None:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = cls.url(path)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, (dict, list)):
            raise AssertionError(f"{path} did not return a JSON object or array")
        return decoded

    @classmethod
    def request_bytes(cls, method: str, path: str, timeout: float | None = None) -> tuple[bytes, dict[str, str], int]:
        headers = cls.headers()
        headers["Accept"] = "*/*"
        url = cls.url(path)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            status = int(getattr(response, "status", 200))
        return body, response_headers, status

    @classmethod
    def request_api_json(
        cls,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        data = None
        headers = cls.api_headers()
        if payload is not None:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = cls.api_url(path)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, (dict, list)):
            raise AssertionError(f"{path} did not return a JSON object or array")
        return decoded

    @classmethod
    def request_api_bytes(cls, method: str, path_or_url: str, timeout: float | None = None) -> tuple[bytes, dict[str, str], int]:
        url = cls.api_url(path_or_url)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, headers=cls.api_headers(accept="*/*"), method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            status = int(getattr(response, "status", 200))
        return body, response_headers, status

    @classmethod
    def request_status(
        cls,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str], int]:
        data = None
        headers = cls.headers()
        headers["Accept"] = "*/*"
        headers.update(extra_headers or {})
        if payload is not None:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = cls.url(path)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            status = int(getattr(response, "status", 200))
        return body, response_headers, status

    @classmethod
    def request_multipart_json(
        cls,
        method: str,
        path: str,
        fields: dict[str, str],
        files: dict[str, tuple[str, str, bytes]],
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        body, content_type = multipart_form_data(fields, files)
        headers = cls.headers()
        headers["Content-Type"] = content_type
        url = cls.url(path)
        cls.enforce_token_transport_security(url)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout or cls.timeout_seconds, context=cls.ssl_context()) as response:
            response_body = response.read()
        decoded = json.loads(response_body.decode("utf-8"))
        if not isinstance(decoded, (dict, list)):
            raise AssertionError(f"{path} did not return a JSON object or array")
        return decoded

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def record_metadata_check(self, name: str, path: str) -> dict[str, Any] | list[Any]:
        payload = self.request_json("GET", path, timeout=60)
        if isinstance(payload, dict):
            sample = {"keys": sorted(str(key) for key in payload.keys())[:20], "type": "object"}
        else:
            sample = {"length": len(payload), "type": "array"}
        self.record_check(name, path=path, **sample)
        self.samples.append({"label": path.strip("/") or "root", **sample})
        return payload

    def verify_node_object_info(self, object_info: dict[str, Any] | list[Any]) -> None:
        self.assertIsInstance(object_info, dict)
        node_class = next(iter(sorted(str(key) for key in object_info.keys())))
        payload = self.request_json("GET", f"/object_info/{urllib.parse.quote(node_class, safe='')}", timeout=60)
        self.assertIsInstance(payload, dict)
        self.assertIn(node_class, payload)
        node_payload = payload.get(node_class)
        node_keys = sorted(str(key) for key in node_payload.keys())[:20] if isinstance(node_payload, dict) else []
        self.record_check(
            "object_info_node_accessible",
            path=f"/object_info/{node_class}",
            node_class=node_class,
            node_keys=node_keys,
        )
        self.samples.append({"label": "object-info-node", "node_class": node_class, "node_keys": node_keys})

    def verify_image_upload(self) -> dict[str, Any]:
        filename = f"b1-native-comfyui-upload-{uuid.uuid4().hex}.png"
        payload = self.request_multipart_json(
            "POST",
            "/upload/image",
            {"type": "input", "overwrite": "true"},
            {"image": (filename, "image/png", TINY_PNG_BYTES)},
            timeout=60,
        )
        self.assertIsInstance(payload, dict)
        response_keys = sorted(str(key) for key in payload.keys())
        self.record_check(
            "upload_image_accessible",
            path="/upload/image",
            filename=filename,
            response_keys=response_keys,
        )
        self.samples.append({"label": "upload-image", "filename": filename, "response_keys": response_keys})
        return payload

    def verify_mask_upload(self, original_upload: dict[str, Any]) -> None:
        original_ref = {
            "filename": str(original_upload.get("name") or ""),
            "subfolder": str(original_upload.get("subfolder") or ""),
            "type": str(original_upload.get("type") or "input"),
        }
        self.assertTrue(original_ref["filename"], f"/upload/image response did not include a usable name: {original_upload}")
        filename = f"b1-native-comfyui-mask-{uuid.uuid4().hex}.png"
        payload = self.request_multipart_json(
            "POST",
            "/upload/mask",
            {
                "type": "input",
                "overwrite": "true",
                "original_ref": json.dumps(original_ref, separators=(",", ":")),
            },
            {"image": (filename, "image/png", TINY_PNG_BYTES)},
            timeout=60,
        )
        self.assertIsInstance(payload, dict)
        response_keys = sorted(str(key) for key in payload.keys())
        self.record_check(
            "upload_mask_accessible",
            path="/upload/mask",
            filename=filename,
            original_ref=original_ref,
            response_keys=response_keys,
        )
        self.samples.append({"label": "upload-mask", "filename": filename, "response_keys": response_keys})

    async def connect_websocket(self):
        import websockets

        self.enforce_token_transport_security(self.websocket_url())
        headers = self.headers()
        headers.pop("Accept", None)
        kwargs: dict[str, Any] = {"ssl": self.ssl_context()} if self.websocket_url().startswith("wss://") else {}
        header_param = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
        if headers:
            kwargs[header_param] = headers
        return websockets.connect(self.websocket_url(), **kwargs)

    async def submit_prompt_and_collect_ws(self) -> str:
        payload = dict(self.prompt_payload)
        payload["client_id"] = self.client_id
        idempotency_key = f"b1-native-comfyui-{uuid.uuid4().hex}"
        idempotency_header = {"Idempotency-Key": idempotency_key}
        event_types: list[str] = []
        binary_messages = 0
        completed = False
        prompt_id = ""
        async with await self.connect_websocket() as websocket:
            response = await asyncio.to_thread(self.request_json, "POST", "/prompt", payload, self.timeout_seconds, idempotency_header)
            self.assertIsInstance(response, dict)
            prompt_id = str(response.get("prompt_id") or "")
            self.assertTrue(prompt_id, f"POST /prompt did not return prompt_id: {response}")
            self.record_check(
                "prompt_submission",
                prompt_id=prompt_id,
                response_keys=sorted(str(key) for key in response.keys()),
                queue_number=response.get("number"),
            )
            replay_body, replay_headers, replay_status = await asyncio.to_thread(
                self.request_status,
                "POST",
                "/prompt",
                payload,
                min(self.timeout_seconds, 60),
                idempotency_header,
            )
            self.assertEqual(replay_status, 200)
            replay_payload = json.loads(replay_body.decode("utf-8"))
            self.assertIsInstance(replay_payload, dict)
            self.assertEqual(replay_payload.get("prompt_id"), prompt_id)
            replay_header = replay_headers.get("x-b1-idempotent-replay", "")
            self.assertEqual(replay_header.lower(), "true")
            self.record_check(
                "prompt_idempotency_replay",
                prompt_id=prompt_id,
                response_keys=sorted(str(key) for key in replay_payload.keys()),
                replay_header=replay_header,
                idempotency_key_length=len(idempotency_key),
            )
            self.samples.append(
                {
                    "label": "prompt-idempotency-replay",
                    "prompt_id": prompt_id,
                    "replay_header": replay_header,
                }
            )
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                remaining = max(0.1, min(5.0, deadline - asyncio.get_running_loop().time()))
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if isinstance(message, bytes):
                    binary_messages += 1
                    continue
                event = json.loads(message)
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or "unknown")
                event_types.append(event_type)
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if event_type == "executing" and data.get("prompt_id") == prompt_id and data.get("node") is None:
                    completed = True
                    break
            self.assertTrue(event_types or binary_messages, "native /ws did not deliver any ComfyUI events")
            self.record_check(
                "websocket_events",
                prompt_id=prompt_id,
                event_types=event_types[:50],
                binary_messages=binary_messages,
                completed=completed,
            )
            self.samples.append(
                {
                    "label": "websocket-completed" if completed else "websocket-events",
                    "prompt_id": prompt_id,
                    "event_types": event_types[:20],
                    "binary_messages": binary_messages,
                    "completed": completed,
                }
            )
        return prompt_id

    def wait_for_history(self, prompt_id: str) -> dict[str, Any]:
        deadline = datetime.now(tz=UTC).timestamp() + self.timeout_seconds
        last_payload: dict[str, Any] | None = None
        while datetime.now(tz=UTC).timestamp() < deadline:
            payload = self.request_json("GET", f"/history/{urllib.parse.quote(prompt_id)}", timeout=60)
            self.assertIsInstance(payload, dict)
            last_payload = payload
            if prompt_id in payload:
                return payload
            if payload:
                return payload
            time.sleep(2)
        raise AssertionError(f"/history/{prompt_id} did not return completed native history; last payload: {last_payload}")

    def history_artifacts(self, prompt_id: str, history: dict[str, Any]) -> list[dict[str, Any]]:
        record = history.get(prompt_id) if isinstance(history.get(prompt_id), dict) else history
        outputs = record.get("outputs") if isinstance(record, dict) else None
        if not isinstance(outputs, dict):
            raise AssertionError(f"/history/{prompt_id} did not contain native outputs")
        artifacts = []
        for node_id, output in outputs.items():
            if not isinstance(output, dict):
                continue
            for output_key in COMFYUI_OUTPUT_KEYS:
                items = output.get(output_key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                        continue
                    artifacts.append(
                        {
                            "node_id": str(node_id),
                            "output_key": output_key,
                            "filename": item["filename"],
                            "subfolder": item.get("subfolder") if isinstance(item.get("subfolder"), str) else "",
                            "type": item.get("type") if isinstance(item.get("type"), str) else "output",
                        }
                    )
        if not artifacts:
            raise AssertionError(f"/history/{prompt_id} did not contain image, video, GIF, or audio outputs")
        return artifacts

    def verify_view_artifact(self, prompt_id: str, history: dict[str, Any]) -> None:
        artifact_proofs = []
        for index, artifact in enumerate(self.history_artifacts(prompt_id, history)):
            query = urllib.parse.urlencode(
                {
                    "filename": artifact["filename"],
                    "subfolder": artifact["subfolder"],
                    "type": artifact["type"],
                }
            )
            body, headers, status = self.request_bytes("GET", f"/view?{query}", timeout=120)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 0, "native /view returned an empty artifact body")
            artifact_proofs.append(
                {
                    "artifact_index": index,
                    "node_id": artifact["node_id"],
                    "output_key": artifact["output_key"],
                    "filename": artifact["filename"],
                    "subfolder": artifact["subfolder"],
                    "type": artifact["type"],
                    "byte_count": len(body),
                    "content_type": headers.get("content-type", ""),
                    "download_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
        first_proof = artifact_proofs[0]
        self.record_check(
            "view_artifact_accessible",
            prompt_id=prompt_id,
            view_count=len(artifact_proofs),
            verified_view_count=len(artifact_proofs),
            total_byte_count=sum(int(proof["byte_count"]) for proof in artifact_proofs),
            artifacts=artifact_proofs,
            node_id=first_proof["node_id"],
            output_key=first_proof["output_key"],
            filename=first_proof["filename"],
            subfolder=first_proof["subfolder"],
            type=first_proof["type"],
            byte_count=first_proof["byte_count"],
            content_type=first_proof["content_type"],
            download_sha256=first_proof["download_sha256"],
        )
        self.samples.append(
            {
                "label": "view-artifact",
                "prompt_id": prompt_id,
                "view_count": len(artifact_proofs),
                "total_byte_count": sum(int(proof["byte_count"]) for proof in artifact_proofs),
                "first_output_key": first_proof["output_key"],
                "first_byte_count": first_proof["byte_count"],
                "first_content_type": first_proof["content_type"],
            }
        )

    def wait_for_durable_job(self, prompt_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"limit": "10", "runtime": "comfyui", "native_prompt_id": prompt_id})
        deadline = datetime.now(tz=UTC).timestamp() + self.timeout_seconds
        last_payload: list[Any] | None = None
        while datetime.now(tz=UTC).timestamp() < deadline:
            payload = self.request_api_json("GET", f"/v1/media/jobs?{query}", timeout=60)
            self.assertIsInstance(payload, list)
            last_payload = payload
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if item.get("native_prompt_id") != prompt_id:
                    continue
                if item.get("runtime") != "comfyui" or item.get("model_alias") != "comfyui-native":
                    continue
                if item.get("operation") != "comfyui-prompt":
                    continue
                if item.get("state") in {"completed", "failed", "cancelled", "expired", "recovery_required"}:
                    return item
            time.sleep(2)
        raise AssertionError(f"B1 durable job for native prompt {prompt_id} did not reach a terminal state; last payload: {last_payload}")

    def verify_durable_job_and_artifacts(self, prompt_id: str) -> None:
        job = self.wait_for_durable_job(prompt_id)
        job_id = str(job.get("id") or "")
        self.assertTrue(job_id, f"durable job for native prompt {prompt_id} did not include an id")
        self.assertEqual(job.get("state"), "completed", f"durable job {job_id} did not complete cleanly: {job}")
        self.record_check(
            "durable_job_observable",
            prompt_id=prompt_id,
            job_id=job_id,
            state=job.get("state"),
            stage=job.get("stage"),
            progress=job.get("progress"),
            artifact_count=len(job.get("artifacts") or []) if isinstance(job.get("artifacts"), list) else 0,
        )
        native_summary = job.get("native_comfyui")
        self.assertIsInstance(native_summary, dict, f"durable job {job_id} did not expose native_comfyui summary: {job}")
        prompt_summary = native_summary.get("prompt")
        artifact_summary = native_summary.get("artifacts")
        self.assertIsInstance(prompt_summary, dict)
        self.assertIsInstance(artifact_summary, dict)
        self.assertTrue(native_summary.get("native_prompt_recorded"), f"native_comfyui summary did not record native prompt: {native_summary}")
        self.assertEqual(native_summary.get("native_prompt_id"), prompt_id)
        self.assertIsInstance(prompt_summary.get("node_count"), int)
        self.assertGreater(prompt_summary.get("node_count"), 0)
        self.assertTrue(prompt_summary.get("body_hash_present"))
        self.assertNotIn("native_prompt_hash", json.dumps(native_summary))
        self.record_check(
            "native_summary_observable",
            prompt_id=prompt_id,
            job_id=job_id,
            node_count=prompt_summary.get("node_count"),
            class_type_count=prompt_summary.get("class_type_count"),
            stored_artifact_count=artifact_summary.get("stored_artifact_count"),
            failed_ingest_count=artifact_summary.get("failed_ingest_count"),
            body_hash_present=prompt_summary.get("body_hash_present"),
            client_id_present=prompt_summary.get("client_id_present"),
        )
        artifacts_payload = self.request_api_json("GET", f"/v1/media/jobs/{urllib.parse.quote(job_id, safe='')}/artifacts", timeout=60)
        self.assertIsInstance(artifacts_payload, dict)
        artifacts = artifacts_payload.get("artifacts")
        self.assertIsInstance(artifacts, list)
        self.assertTrue(artifacts, f"durable job {job_id} did not expose B1 artifact records")
        artifact_proofs = []
        for index, artifact in enumerate(artifacts):
            self.assertIsInstance(artifact, dict)
            artifact_proofs.append(self.verify_b1_artifact_download(job_id, artifact, index))
        artifact_collection = b1_artifact_collection_proof(job_id, artifact_proofs)
        self.record_check(
            "durable_artifacts_observable",
            prompt_id=prompt_id,
            **artifact_collection,
            artifact_url_prefix=str(artifact_collection["artifact_url"]).split("/", 3)[:3],
        )
        self.samples.append(
            {
                "label": "durable-job-artifact",
                "prompt_id": prompt_id,
                "job_id": job_id,
                "artifact_count": len(artifact_proofs),
                "verified_artifact_count": len(artifact_proofs),
                "total_downloaded_bytes": artifact_collection["total_downloaded_bytes"],
                "first_artifact_bytes": artifact_collection["first_artifact_bytes"],
                "first_content_type": artifact_collection["content_type"],
            }
        )

    def verify_b1_artifact_download(self, job_id: str, artifact: dict[str, Any], index: int) -> dict[str, Any]:
        artifact_url = artifact.get("url")
        self.assertIsInstance(artifact_url, str, f"durable job {job_id} artifact did not include a URL: {artifact}")
        self.assertTrue(artifact_url.startswith("/artifacts/"), artifact)
        artifact_bytes = artifact.get("bytes")
        artifact_sha256 = artifact.get("sha256")
        artifact_mime_type = artifact.get("mime_type")
        self.assertIsInstance(artifact_bytes, int, artifact)
        self.assertGreater(artifact_bytes, 0, artifact)
        self.assertIsInstance(artifact_sha256, str, artifact)
        self.assertRegex(artifact_sha256, r"^[a-f0-9]{64}$", artifact)
        self.assertIsInstance(artifact_mime_type, str, artifact)
        self.assertTrue(artifact_mime_type, artifact)
        body, headers, status = self.request_api_bytes("GET", artifact_url, timeout=120)
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 0, f"B1 artifact URL for job {job_id} returned an empty body")
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(artifact_bytes, len(body), artifact)
        self.assertEqual(artifact_sha256, digest, artifact)
        self.assertEqual(response_header(headers, "content-length"), str(len(body)), headers)
        self.assertTrue(response_header(headers, "content-type"), headers)
        self.assertTrue(response_header(headers, "etag"), headers)
        self.assertEqual(response_header(headers, "accept-ranges").lower(), "bytes", headers)
        return downloaded_b1_artifact_proof(artifact, headers, body, index=index)

    def verify_queue_delete(self, prompt_id: str) -> None:
        body, headers, status = self.request_status("POST", "/queue", {"delete": [prompt_id]}, timeout=60)
        self.assertEqual(status, 200)
        self.record_check(
            "queue_delete_accessible",
            path="/queue",
            prompt_id=prompt_id,
            http_status=status,
            byte_count=len(body),
            content_type=headers.get("content-type", ""),
        )
        self.samples.append({"label": "queue-delete", "prompt_id": prompt_id, "http_status": status, "byte_count": len(body)})

    def verify_targeted_interrupt(self, prompt_id: str) -> None:
        body, headers, status = self.request_status("POST", "/interrupt", {"prompt_id": prompt_id}, timeout=60)
        self.assertEqual(status, 200)
        self.record_check(
            "interrupt_accessible",
            path="/interrupt",
            prompt_id=prompt_id,
            http_status=status,
            byte_count=len(body),
            content_type=headers.get("content-type", ""),
        )
        self.samples.append({"label": "targeted-interrupt", "prompt_id": prompt_id, "http_status": status, "byte_count": len(body)})

    def verify_history_listing(self) -> None:
        payload = self.request_json("GET", "/history?max_items=1", timeout=60)
        self.assertIsInstance(payload, dict)
        self.record_check(
            "history_listing_accessible",
            path="/history?max_items=1",
            history_keys=sorted(str(key) for key in payload.keys())[:20],
        )
        self.samples.append({"label": "history-listing", "history_count": len(payload)})

    def test_native_rest_websocket_prompt_history_and_metadata(self) -> None:
        object_info = self.record_metadata_check("object_info_accessible", "/object_info")
        self.assertTrue(object_info, "native /object_info response is empty")
        self.verify_node_object_info(object_info)
        self.record_metadata_check("system_stats_accessible", "/system_stats")
        self.record_metadata_check("models_accessible", "/models")
        self.record_metadata_check("queue_accessible", "/queue")
        image_upload = self.verify_image_upload()
        self.verify_mask_upload(image_upload)

        prompt_id = asyncio.run(self.submit_prompt_and_collect_ws())
        history = self.wait_for_history(prompt_id)
        self.record_check(
            "history_available",
            prompt_id=prompt_id,
            history_keys=sorted(str(key) for key in history.keys())[:20],
        )
        self.samples.append({"label": "prompt-submission", "prompt_id": prompt_id, "history_available": True})
        self.verify_durable_job_and_artifacts(prompt_id)
        self.verify_history_listing()
        self.verify_queue_delete(prompt_id)
        self.verify_targeted_interrupt(prompt_id)
        self.verify_view_artifact(prompt_id, history)


if __name__ == "__main__":
    unittest.main()
