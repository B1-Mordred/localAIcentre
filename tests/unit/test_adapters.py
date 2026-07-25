from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

import app.adapters as adapters  # noqa: E402
from app.adapters import ADAPTER_CONTRACT_VERSION, RuntimeResolutionError, build_runtime_registry, validate_external_runtime_base_url  # noqa: E402
from app.catalog import (  # noqa: E402
    AliasDefinition,
    ManifestFile,
    ManifestSource,
    ModelCatalog,
    ModelLicense,
    ModelManifest,
    ModelResourceEstimate,
    load_catalog,
)
from app.scheduler import ResourcePolicy  # noqa: E402


def manifest(
    model_id: str,
    modality: str,
    aliases: list[str],
    runtimes: list[str],
    preferred_runtime: str,
    vram_gib: float = 0.0,
    operations: list[str] | None = None,
) -> ModelManifest:
    default_operations = {
        "llm": ["chat"],
        "vlm": ["chat"],
        "embedding": ["embedding"],
        "tts": ["text-to-speech"],
        "stt": ["transcription"],
        "image": ["image-generation", "image-edit"],
        "video": ["video-generation", "image-to-video"],
        "workflow": ["workflow"],
    }
    return ModelManifest(
        id=model_id,
        version="0.1.0",
        display_name=model_id,
        modality=modality,
        operations=operations or default_operations.get(modality, ["test"]),
        source=ManifestSource(type="catalog", url="https://models.ai.b1.germering/test", revision="0.1.0"),
        files=[ManifestFile(path="internal", sha256="0" * 64, size_bytes=1)],
        runtimes=runtimes,
        preferred_runtime=preferred_runtime,
        resource_estimate=ModelResourceEstimate(vram_gib=vram_gib, ram_gib=0.1, disk_gib=0.001),
        license=ModelLicense(name="internal", redistribution="inference-only"),
        execution_modes=["hosted-inference"],
        aliases=aliases,
    )


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_resolver(["93.184.216.34"])

    def patch_resolver(self, addresses: list[str], *, raises: OSError | None = None) -> None:
        original = adapters.resolve_hostname_addresses

        def fake_resolver(hostname: str, port: int | None) -> list[str]:
            self.resolver_calls.append({"hostname": hostname, "port": port})
            if raises is not None:
                raise raises
            return list(addresses)

        self.resolver_calls: list[dict[str, Any]] = []
        adapters.resolve_hostname_addresses = fake_resolver
        self.addCleanup(lambda: setattr(adapters, "resolve_hostname_addresses", original))

    def registry(self, allow_external: bool = False, **kwargs):
        return build_runtime_registry(
            localai_url="http://localai",
            comfyui_url="http://comfyui",
            voicebox_url="http://voicebox",
            audio_cpu_url="http://audio-cpu",
            allow_external=allow_external,
            **kwargs,
        )

    def test_seed_cpu_alias_resolves_to_audio_cpu(self) -> None:
        catalog = load_catalog(ROOT / "model-catalog", ResourcePolicy())
        alias = catalog.require_alias("tts-fast")
        resolution = self.registry().resolve(alias, operation="text-to-speech")
        adapter = self.registry().adapter("audio-cpu")
        self.assertEqual(resolution.runtime, "audio-cpu")
        self.assertFalse(resolution.requires_gpu)
        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.openai_compatible)
        self.assertEqual(
            resolution.to_job_fields(),
            {"resolved_model_version": "b1-cpu-placeholder-tts@0.1.0", "runtime": "audio-cpu"},
        )

    def test_localai_adapter_uses_official_ready_endpoint(self) -> None:
        adapter = self.registry().adapter("localai")

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.health_path, "/readyz")

    def test_public_adapter_contract_exposes_versioned_capabilities(self) -> None:
        adapter = self.registry().adapter("comfyui")
        self.assertIsNotNone(adapter)

        public = adapter.public_dict()

        self.assertEqual(public["adapter_contract"]["version"], ADAPTER_CONTRACT_VERSION)
        self.assertEqual(public["adapter_contract"]["surfaces"]["scheduler"], "global-gpu-lease")
        self.assertEqual(public["adapter_contract"]["surfaces"]["submit"], "native-http-websocket")
        self.assertEqual(public["adapter_contract"]["methods"]["load_warm_model"], "b1-runtime-hooks")
        self.assertEqual(public["capabilities"]["modalities"], ["image", "video", "workflow"])
        self.assertIn("comfyui-prompt", public["capabilities"]["operations"])
        self.assertIn("image-generation", public["capabilities"]["operations"])
        self.assertTrue(public["capabilities"]["requires_gpu"])
        self.assertTrue(public["native_api"])

    def test_non_comfy_policy_selects_non_comfy_runtime_when_available(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="image-flex", modality="image", preferred_runtime="comfyui", status="installed")],
            manifests=[manifest("image-flex-model", "image", ["image-flex"], ["comfyui", "localai"], "comfyui", vram_gib=1.0)],
            policy=ResourcePolicy(),
        )
        resolution = self.registry().resolve(catalog.require_alias("image-flex"), runtime_policy="non_comfy_only")
        self.assertEqual(resolution.runtime, "localai")
        self.assertTrue(resolution.requires_gpu)

    def test_manifest_operations_are_enforced_during_resolution(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="image-flex", modality="image", preferred_runtime="localai", status="installed")],
            manifests=[
                manifest(
                    "image-flex-model",
                    "image",
                    ["image-flex"],
                    ["localai"],
                    "localai",
                    vram_gib=1.0,
                    operations=["image-edit"],
                )
            ],
            policy=ResourcePolicy(),
        )

        with self.assertRaises(RuntimeResolutionError) as caught:
            self.registry().resolve(catalog.require_alias("image-flex"), operation="image-generation")

        self.assertIn("has no compatible runtime", str(caught.exception))
        self.assertIn("operation image-generation", str(caught.exception))
        self.assertIn("manifest operations image-edit", str(caught.exception))

    def test_operation_aliases_allow_endpoint_specific_names(self) -> None:
        registry = self.registry()
        cases = [
            ("chat-model", "llm", "chat-default", ["localai"], "localai", ["chat"], "responses", "localai"),
            ("embed-model", "embedding", "embedding-default", ["audio-cpu"], "audio-cpu", ["embedding"], "embeddings", "audio-cpu"),
            ("image-model", "image", "image-default", ["comfyui"], "comfyui", ["text-to-image"], "image-generation", "comfyui"),
            ("tts-model", "tts", "tts-fast", ["audio-cpu"], "audio-cpu", ["text-to-speech"], "speech", "audio-cpu"),
            ("stt-model", "stt", "stt-default", ["audio-cpu"], "audio-cpu", ["transcription"], "audio-transcriptions", "audio-cpu"),
        ]
        for model_id, modality, alias_name, runtimes, preferred, operations, requested_operation, expected_runtime in cases:
            with self.subTest(alias=alias_name, requested_operation=requested_operation):
                catalog = ModelCatalog(
                    aliases=[AliasDefinition(alias=alias_name, modality=modality, preferred_runtime=preferred, status="installed")],
                    manifests=[manifest(model_id, modality, [alias_name], runtimes, preferred, operations=operations)],
                    policy=ResourcePolicy(),
                )

                resolution = registry.resolve(catalog.require_alias(alias_name), operation=requested_operation)

                self.assertEqual(resolution.runtime, expected_runtime)

    def test_alias_preferred_runtime_override_selects_compatible_runtime(self) -> None:
        catalog = ModelCatalog(
            aliases=[
                AliasDefinition(
                    alias="image-flex",
                    modality="image",
                    preferred_runtime="comfyui",
                    status="installed",
                    preferred_runtime_override="localai",
                    policy_source="database",
                )
            ],
            manifests=[manifest("image-flex-model", "image", ["image-flex"], ["comfyui", "localai"], "comfyui", vram_gib=1.0)],
            policy=ResourcePolicy(),
        )

        resolution = self.registry().resolve(catalog.require_alias("image-flex"))

        self.assertEqual(resolution.preferred_runtime, "localai")
        self.assertEqual(resolution.runtime, "localai")

    def test_external_runtime_is_rejected_when_disabled(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="remote-chat", modality="llm", preferred_runtime="openai-compatible", status="installed")],
            manifests=[manifest("remote-chat-model", "llm", ["remote-chat"], ["openai-compatible"], "openai-compatible")],
            policy=ResourcePolicy(),
        )
        with self.assertRaises(RuntimeResolutionError):
            self.registry(allow_external=False).resolve(catalog.require_alias("remote-chat"))
        resolution = self.registry(allow_external=True, openai_compatible_base_url="https://api.example.com").resolve(catalog.require_alias("remote-chat"))
        self.assertEqual(resolution.runtime, "openai-compatible")

    def test_external_runtime_is_rejected_when_unconfigured_even_if_enabled(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="remote-chat", modality="llm", preferred_runtime="openai-compatible", status="installed")],
            manifests=[manifest("remote-chat-model", "llm", ["remote-chat"], ["openai-compatible"], "openai-compatible")],
            policy=ResourcePolicy(),
        )
        registry = self.registry(allow_external=True)
        adapter = registry.adapter("openai-compatible")

        self.assertIsNotNone(adapter)
        self.assertFalse(adapter.configured)
        with self.assertRaises(RuntimeResolutionError):
            registry.resolve(catalog.require_alias("remote-chat"))

    def test_external_runtime_base_url_validation_rejects_unsafe_destinations(self) -> None:
        for url in (
            "http://api.example.com",
            "https://user:token@api.example.com",
            "https://127.0.0.1/v1",
            "https://192.168.2.10/v1",
            "https://api.example.com/v1?token=abc",
            "https://api.example.com/../admin",
            "https://api.example.com:bad/v1",
        ):
            with self.subTest(url=url):
                normalized, error = validate_external_runtime_base_url(url)
                self.assertEqual(normalized, "")
                self.assertIsNotNone(error)

    def test_external_runtime_base_url_rejects_encoded_path_controls(self) -> None:
        for url in (
            "https://api.example.com/v1/%2e%2e/admin",
            "https://api.example.com/v1/%2E/admin",
            "https://api.example.com/v1/safe%2Fadmin",
            "https://api.example.com/v1/safe%5Cadmin",
            "https://api.example.com/v1/%00admin",
            "https://api.example.com/v1/%",
            "https://api.example.com/v1/%2",
            "https://api.example.com/v1/%zz",
            "https://api.example.com/v1/%ffadmin",
        ):
            with self.subTest(url=url):
                normalized, error = validate_external_runtime_base_url(url)
                self.assertEqual(normalized, "")
                self.assertEqual(error, "external runtime base URL path must not contain relative or encoded path-control segments")

    def test_external_runtime_base_url_validation_rejects_private_dns_answers(self) -> None:
        self.patch_resolver(["203.0.113.10", "127.0.0.1"])

        normalized, error = validate_external_runtime_base_url("https://runtime.example.org/v1")

        self.assertEqual(normalized, "")
        self.assertEqual(error, "external runtime hostname must not resolve to private, loopback, link-local, or reserved IP ranges")
        self.assertEqual(self.resolver_calls[-1], {"hostname": "runtime.example.org", "port": None})

    def test_external_runtime_base_url_validation_fails_closed_on_dns_failure(self) -> None:
        self.patch_resolver([], raises=OSError("dns unavailable"))

        normalized, error = validate_external_runtime_base_url("https://runtime.example.org/v1")

        self.assertEqual(normalized, "")
        self.assertEqual(error, "external runtime hostname could not be resolved safely")

    def test_external_runtime_public_shape_redacts_secret_and_joins_v1_path(self) -> None:
        registry = self.registry(
            allow_external=True,
            openai_compatible_base_url="https://API.EXAMPLE.COM/v1/",
            openai_compatible_api_key="secret-token",
        )
        adapter = registry.adapter("openai-compatible")

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.configured)
        self.assertEqual(adapter.openai_url("/v1/chat/completions"), "https://api.example.com/v1/chat/completions")
        public = adapter.public_dict()
        self.assertNotIn("api_key", public)
        self.assertNotIn("base_url", public)
        self.assertTrue(public["external"])
        self.assertEqual(public["adapter_contract"]["version"], ADAPTER_CONTRACT_VERSION)
        self.assertEqual(public["adapter_contract"]["surfaces"]["scheduler"], "cpu-or-external")
        self.assertEqual(public["adapter_contract"]["methods"]["unload_free_memory"], "not-available-for-external-runtime")

    @unittest.skipIf(importlib.util.find_spec("httpx") is None, "httpx is not installed in this lightweight test environment")
    def test_openai_compatible_adapter_uses_configured_bearer_token(self) -> None:
        import httpx

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def json(self) -> dict[str, object]:
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"url": url, "json": json, "headers": headers})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        original = httpx.AsyncClient
        httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(httpx, "AsyncClient", original))

        adapter = self.registry(
            allow_external=True,
            openai_compatible_base_url="https://api.example.com/v1",
            openai_compatible_api_key="secret-token",
        ).adapter("openai-compatible")
        self.assertIsNotNone(adapter)

        status, _, body = asyncio.run(adapter.post_openai_json("/v1/models", {"model": "x"}))

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(FakeAsyncClient.calls[0]["url"], "https://api.example.com/v1/models")  # type: ignore[attr-defined]
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer secret-token")  # type: ignore[attr-defined]

    def test_localai_openai_payload_uses_resolved_model_and_strips_b1_policy(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="chat-default", modality="llm", preferred_runtime="localai", status="installed")],
            manifests=[manifest("localai-chat-model", "llm", ["chat-default"], ["localai"], "localai", vram_gib=1.0)],
            policy=ResourcePolicy(),
        )
        registry = self.registry()
        resolution = registry.resolve(catalog.require_alias("chat-default"), operation="chat")
        adapter = registry.adapter("localai")

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.openai_compatible)
        payload = adapter.openai_payload(
            {
                "model": "chat-default",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "temperature": None,
                "runtime_policy": "non_comfy_only",
                "b1_resolved_model_version": "client-spoof@9.9.9",
                "b1_internal_note": "must-not-forward",
            },
            resolution,
        )

        self.assertEqual(payload["model"], "localai-chat-model")
        self.assertEqual(payload["b1_resolved_model_version"], "localai-chat-model@0.1.0")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertNotIn("runtime_policy", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("b1_internal_note", payload)

    def test_external_openai_payload_does_not_send_internal_model_version(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="remote-chat", modality="llm", preferred_runtime="openai-compatible", status="installed")],
            manifests=[manifest("remote-chat-model", "llm", ["remote-chat"], ["openai-compatible"], "openai-compatible", vram_gib=0.0)],
            policy=ResourcePolicy(),
        )
        registry = self.registry(allow_external=True, openai_compatible_base_url="https://api.example.com")
        resolution = registry.resolve(catalog.require_alias("remote-chat"), operation="chat")
        adapter = registry.adapter("openai-compatible")

        self.assertIsNotNone(adapter)
        payload = adapter.openai_payload(
            {
                "model": "remote-chat",
                "messages": [],
                "b1_resolved_model_version": "client-spoof@9.9.9",
                "b1_internal_note": "must-not-forward",
            },
            resolution,
        )

        self.assertEqual(payload["model"], "remote-chat-model")
        self.assertNotIn("b1_resolved_model_version", payload)
        self.assertNotIn("b1_internal_note", payload)

    def test_non_openai_runtime_rejects_openai_forwarding(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="image-default", modality="image", preferred_runtime="comfyui", status="installed")],
            manifests=[manifest("comfy-image-model", "image", ["image-default"], ["comfyui"], "comfyui", vram_gib=1.0)],
            policy=ResourcePolicy(),
        )
        registry = self.registry()
        resolution = registry.resolve(catalog.require_alias("image-default"))
        adapter = registry.adapter("comfyui")

        self.assertIsNotNone(adapter)
        self.assertFalse(adapter.openai_compatible)
        with self.assertRaises(RuntimeResolutionError):
            adapter.openai_payload({"model": "image-default"}, resolution)

    @unittest.skipIf(importlib.util.find_spec("httpx") is None, "httpx is not installed in this lightweight test environment")
    def test_health_honors_runtime_reported_unconfigured_status(self) -> None:
        import httpx

        calls: list[str] = []

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {
                    "status": "unconfigured",
                    "engine": "scaffold",
                    "placeholder": True,
                    "capabilities": {"speech": False, "transcription": False, "embeddings": False},
                }

        class FakeAsyncClient:
            def __init__(self, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                calls.append(url)
                return FakeResponse()

        original = httpx.AsyncClient
        httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(httpx, "AsyncClient", original))

        adapter = self.registry().adapter("audio-cpu")
        self.assertIsNotNone(adapter)
        health = asyncio.run(adapter.health())

        self.assertEqual(calls, ["http://audio-cpu/healthz"])
        self.assertEqual(health["status"], "unconfigured")
        self.assertEqual(health["adapter_contract"]["version"], ADAPTER_CONTRACT_VERSION)
        self.assertEqual(health["capabilities"]["modalities"], ["embedding", "tts", "stt"])
        self.assertEqual(health["details"]["engine"], "scaffold")
        self.assertFalse(health["details"]["capabilities"]["speech"])


if __name__ == "__main__":
    unittest.main()
