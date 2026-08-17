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
from app.adapters import (  # noqa: E402
    ADAPTER_CONTRACT_VERSION,
    RuntimeResolutionError,
    build_runtime_registry,
    validate_external_runtime_base_url,
    validate_lan_runtime_base_url,
)
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
        self.assertTrue(resolution.cpu_resident_allowed)
        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.openai_compatible)
        payload = adapter.openai_payload(
            {
                "model": "tts-fast",
                "input": "hello",
                "b1_model_alias": "client-spoof",
                "b1_cpu_residency_allowed": False,
            },
            resolution,
        )
        self.assertEqual(payload["model"], "b1-cpu-placeholder-tts")
        self.assertEqual(payload["b1_model_alias"], "tts-fast")
        self.assertTrue(payload["b1_cpu_residency_allowed"])
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

    def test_lan_p40_media_adapter_is_private_authenticated_and_fail_closed(self) -> None:
        self.patch_resolver(["192.168.2.109"])
        registry = self.registry(
            lan_p40_media_url="https://p40-worker.b1.germering:9443",
            lan_p40_media_hostname="p40-worker.b1.germering",
            lan_p40_media_allowed_cidrs=("192.168.2.109/32",),
            lan_p40_media_tls_ca_file="/run/secrets/p40-ca.crt",
            lan_p40_media_api_key="runtime-token",
        )
        adapter = registry.adapter("lan-p40-media")

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.configured)
        self.assertTrue(adapter.private_lan)
        self.assertEqual(adapter.health_path, "/media/healthz")
        self.assertEqual(adapter.request_headers(), {"Authorization": "Bearer runtime-token"})
        self.assertIn("studio-seated-character", adapter.operations)
        self.assertNotIn("studio-panel-shot", adapter.operations)

        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="seated-p40", modality="image", preferred_runtime="lan-p40-media", status="installed")],
            manifests=[
                manifest(
                    "seated-p40-model",
                    "image",
                    ["seated-p40"],
                    ["lan-p40-media"],
                    "lan-p40-media",
                    vram_gib=5.8,
                    operations=["studio-seated-character"],
                )
            ],
            policy=ResourcePolicy(),
        )
        self.assertEqual(
            registry.resolve(catalog.require_alias("seated-p40"), operation="studio-seated-character").runtime,
            "lan-p40-media",
        )

    def test_lan_p40_media_adapter_rejects_dns_outside_approved_cidr(self) -> None:
        self.patch_resolver(["100.100.100.100"])
        adapter = self.registry(
            lan_p40_media_url="https://p40-worker.b1.germering:9443",
            lan_p40_media_hostname="p40-worker.b1.germering",
            lan_p40_media_allowed_cidrs=("192.168.2.109/32",),
            lan_p40_media_tls_ca_file="/run/secrets/p40-ca.crt",
        ).adapter("lan-p40-media")

        self.assertIsNotNone(adapter)
        self.assertFalse(adapter.configured)
        self.assertIn("approved CIDRs", adapter.configuration_error)

    def test_panel_compositor_resolves_to_in_process_cpu_adapter(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="studio-panel-shot", modality="image", preferred_runtime="panel-cpu", status="installed")],
            manifests=[
                manifest(
                    "panel-compositor",
                    "image",
                    ["studio-panel-shot"],
                    ["panel-cpu"],
                    "panel-cpu",
                    vram_gib=0.0,
                    operations=["studio-panel-shot"],
                )
            ],
            policy=ResourcePolicy(),
        )
        registry = self.registry()
        adapter = registry.adapter("panel-cpu")
        resolution = registry.resolve(catalog.require_alias("studio-panel-shot"), operation="studio-panel-shot")

        self.assertIsNotNone(adapter)
        self.assertFalse(adapter.requires_gpu)
        self.assertEqual(resolution.runtime, "panel-cpu")
        self.assertFalse(resolution.requires_gpu)
        self.assertEqual(adapter.adapter_contract()["surfaces"]["scheduler"], "cpu-or-external")
        self.assertEqual(asyncio.run(adapter.health())["status"], "ok")

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

    def test_lan_runtime_validation_requires_exact_host_and_approved_cidr(self) -> None:
        resolver = lambda _hostname, _port: ["192.168.2.109"]

        normalized, error = validate_lan_runtime_base_url(
            "https://p40-worker.b1.germering:9443",
            approved_hostname="p40-worker.b1.germering",
            approved_cidrs=("192.168.2.109/32",),
            resolver=resolver,
        )

        self.assertEqual(normalized, "https://p40-worker.b1.germering:9443")
        self.assertIsNone(error)
        for url, hostname, addresses in (
            ("http://p40-worker.b1.germering:9443", "p40-worker.b1.germering", ["192.168.2.109"]),
            ("https://other.b1.germering:9443", "p40-worker.b1.germering", ["192.168.2.109"]),
            ("https://p40-worker.b1.germering:9443/v1", "p40-worker.b1.germering", ["192.168.2.109"]),
            ("https://p40-worker.b1.germering:9443", "p40-worker.b1.germering", ["192.168.2.110"]),
        ):
            with self.subTest(url=url, addresses=addresses):
                rejected, rejection = validate_lan_runtime_base_url(
                    url,
                    approved_hostname=hostname,
                    approved_cidrs=("192.168.2.109/32",),
                    resolver=lambda _host, _port, answers=addresses: answers,
                )
                self.assertEqual(rejected, "")
                self.assertIsNotNone(rejection)

        normalized_path, path_error = validate_lan_runtime_base_url(
            "https://p40-worker.b1.germering:9443/deepseek",
            approved_hostname="p40-worker.b1.germering",
            approved_cidrs=("192.168.2.109/32",),
            approved_path="/deepseek",
            resolver=resolver,
        )
        self.assertEqual(normalized_path, "https://p40-worker.b1.germering:9443/deepseek")
        self.assertIsNone(path_error)

    def test_lan_worker_adapter_is_private_managed_and_redacts_trust_paths(self) -> None:
        self.patch_resolver(["192.168.2.109"])
        registry = self.registry(
            lan_localai_worker_url="https://p40-worker.b1.germering:9443",
            lan_localai_worker_hostname="p40-worker.b1.germering",
            lan_localai_worker_allowed_cidrs=("192.168.2.109/32",),
            lan_localai_worker_tls_ca_file="/run/trust/p40-worker.crt",
            lan_localai_worker_api_key="hook-token",
        )
        adapter = registry.adapter("lan-localai-worker")

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.configured)
        self.assertTrue(adapter.private_lan)
        public = adapter.public_dict()
        self.assertNotIn("tls_ca_file", public)
        self.assertNotIn("approved_hostname", public)
        self.assertNotIn("approved_cidrs", public)
        self.assertNotIn("api_key", public)
        self.assertEqual(adapter.request_headers(), {"Authorization": "Bearer hook-token"})
        self.assertEqual(public["adapter_contract"]["surfaces"]["scheduler"], "global-gpu-lease")
        self.assertEqual(public["adapter_contract"]["methods"]["metrics"], "authenticated-lan-runtime-metrics")

    def test_lan_deepseek_adapter_uses_isolated_path_and_private_trust_policy(self) -> None:
        self.patch_resolver(["192.168.2.109"])
        registry = self.registry(
            lan_deepseek_worker_url="https://p40-worker.b1.germering:9443/deepseek",
            lan_deepseek_worker_hostname="p40-worker.b1.germering",
            lan_deepseek_worker_allowed_cidrs=("192.168.2.109/32",),
            lan_deepseek_worker_tls_ca_file="/run/trust/p40-worker.crt",
            lan_deepseek_worker_api_key="hook-token",
        )
        adapter = registry.adapter("lan-deepseek-worker")

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.configured)
        self.assertTrue(adapter.private_lan)
        self.assertEqual(
            adapter.openai_url("/v1/chat/completions"),
            "https://p40-worker.b1.germering:9443/deepseek/v1/chat/completions",
        )
        self.assertEqual(adapter.request_headers(), {"Authorization": "Bearer hook-token"})
        self.assertEqual(adapter.modalities, ("llm",))
        self.assertEqual(adapter.operations, ("chat", "responses"))

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
            def __init__(self, timeout: float, **kwargs: object) -> None:
                self.timeout = timeout
                self.__class__.init_calls.append({"timeout": timeout, **kwargs})

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
                self.__class__.calls.append({"url": url, "json": json, "headers": headers})
                return FakeResponse()

        FakeAsyncClient.calls = []  # type: ignore[attr-defined]
        FakeAsyncClient.init_calls = []  # type: ignore[attr-defined]
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
        self.assertEqual(FakeAsyncClient.init_calls[0]["timeout"], 120.0)  # type: ignore[attr-defined]

        self.patch_resolver(["192.168.2.109"])
        lan_adapter = self.registry(
            lan_localai_worker_url="https://p40-worker.b1.germering:9443",
            lan_localai_worker_hostname="p40-worker.b1.germering",
            lan_localai_worker_allowed_cidrs=("192.168.2.109/32",),
            lan_localai_worker_tls_ca_file="/run/trust/p40-worker.crt",
            lan_localai_worker_api_key="hook-token",
        ).adapter("lan-localai-worker")
        self.assertIsNotNone(lan_adapter)

        asyncio.run(lan_adapter.post_openai_json("/v1/chat/completions", {"model": "laguna-s-quality"}))

        self.assertEqual(FakeAsyncClient.init_calls[1]["timeout"], 3700.0)  # type: ignore[attr-defined]
        self.assertEqual(FakeAsyncClient.init_calls[1]["verify"], "/run/trust/p40-worker.crt")  # type: ignore[attr-defined]

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

    def test_localai_openai_payload_preserves_reasoning_and_tool_history(self) -> None:
        catalog = ModelCatalog(
            aliases=[AliasDefinition(alias="chat-default", modality="llm", preferred_runtime="localai", status="installed")],
            manifests=[manifest("localai-chat-model", "llm", ["chat-default"], ["localai"], "localai", vram_gib=1.0)],
            policy=ResourcePolicy(),
        )
        registry = self.registry()
        resolution = registry.resolve(catalog.require_alias("chat-default"), operation="chat")
        adapter = registry.adapter("localai")

        self.assertIsNotNone(adapter)
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"laguna\"}"},
            }
        ]
        messages = [
            {"role": "user", "content": "check the repo"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I need to inspect the source before answering.",
                "tool_calls": tool_calls,
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"},
        ]

        payload = adapter.openai_payload(
            {
                "model": "chat-default",
                "messages": messages,
                "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
            resolution,
        )

        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["messages"][1]["reasoning_content"], "I need to inspect the source before answering.")
        self.assertEqual(payload["messages"][1]["tool_calls"], tool_calls)
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call_1")
        self.assertEqual(payload["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertFalse(payload["parallel_tool_calls"])

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
