from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "b1-model-client"))

from b1_model_client import __main__ as client  # noqa: E402


def load_modelhub_compatibility_module() -> Any:
    path = ROOT / "tests" / "compatibility" / "test_modelhub_client_sync.py"
    spec = importlib.util.spec_from_file_location("b1_modelhub_client_sync_compatibility", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load compatibility test module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelClientTests(unittest.TestCase):
    def force_local_planner(self) -> None:
        original = client.sync_plan_from_server

        def unavailable(*args: object, **kwargs: object) -> list[dict[str, Any]]:
            raise urllib.error.HTTPError("http://modelhub/modelhub/v1/sync/plan", 404, "missing", {}, None)

        client.sync_plan_from_server = unavailable
        self.addCleanup(lambda: setattr(client, "sync_plan_from_server", original))

    def symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink creation is unavailable")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

    def test_validate_base_url_canonicalizes_safe_http_endpoint(self) -> None:
        self.assertEqual(
            client.validate_base_url(" HTTPS://models.ai.b1.germering/modelhub/ "),
            "https://models.ai.b1.germering/modelhub",
        )

    def test_validate_base_url_rejects_unsafe_endpoint_forms(self) -> None:
        unsafe_values = [
            "",
            "models.ai.b1.germering",
            "ftp://models.ai.b1.germering",
            "https://user:pass@models.ai.b1.germering",
            "https://models.ai.b1.germering?token=secret",
            "https://models.ai.b1.germering?",
            "https://models.ai.b1.germering/#fragment",
            "https://models.ai.b1.germering/#",
            "https://models.ai.b1.germering/../admin",
            "https://models.ai.b1.germering/%2e%2e/admin",
            "https://models.ai.b1.germering/models%2fescape",
            "https://models.ai.b1.germering/models%3ftoken",
            "https://models.ai.b1.germering/models%23fragment",
            "https://models.ai.b1.germering/models%00name",
            "https://models.ai.b1.germering/models%name",
            "https://models.ai.b1.germering/models%2/name",
            "https://models.ai.b1.germering/models%zzname",
            "https://models.ai.b1.germering/models%ffname",
            "https://models.ai.b1.germering:bad",
            "https://models.ai.b1.germering/models\nadmin",
            "https://models.ai.b1.germering/models\\admin",
        ]
        for value in unsafe_values:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    client.validate_base_url(value)

    def test_request_json_rejects_unsafe_request_path_before_network(self) -> None:
        called = False

        def fake_urlopen(request: object, timeout: int = 30) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be called for unsafe request paths")

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            for path in ("/modelhub/v1/%/catalog", "/modelhub/v1/%2/catalog", "/modelhub/v1/%zz/catalog", "/modelhub/v1/%ffcatalog"):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(RuntimeError, "request path is unsafe"):
                        client.request_json("https://models.ai.b1.germering", path, None)
        finally:
            client.urllib.request.urlopen = original
        self.assertFalse(called)

    def test_model_id_validation_rejects_path_controls_before_requests(self) -> None:
        unsafe_values = [
            "",
            "../chat-default",
            "chat/default",
            "chat%2Fdefault",
            "chat%3Fdefault",
            "chat%23default",
            "chat default",
            "-chat-default",
            "a" * 129,
        ]
        for value in unsafe_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "model id or alias is unsafe"):
                    client.normalize_model_id(value) if value == "" else client.normalize_model_list([value])

        called = False

        def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal called
            called = True
            raise AssertionError("unsafe model id must not reach the request layer")

        original = client.request_json
        try:
            client.request_json = fake_request_json
            with self.assertRaisesRegex(RuntimeError, "model id or alias is unsafe"):
                client.model_record("https://models.ai.b1.germering", None, "chat/default")
        finally:
            client.request_json = original
        self.assertFalse(called)

    def test_catalog_default_models_rejects_unsafe_server_aliases(self) -> None:
        def fake_request_json(
            base_url: str,
            path: str,
            token: str | None,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            ca_file: str | None = None,
        ) -> dict[str, object]:
            return {"aliases": [{"id": "chat-default"}, {"id": "bad/alias"}]}

        original = client.request_json
        try:
            client.request_json = fake_request_json
            with self.assertRaisesRegex(RuntimeError, "model id or alias is unsafe"):
                client.catalog_default_models("https://models.ai.b1.germering", None)
        finally:
            client.request_json = original

    def test_request_json_rejects_bad_base_url_before_network(self) -> None:
        called = False

        def fake_urlopen(request: object, timeout: int = 30) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be called for unsafe base URLs")

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with self.assertRaisesRegex(RuntimeError, "query string"):
                client.request_json("https://models.ai.b1.germering?token=secret", "/modelhub/v1/catalog", None)
        finally:
            client.urllib.request.urlopen = original
        self.assertFalse(called)

    def test_request_json_refuses_http_bearer_token_without_explicit_opt_in(self) -> None:
        called = False

        def fake_urlopen(request: object, timeout: int = 30) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be called when token transport is unsafe")

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(client.ALLOW_INSECURE_HTTP_ENV, None)
                with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                    client.request_json("http://modelhub", "/modelhub/v1/catalog", "secret-token")
        finally:
            client.urllib.request.urlopen = original
        self.assertFalse(called)

    def test_request_json_allows_http_bearer_token_only_with_explicit_opt_in(self) -> None:
        seen: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        def fake_urlopen(request: object, timeout: int = 30) -> FakeResponse:
            seen["url"] = request.full_url
            seen.update({key.lower(): value for key, value in request.header_items()})
            return FakeResponse()

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {client.ALLOW_INSECURE_HTTP_ENV: "true"}, clear=False):
                self.assertEqual(client.request_json("http://modelhub", "/modelhub/v1/catalog", "secret-token"), {"ok": True})
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(seen["url"], "http://modelhub/modelhub/v1/catalog")
        self.assertEqual(seen["authorization"], "Bearer secret-token")

    def test_request_json_applies_temporary_host_resolution(self) -> None:
        seen: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        def fake_getaddrinfo(
            host: str | bytes | None,
            port: str | int | None,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[Any, ...]]:
            seen["resolved_host"] = host
            seen["resolved_port"] = port
            return []

        def fake_urlopen(request: object, timeout: int = 30) -> FakeResponse:
            seen["url"] = request.full_url
            client.socket.getaddrinfo("models.ai.b1.germering", 443)
            return FakeResponse()

        original_getaddrinfo = client.socket.getaddrinfo
        original_urlopen = client.urllib.request.urlopen
        try:
            client.socket.getaddrinfo = fake_getaddrinfo
            client.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {client.RESOLVE_HOSTS_ENV: "models.ai.b1.germering=127.0.0.1"}, clear=False):
                self.assertEqual(client.request_json("https://models.ai.b1.germering", "/modelhub/v1/catalog", None), {"ok": True})
        finally:
            client.socket.getaddrinfo = original_getaddrinfo
            client.urllib.request.urlopen = original_urlopen

        self.assertEqual(seen["url"], "https://models.ai.b1.germering/modelhub/v1/catalog")
        self.assertEqual(seen["resolved_host"], "127.0.0.1")
        self.assertEqual(seen["resolved_port"], 443)
        self.assertIs(client.socket.getaddrinfo, original_getaddrinfo)

    def test_request_json_rejects_invalid_temporary_host_resolution_entries(self) -> None:
        with patch.dict(os.environ, {client.RESOLVE_HOSTS_ENV: "https://models.ai.b1.germering=127.0.0.1"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must not include schemes or paths"):
                client.request_json("https://models.ai.b1.germering", "/modelhub/v1/catalog", None)

    def test_resolve_token_reads_private_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("file-token\n", encoding="utf-8")
            if os.name != "nt":
                token_file.chmod(0o600)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(client.TOKEN_ENV, None)
                os.environ.pop(client.TOKEN_FILE_ENV, None)
                args = argparse.Namespace(token=None, token_file=str(token_file))
                self.assertEqual(client.resolve_token(args), "file-token")

    def test_resolve_token_rejects_ambiguous_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("file-token\n", encoding="utf-8")
            if os.name != "nt":
                token_file.chmod(0o600)
            with patch.dict(os.environ, {client.TOKEN_ENV: "env-token"}, clear=False):
                args = argparse.Namespace(token=None, token_file=str(token_file))
                with self.assertRaisesRegex(RuntimeError, "ambiguous Model Hub credentials"):
                    client.resolve_token(args)

    @unittest.skipIf(os.name == "nt", "POSIX mode checks do not apply on Windows")
    def test_resolve_token_rejects_group_readable_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("file-token\n", encoding="utf-8")
            token_file.chmod(0o644)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(client.TOKEN_ENV, None)
                os.environ.pop(client.TOKEN_FILE_ENV, None)
                args = argparse.Namespace(token=None, token_file=str(token_file))
                with self.assertRaisesRegex(RuntimeError, "chmod 0600"):
                    client.resolve_token(args)

    def test_resolve_ca_file_reads_cli_or_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "root.crt"
            ca_file.write_text("test-ca", encoding="utf-8")
            with patch.dict(os.environ, {client.CA_FILE_ENV: str(ca_file)}, clear=False):
                self.assertEqual(client.resolve_ca_file(argparse.Namespace(ca_file=None)), str(ca_file))
            self.assertEqual(client.resolve_ca_file(argparse.Namespace(ca_file=str(ca_file))), str(ca_file))

    def test_resolve_ca_file_rejects_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-root.crt"
            with self.assertRaisesRegex(RuntimeError, "CA file is not a regular file"):
                client.resolve_ca_file(argparse.Namespace(ca_file=str(missing)))

    def test_request_json_uses_configured_ca_file_for_https(self) -> None:
        seen: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        def fake_create_default_context(*, cafile: str | None = None) -> str:
            seen["cafile"] = cafile
            return "ssl-context"

        def fake_urlopen(request: object, timeout: int = 30, context: object | None = None) -> FakeResponse:
            seen["url"] = request.full_url
            seen["context"] = context
            return FakeResponse()

        original_context = client.ssl.create_default_context
        original_urlopen = client.urllib.request.urlopen
        try:
            client.ssl.create_default_context = fake_create_default_context
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                ca_file = str(Path(tmp) / "root.crt")
                result = client.request_json("https://models.ai.b1.germering", "/modelhub/v1/catalog", "secret-token", ca_file=ca_file)
        finally:
            client.ssl.create_default_context = original_context
            client.urllib.request.urlopen = original_urlopen

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["url"], "https://models.ai.b1.germering/modelhub/v1/catalog")
        self.assertEqual(seen["cafile"], ca_file)
        self.assertEqual(seen["context"], "ssl-context")

    def test_modelhub_compatibility_range_probe_uses_hardened_transport_and_ca(self) -> None:
        harness = load_modelhub_compatibility_module()
        seen: dict[str, object] = {}

        class FakeResponse:
            status = 206

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self) -> bytes:
                return b"abc"

        def fake_modelhub_urlopen(request: object, *, timeout: int, ca_file: str | None = None) -> FakeResponse:
            seen["url"] = request.full_url
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            seen["timeout"] = timeout
            seen["ca_file"] = ca_file
            return FakeResponse()

        original = harness.client.modelhub_urlopen
        try:
            harness.client.modelhub_urlopen = fake_modelhub_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                test_case = harness.ModelHubClientSyncCompatibilityTests(
                    methodName="test_model_client_downloads_resumes_verifies_and_blocks_inference_only"
                )
                test_case.base_url = "https://models.ai.b1.germering"
                test_case.token = "secret-token"
                test_case.ca_file = "/tmp/b1-caddy-root.crt"
                test_case.accept_licenses = False
                partial_size = test_case.range_seed_partial(Path(tmp), {"blob": "a" * 64, "expected_size": 4})
                partial = Path(tmp) / "blobs" / f"{'a' * 64}.partial"
                self.assertEqual(partial.read_bytes(), b"abc")
        finally:
            harness.client.modelhub_urlopen = original

        self.assertEqual(partial_size, 3)
        self.assertEqual(seen["url"], f"https://models.ai.b1.germering/modelhub/v1/blobs/{'a' * 64}")
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["ca_file"], "/tmp/b1-caddy-root.crt")
        headers = seen["headers"]
        self.assertIsInstance(headers, dict)
        self.assertEqual(headers["authorization"], "Bearer secret-token")
        self.assertEqual(headers["range"], "bytes=0-2")

    def test_modelhub_compatibility_head_probe_validates_blob_metadata(self) -> None:
        harness = load_modelhub_compatibility_module()
        seen: dict[str, object] = {}
        blob = "a" * 64

        class FakeResponse:
            status = 200
            headers = {
                "ETag": client.expected_etag(blob),
                "Content-Length": "4",
                "X-Checksum-SHA256": blob,
                "Accept-Ranges": "bytes",
            }

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

        def fake_modelhub_urlopen(request: object, *, timeout: int, ca_file: str | None = None) -> FakeResponse:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            seen["timeout"] = timeout
            seen["ca_file"] = ca_file
            return FakeResponse()

        original = harness.client.modelhub_urlopen
        try:
            harness.client.modelhub_urlopen = fake_modelhub_urlopen
            test_case = harness.ModelHubClientSyncCompatibilityTests(
                methodName="test_model_client_downloads_resumes_verifies_and_blocks_inference_only"
            )
            test_case.checks = {}
            test_case.base_url = "https://models.ai.b1.germering"
            test_case.token = "secret-token"
            test_case.ca_file = "/tmp/b1-caddy-root.crt"
            test_case.accept_licenses = False
            test_case.sync_model = "chat-default"
            test_case.validate_blob_head_metadata({"blob": blob, "expected_size": 4})
        finally:
            harness.client.modelhub_urlopen = original

        self.assertEqual(seen["url"], f"https://models.ai.b1.germering/modelhub/v1/blobs/{blob}")
        self.assertEqual(seen["method"], "HEAD")
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["ca_file"], "/tmp/b1-caddy-root.crt")
        headers = seen["headers"]
        self.assertIsInstance(headers, dict)
        self.assertEqual(headers["authorization"], "Bearer secret-token")
        check = test_case.checks["head_metadata_validated"]
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["blob"], blob)
        self.assertEqual(check["expected_size"], 4)

    def test_modelhub_compatibility_conditional_get_validates_etag_revalidation(self) -> None:
        harness = load_modelhub_compatibility_module()
        seen: dict[str, object] = {}
        blob = "b" * 64

        def fake_modelhub_urlopen(request: object, *, timeout: int, ca_file: str | None = None) -> object:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            seen["timeout"] = timeout
            seen["ca_file"] = ca_file
            raise urllib.error.HTTPError(
                request.full_url,
                304,
                "Not Modified",
                {"ETag": client.expected_etag(blob), "X-Checksum-SHA256": blob},
                io.BytesIO(b""),
            )

        original = harness.client.modelhub_urlopen
        try:
            harness.client.modelhub_urlopen = fake_modelhub_urlopen
            test_case = harness.ModelHubClientSyncCompatibilityTests(
                methodName="test_model_client_downloads_resumes_verifies_and_blocks_inference_only"
            )
            test_case.checks = {}
            test_case.base_url = "https://models.ai.b1.germering"
            test_case.token = "secret-token"
            test_case.ca_file = "/tmp/b1-caddy-root.crt"
            test_case.accept_licenses = False
            test_case.sync_model = "chat-default"
            test_case.validate_blob_conditional_get({"blob": blob, "expected_size": 4})
        finally:
            harness.client.modelhub_urlopen = original

        self.assertEqual(seen["url"], f"https://models.ai.b1.germering/modelhub/v1/blobs/{blob}")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["ca_file"], "/tmp/b1-caddy-root.crt")
        headers = seen["headers"]
        self.assertIsInstance(headers, dict)
        self.assertEqual(headers["authorization"], "Bearer secret-token")
        self.assertEqual(headers["if-none-match"], client.expected_etag(blob))
        check = test_case.checks["etag_if_none_match_validated"]
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["blob"], blob)
        self.assertEqual(check["etag"], client.expected_etag(blob))

    def test_modelhub_compatibility_range_probe_refuses_plain_http_token(self) -> None:
        harness = load_modelhub_compatibility_module()
        called = False

        def fake_modelhub_urlopen(request: object, *, timeout: int, ca_file: str | None = None) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be called when token transport is unsafe")

        original = harness.client.modelhub_urlopen
        try:
            harness.client.modelhub_urlopen = fake_modelhub_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                test_case = harness.ModelHubClientSyncCompatibilityTests(
                    methodName="test_model_client_downloads_resumes_verifies_and_blocks_inference_only"
                )
                test_case.base_url = "http://models.ai.b1.germering"
                test_case.token = "secret-token"
                test_case.ca_file = None
                test_case.accept_licenses = False
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(client.ALLOW_INSECURE_HTTP_ENV, None)
                    with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                        test_case.range_seed_partial(Path(tmp), {"blob": "b" * 64, "expected_size": 4})
        finally:
            harness.client.modelhub_urlopen = original

        self.assertFalse(called)

    def test_planned_actions_skip_inference_only_models(self) -> None:
        self.force_local_planner()
        original = client.model_record
        try:
            client.model_record = lambda base_url, token, model_id, ca_file=None: {
                "id": "tts-fast",
                "downloadable": False,
                "files": [],
                "license": {"name": "Test", "redistribution": "inference-only"},
                "execution_modes": ["hosted-inference"],
            }
            with tempfile.TemporaryDirectory() as tmp:
                actions = client.planned_actions("http://modelhub", None, Path(tmp), ["tts-fast"])
        finally:
            client.model_record = original
        self.assertEqual(actions[0]["model"], "tts-fast")
        self.assertEqual(actions[0]["action"], "skip")
        self.assertEqual(actions[0]["reason"], "model is not downloadable")
        self.assertEqual(actions[0]["license"]["redistribution"], "inference-only")

    def test_planned_actions_keep_verified_blob_and_download_missing_blob(self) -> None:
        self.force_local_planner()
        payload = b"model-blob"
        digest = hashlib.sha256(payload).hexdigest()
        missing_digest = hashlib.sha256(b"missing").hexdigest()

        def fake_model_record(base_url: str, token: str | None, model_id: str, ca_file: str | None = None) -> dict[str, object]:
            return {
                "id": model_id,
                "version": "1.0.0",
                "display_name": "Downloadable Model",
                "downloadable": True,
                "source": {
                    "type": "direct-url",
                    "url": "https://models.example.test/model.gguf?token=secret",
                    "revision": "test",
                },
                "license": {"name": "Test", "redistribution": "downloadable", "acceptance_required": False},
                "resource_estimate": {"vram_gib": 1, "ram_gib": 1, "disk_gib": 1},
                "execution_modes": ["hosted-inference", "downloadable"],
                "files": [
                    {"sha256": digest, "size_bytes": len(payload)},
                    {"sha256": missing_digest, "size_bytes": 7},
                ],
            }

        original = client.model_record
        try:
            client.model_record = fake_model_record
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                (cache / "blobs").mkdir()
                (cache / "blobs" / digest).write_bytes(payload)
                actions = client.planned_actions("http://modelhub", None, cache, ["downloadable-model"])
        finally:
            client.model_record = original

        by_blob = {action["blob"]: action for action in actions}
        self.assertEqual(by_blob[digest]["action"], "keep")
        self.assertEqual(by_blob[missing_digest]["action"], "download")
        self.assertEqual(by_blob[missing_digest]["expected_size"], 7)
        self.assertEqual(by_blob[missing_digest]["source"]["url"], "https://models.example.test/model.gguf")
        self.assertFalse(by_blob[missing_digest]["requires_license_acceptance"])

    def test_planned_actions_prefers_server_sync_plan_and_adds_local_paths(self) -> None:
        digest = "a" * 64

        def fake_request_json(
            base_url: str,
            path: str,
            token: str | None,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            ca_file: str | None = None,
        ) -> dict[str, Any]:
            self.assertEqual(path, "/modelhub/v1/sync/plan")
            self.assertEqual(method, "POST")
            self.assertEqual(payload, {"models": ["chat-default"], "installed_blobs": []})
            return {"actions": [{"model": "chat-default", "blob": digest, "action": "download", "expected_size": 12}]}

        original = client.request_json
        try:
            client.request_json = fake_request_json
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                actions = client.planned_actions("http://modelhub", "token", cache, ["chat-default"])
        finally:
            client.request_json = original

        self.assertEqual(actions[0]["blob"], digest)
        self.assertEqual(actions[0]["path"], str(cache / "blobs" / digest))
        self.assertEqual(actions[0]["resume_from"], 0)

    def test_server_sync_plan_rejects_non_sha_blob_before_path_derivation(self) -> None:
        def fake_request_json(
            base_url: str,
            path: str,
            token: str | None,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            ca_file: str | None = None,
        ) -> dict[str, Any]:
            return {"actions": [{"model": "chat-default", "blob": "../escape", "action": "download", "expected_size": 12}]}

        original = client.request_json
        try:
            client.request_json = fake_request_json
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(RuntimeError, "sync plan blob must be a 64-character SHA-256"):
                    client.planned_actions("http://modelhub", "token", Path(tmp), ["chat-default"])
        finally:
            client.request_json = original

    def test_sync_once_recomputes_download_path_from_verified_blob(self) -> None:
        digest = "b" * 64
        seen: dict[str, object] = {}

        def fake_actions(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
            return [
                {
                    "action": "download",
                    "blob": digest.upper(),
                    "expected_size": 7,
                    "path": str(cache.parent / "outside-cache"),
                    "partial": str(cache.parent / "outside-cache.partial"),
                }
            ]

        def fake_download(
            base_url: str,
            token: str | None,
            sha256: str,
            expected_size: int,
            target: Path,
            *,
            source: dict[str, Any] | None = None,
            accept_licenses: bool = False,
            ca_file: str | None = None,
        ) -> dict[str, Any]:
            seen.update({"sha256": sha256, "target": target, "source": source})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload")
            return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size}

        original_plan = client.planned_actions
        original_download = client.download_blob
        try:
            client.planned_actions = fake_actions
            client.download_blob = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp) / "cache"
                payload = client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"])
                state = client.load_state(cache)
        finally:
            client.planned_actions = original_plan
            client.download_blob = original_download

        expected_target = Path(payload["cache"]) / "blobs" / digest
        self.assertEqual(seen["sha256"], digest)
        self.assertEqual(seen["target"], expected_target)
        self.assertEqual(seen["source"]["path"], str(expected_target))
        self.assertIn(digest, state["managed_blobs"])
        self.assertEqual(state["managed_blobs"][digest]["source"]["path"], str(expected_target))

    def test_local_blob_inventory_reports_only_hash_verified_blobs(self) -> None:
        valid_payload = b"verified-model-blob"
        valid_digest = hashlib.sha256(valid_payload).hexdigest()
        corrupt_digest = hashlib.sha256(b"expected-other-content").hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            blobs = cache / "blobs"
            blobs.mkdir(parents=True)
            (blobs / valid_digest).write_bytes(valid_payload)
            (blobs / corrupt_digest).write_bytes(b"corrupt-but-sha-named")
            (blobs / "not-a-digest").write_bytes(b"ignored")
            (blobs / f"{valid_digest}.partial").write_bytes(valid_payload)

            inventory = client.local_blob_inventory(cache)

        self.assertEqual(inventory, [{"sha256": valid_digest, "size_bytes": len(valid_payload)}])

    def test_local_blob_inventory_ignores_symlinked_digest_blobs(self) -> None:
        payload = b"verified-but-outside-cache"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside-blob"
            outside.write_bytes(payload)
            blobs = root / "cache" / "blobs"
            blobs.mkdir(parents=True)
            self.symlink_or_skip(outside, blobs / digest)

            inventory = client.local_blob_inventory(root / "cache")

        self.assertEqual(inventory, [])

    def test_pin_and_unpin_persist_local_state_without_plaintext_tokens(self) -> None:
        original = client.model_record
        try:
            client.model_record = lambda base_url, token, model_id, ca_file=None: {"display_name": f"Model {model_id}", "downloadable": True}
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                pin_args = argparse.Namespace(base_url="http://modelhub", token="secret-token", cache=str(cache), model=["chat-default"])
                self.assertEqual(client.pin(pin_args), 0)
                state = client.load_state(cache)
                state_file = cache / "b1-model-client-state.json"
                self.assertIn("chat-default", state["pins"])
                self.assertNotIn("secret-token", state_file.read_text(encoding="utf-8"))
                if os.name != "nt":
                    self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

                unpin_args = argparse.Namespace(cache=str(cache), model=["chat-default"])
                self.assertEqual(client.unpin(unpin_args), 0)
                self.assertEqual(client.load_state(cache)["pins"], {})
                if os.name != "nt":
                    self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
        finally:
            client.model_record = original

    def test_prune_removes_only_managed_unpinned_blobs_and_ignores_unmanaged_files(self) -> None:
        kept_digest = "1" * 64
        removed_digest = "2" * 64
        unmanaged_name = "unmanaged-file"

        def fake_plan(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
            return [{"action": "keep", "blob": kept_digest, "expected_size": 4}]

        original = client.sync_plan_from_server
        try:
            client.sync_plan_from_server = fake_plan
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                blobs = cache / "blobs"
                blobs.mkdir(parents=True)
                (blobs / kept_digest).write_bytes(b"keep")
                (blobs / removed_digest).write_bytes(b"drop")
                (blobs / unmanaged_name).write_bytes(b"do-not-touch")
                state = client.empty_state()
                state["pins"] = {"chat-default": {"model": "chat-default"}}
                state["managed_blobs"] = {
                    kept_digest: {"size_bytes": 4},
                    removed_digest: {"size_bytes": 4},
                    "../escape": {"size_bytes": 1},
                }
                client.save_state(cache, state)

                dry_plan = client.prune_plan("http://modelhub", None, cache, ["chat-default"])
                self.assertEqual([item["blob"] for item in dry_plan["candidates"]], [removed_digest])
                self.assertTrue((blobs / removed_digest).is_file())

                prune_args = argparse.Namespace(base_url="http://modelhub", token=None, cache=str(cache), model=["chat-default"], dry_run=False)
                self.assertEqual(client.prune(prune_args), 0)

                self.assertTrue((blobs / kept_digest).is_file())
                self.assertFalse((blobs / removed_digest).exists())
                self.assertTrue((blobs / unmanaged_name).is_file())
                self.assertNotIn(removed_digest, client.load_state(cache)["managed_blobs"])
        finally:
            client.sync_plan_from_server = original

    def test_daemon_once_syncs_pinned_models_and_prunes_managed_cache(self) -> None:
        kept_digest = "3" * 64
        removed_digest = "4" * 64

        def fake_plan(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
            self.assertEqual(models, ["chat-default"])
            return [{"action": "keep", "blob": kept_digest, "expected_size": 4, "path": str(cache / "blobs" / kept_digest)}]

        original = client.sync_plan_from_server
        try:
            client.sync_plan_from_server = fake_plan
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                blobs = cache / "blobs"
                blobs.mkdir(parents=True)
                (blobs / kept_digest).write_bytes(b"keep")
                (blobs / removed_digest).write_bytes(b"drop")
                state = client.empty_state()
                state["pins"] = {"chat-default": {"model": "chat-default"}}
                state["managed_blobs"] = {
                    kept_digest: {"size_bytes": 4},
                    removed_digest: {"size_bytes": 4},
                }
                client.save_state(cache, state)

                args = argparse.Namespace(
                    base_url="http://modelhub",
                    token=None,
                    cache=str(cache),
                    model=[],
                    dry_run=False,
                    prune=True,
                    once=True,
                    interval_seconds=1,
                )
                self.assertEqual(client.daemon(args), 0)

                self.assertTrue((blobs / kept_digest).is_file())
                self.assertFalse((blobs / removed_digest).exists())
                self.assertNotIn(removed_digest, client.load_state(cache)["managed_blobs"])
        finally:
            client.sync_plan_from_server = original

    def test_sync_once_downloads_with_supplied_endpoint_and_records_managed_blob(self) -> None:
        digest = "5" * 64
        seen: dict[str, object] = {}

        def fake_actions(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
            self.assertEqual(base_url, "http://modelhub")
            self.assertEqual(token, "sync-token")
            return [
                {
                    "action": "download",
                    "blob": digest,
                    "expected_size": 7,
                    "path": str(cache / "blobs" / digest),
                }
            ]

        def fake_download(
            base_url: str,
            token: str | None,
            sha256: str,
            expected_size: int,
            target: Path,
            *,
            source: dict[str, Any] | None = None,
            accept_licenses: bool = False,
            ca_file: str | None = None,
        ) -> dict[str, Any]:
            seen.update({"base_url": base_url, "token": token, "sha256": sha256, "expected_size": expected_size, "target": target})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload")
            return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size}

        original_plan = client.planned_actions
        original_download = client.download_blob
        try:
            client.planned_actions = fake_actions
            client.download_blob = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                payload = client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"])
                state = client.load_state(cache)
        finally:
            client.planned_actions = original_plan
            client.download_blob = original_download

        self.assertEqual(payload["changes"][0]["status"], "downloaded")
        self.assertEqual(seen["base_url"], "http://modelhub")
        self.assertEqual(seen["token"], "sync-token")
        self.assertIn(digest, state["managed_blobs"])

    def test_sync_once_requires_explicit_license_acceptance(self) -> None:
        digest = "6" * 64
        seen: dict[str, object] = {}

        def fake_actions(base_url: str, token: str | None, cache: Path, models: list[str], ca_file: str | None = None) -> list[dict[str, Any]]:
            return [
                {
                    "action": "download",
                    "model": "chat-default",
                    "blob": digest,
                    "expected_size": 7,
                    "path": str(cache / "blobs" / digest),
                    "requires_license_acceptance": True,
                    "model_metadata": {
                        "id": "licenced-model",
                        "version": "1.0.0",
                        "display_name": "Licenced Model",
                        "license": {"name": "Example", "acceptance_required": True},
                    },
                }
            ]

        def fake_download(
            base_url: str,
            token: str | None,
            sha256: str,
            expected_size: int,
            target: Path,
            *,
            source: dict[str, Any] | None = None,
            accept_licenses: bool = False,
            ca_file: str | None = None,
        ) -> dict[str, Any]:
            self.assertTrue(accept_licenses)
            seen["downloaded"] = True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload")
            return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size}

        original_plan = client.planned_actions
        original_download = client.download_blob
        try:
            client.planned_actions = fake_actions
            client.download_blob = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                with self.assertRaisesRegex(RuntimeError, "licence acceptance required"):
                    client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"])
                self.assertNotIn("downloaded", seen)
                payload = client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"], accept_licenses=True)
        finally:
            client.planned_actions = original_plan
            client.download_blob = original_download

        self.assertEqual(payload["changes"][0]["status"], "downloaded")
        self.assertTrue(seen["downloaded"])

    def test_local_plan_surfaces_top_level_license_acceptance_requirement(self) -> None:
        metadata = client.plan_action_metadata(
            {
                "id": "licenced-model",
                "version": "1.0.0",
                "downloadable": True,
                "requires_license_acceptance": True,
                "license": {"name": "Custom", "redistribution": "downloadable"},
            }
        )

        action = {
            "action": "download",
            "model": "chat-default",
            "blob": "7" * 64,
            **metadata,
        }

        self.assertTrue(metadata["requires_license_acceptance"])
        self.assertTrue(metadata["model_metadata"]["requires_license_acceptance"])
        self.assertTrue(client.action_requires_license_acceptance(action))
        self.assertEqual(client.accepted_license_refs_for_action(action), ["licenced-model@1.0.0"])

    def test_download_blob_validates_etag_content_length_and_range_metadata(self) -> None:
        payload = b"hello-world"
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes, headers: dict[str, str]) -> None:
                self.body = body
                self.headers = headers
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(self.body):
                    return b""
                end = len(self.body) if size < 0 else min(len(self.body), self.offset + size)
                chunk = self.body[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            return FakeResponse(
                payload,
                {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(payload)),
                },
            )

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                result = client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(target.read_bytes(), payload)
                if os.name != "nt":
                    self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        finally:
            client.urllib.request.urlopen = original

    def test_download_blob_requires_integrity_headers_before_reading_body(self) -> None:
        payload = b"missing-header"
        digest = hashlib.sha256(payload).hexdigest()
        complete_headers = {
            "ETag": f'"sha256:{digest}"',
            "X-Checksum-SHA256": digest,
            "Content-Length": str(len(payload)),
        }

        class FakeResponse:
            status = 200

            def __init__(self, headers: dict[str, str]) -> None:
                self.headers = headers

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                raise AssertionError("body must not be read when integrity headers are missing")

        for missing_header, expected_error in [
            ("ETag", "missing ETag"),
            ("X-Checksum-SHA256", "missing X-Checksum-SHA256"),
            ("Content-Length", "missing Content-Length"),
        ]:
            with self.subTest(missing_header=missing_header):
                headers = {key: value for key, value in complete_headers.items() if key != missing_header}

                def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
                    return FakeResponse(headers)

                original = client.urllib.request.urlopen
                try:
                    client.urllib.request.urlopen = fake_urlopen
                    with tempfile.TemporaryDirectory() as tmp:
                        target = Path(tmp) / "blobs" / digest
                        with self.assertRaisesRegex(RuntimeError, expected_error):
                            client.download_blob("http://modelhub", None, digest, len(payload), target)
                        self.assertFalse(target.exists())
                finally:
                    client.urllib.request.urlopen = original

    def test_download_blob_rejects_unexpected_success_status_before_reading_body(self) -> None:
        payload = b"status-check"
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            def __init__(self, status: int) -> None:
                self.status = status
                self.headers = {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(payload)),
                    "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
                }

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                raise AssertionError("body must not be read when HTTP status is unexpected")

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            return FakeResponse(206)

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                with self.assertRaisesRegex(RuntimeError, "unexpected HTTP status 206"):
                    client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertFalse(target.exists())
        finally:
            client.urllib.request.urlopen = original

    def test_download_blob_tightens_existing_verified_blob_permissions(self) -> None:
        payload = b"already-present"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blobs" / digest
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            if os.name != "nt":
                target.chmod(0o644)

            result = client.download_blob("http://modelhub", None, digest, len(payload), target)

            self.assertEqual(result["status"], "kept")
            self.assertEqual(target.read_bytes(), payload)
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_download_blob_refuses_symlinked_existing_target(self) -> None:
        payload = b"outside-existing-blob"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside-blob"
            outside.write_bytes(payload)
            target = root / "cache" / "blobs" / digest
            target.parent.mkdir(parents=True)
            self.symlink_or_skip(outside, target)

            with self.assertRaisesRegex(RuntimeError, "cache blob target must be a regular file"):
                client.download_blob("http://modelhub", None, digest, len(payload), target)

    def test_download_blob_refuses_symlinked_partial_before_network(self) -> None:
        payload = b"full-blob-payload"
        digest = hashlib.sha256(payload).hexdigest()
        called = False

        def fake_urlopen(request: object, timeout: int = 120) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be called for symlinked partial blobs")

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                outside = root / "outside-partial"
                outside.write_bytes(payload[:4])
                target = root / "cache" / "blobs" / digest
                target.parent.mkdir(parents=True)
                self.symlink_or_skip(outside, target.with_suffix(".partial"))

                with self.assertRaisesRegex(RuntimeError, "partial cache blob must be a regular file"):
                    client.download_blob("http://modelhub", None, digest, len(payload), target)
        finally:
            client.urllib.request.urlopen = original

        self.assertFalse(called)

    def test_download_blob_accepts_exact_resumed_content_range(self) -> None:
        partial_payload = b"hello-"
        remaining_payload = b"world"
        payload = partial_payload + remaining_payload
        digest = hashlib.sha256(payload).hexdigest()
        seen_ranges: list[str | None] = []

        class FakeResponse:
            status = 206

            def __init__(self) -> None:
                self.headers = {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(remaining_payload)),
                    "Content-Range": f"bytes {len(partial_payload)}-{len(payload) - 1}/{len(payload)}",
                }
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(remaining_payload):
                    return b""
                end = len(remaining_payload) if size < 0 else min(len(remaining_payload), self.offset + size)
                chunk = remaining_payload[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            headers = {key.lower(): value for key, value in request.header_items()}
            seen_ranges.append(headers.get("range"))
            return FakeResponse()

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                target.parent.mkdir(parents=True)
                target.with_suffix(".partial").write_bytes(partial_payload)
                result = client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(target.with_suffix(".partial").exists())
                if os.name != "nt":
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(seen_ranges, [f"bytes={len(partial_payload)}-"])

    def test_download_blob_rejects_mismatched_resumed_content_range_before_append(self) -> None:
        partial_payload = b"hello-"
        remaining_payload = b"world"
        payload = partial_payload + remaining_payload
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            status = 206

            def __init__(self) -> None:
                self.headers = {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(remaining_payload)),
                    "Content-Range": f"bytes {len(partial_payload)}-99/{len(payload)}",
                }
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                raise AssertionError("body should not be read when Content-Range is invalid")

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            return FakeResponse()

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                target.parent.mkdir(parents=True)
                partial = target.with_suffix(".partial")
                partial.write_bytes(partial_payload)
                with self.assertRaisesRegex(RuntimeError, "unexpected Content-Range"):
                    client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(partial.read_bytes(), partial_payload)
                self.assertFalse(target.exists())
        finally:
            client.urllib.request.urlopen = original

    def test_download_blob_restarts_when_resume_range_is_unsatisfiable(self) -> None:
        payload = b"restart-range"
        digest = hashlib.sha256(payload).hexdigest()
        seen_ranges: list[str | None] = []

        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(payload)),
                }
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(payload):
                    return b""
                end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                chunk = payload[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            headers = {key.lower(): value for key, value in request.header_items()}
            seen_ranges.append(headers.get("range"))
            if headers.get("range"):
                raise urllib.error.HTTPError(request.full_url, 416, "range not satisfiable", {}, None)
            return FakeResponse()

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                target.parent.mkdir(parents=True)
                target.with_suffix(".partial").write_bytes(b"stale")
                result = client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(target.with_suffix(".partial").exists())
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(seen_ranges, ["bytes=5-", None])

    def test_download_blob_restarts_when_resume_range_is_ignored_with_full_response(self) -> None:
        payload = b"ignored-range-full-response"
        digest = hashlib.sha256(payload).hexdigest()
        seen_ranges: list[str | None] = []

        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(payload)),
                }
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(payload):
                    return b""
                end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                chunk = payload[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            headers = {key.lower(): value for key, value in request.header_items()}
            seen_ranges.append(headers.get("range"))
            return FakeResponse()

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                target.parent.mkdir(parents=True)
                partial = target.with_suffix(".partial")
                partial.write_bytes(b"stale")
                result = client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(partial.exists())
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(seen_ranges, ["bytes=5-"])

    def test_download_blob_sends_license_acceptance_header_when_enabled(self) -> None:
        payload = b"accepted-model"
        digest = hashlib.sha256(payload).hexdigest()
        seen_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            headers = {
                "ETag": f'"sha256:{digest}"',
                "X-Checksum-SHA256": digest,
                "Content-Length": str(len(payload)),
            }

            def __init__(self) -> None:
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(payload):
                    return b""
                end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                chunk = payload[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            seen_headers.update({key.lower(): value for key, value in request.header_items()})
            return FakeResponse()

        source = {
            "requires_license_acceptance": True,
            "model_metadata": {"id": "licenced-model", "version": "1.0.0"},
        }
        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                result = client.download_blob(
                    "http://modelhub",
                    None,
                    digest,
                    len(payload),
                    target,
                    source=source,
                    accept_licenses=True,
                )
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(seen_headers["x-b1-accept-license"], "licenced-model@1.0.0")

    def test_load_state_rejects_malformed_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "b1-model-client-state.json").write_text(
                '{"format":"b1-model-client-cache/v1","pins":[],"managed_blobs":{}}',
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                client.load_state(cache)


if __name__ == "__main__":
    unittest.main()
