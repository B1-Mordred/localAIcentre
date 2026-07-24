from __future__ import annotations

import asyncio
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
    "system_stats_accessible",
    "models_accessible",
    "queue_accessible",
    "upload_image_accessible",
    "upload_mask_accessible",
    "prompt_submission",
    "websocket_events",
    "history_available",
    "view_artifact_accessible",
)

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
        cls.api_key = os.getenv("B1_NATIVE_COMFYUI_API_KEY", "").strip()
        cls.host_header = os.getenv("B1_NATIVE_COMFYUI_HOST_HEADER", "").strip()
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
    def url(cls, path: str) -> str:
        return urllib.parse.urljoin(cls.base_url + "/", path.lstrip("/"))

    @classmethod
    def websocket_url(cls) -> str:
        parsed = urllib.parse.urlparse(cls.url(f"/ws?clientId={urllib.parse.quote(cls.client_id)}"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urllib.parse.urlunparse(parsed._replace(scheme=scheme))

    @classmethod
    def request_json(cls, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any] | list[Any]:
        data = None
        headers = cls.headers()
        if payload is not None:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(cls.url(path), data=data, headers=headers, method=method)
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
        request = urllib.request.Request(cls.url(path), headers=headers, method=method)
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
        request = urllib.request.Request(cls.url(path), data=body, headers=headers, method=method)
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
        event_types: list[str] = []
        binary_messages = 0
        completed = False
        prompt_id = ""
        async with await self.connect_websocket() as websocket:
            response = await asyncio.to_thread(self.request_json, "POST", "/prompt", payload, self.timeout_seconds)
            self.assertIsInstance(response, dict)
            prompt_id = str(response.get("prompt_id") or "")
            self.assertTrue(prompt_id, f"POST /prompt did not return prompt_id: {response}")
            self.record_check(
                "prompt_submission",
                prompt_id=prompt_id,
                response_keys=sorted(str(key) for key in response.keys()),
                queue_number=response.get("number"),
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

    def first_history_artifact(self, prompt_id: str, history: dict[str, Any]) -> dict[str, Any]:
        record = history.get(prompt_id) if isinstance(history.get(prompt_id), dict) else history
        outputs = record.get("outputs") if isinstance(record, dict) else None
        if not isinstance(outputs, dict):
            raise AssertionError(f"/history/{prompt_id} did not contain native outputs")
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
                    return {
                        "node_id": str(node_id),
                        "output_key": output_key,
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder") if isinstance(item.get("subfolder"), str) else "",
                        "type": item.get("type") if isinstance(item.get("type"), str) else "output",
                    }
        raise AssertionError(f"/history/{prompt_id} did not contain image, video, GIF, or audio outputs")

    def verify_view_artifact(self, prompt_id: str, history: dict[str, Any]) -> None:
        artifact = self.first_history_artifact(prompt_id, history)
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
        content_type = headers.get("content-type", "")
        self.record_check(
            "view_artifact_accessible",
            prompt_id=prompt_id,
            node_id=artifact["node_id"],
            output_key=artifact["output_key"],
            filename=artifact["filename"],
            subfolder=artifact["subfolder"],
            type=artifact["type"],
            byte_count=len(body),
            content_type=content_type,
        )
        self.samples.append(
            {
                "label": "view-artifact",
                "prompt_id": prompt_id,
                "output_key": artifact["output_key"],
                "byte_count": len(body),
                "content_type": content_type,
            }
        )

    def test_native_rest_websocket_prompt_history_and_metadata(self) -> None:
        object_info = self.record_metadata_check("object_info_accessible", "/object_info")
        self.assertTrue(object_info, "native /object_info response is empty")
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
        self.verify_view_artifact(prompt_id, history)


if __name__ == "__main__":
    unittest.main()
