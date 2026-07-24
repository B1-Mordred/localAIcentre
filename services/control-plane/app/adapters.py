from __future__ import annotations

import ipaddress
import socket
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from .catalog import CatalogAlias, operation_is_supported

ADAPTER_CONTRACT_VERSION = "b1-runtime-adapter/v1alpha1"

PRIVATE_RUNTIME_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
HostnameResolver = Callable[[str, int | None], list[str]]


class RuntimeResolutionError(ValueError):
    pass


def runtime_ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global:
        return False
    return not any(ip in network for network in PRIVATE_RUNTIME_NETS)


def resolve_hostname_addresses(hostname: str, port: int | None) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for result in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM):
        sockaddr = result[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses


def validate_external_runtime_base_url(value: str, *, resolver: HostnameResolver | None = None) -> tuple[str, str | None]:
    raw = value.strip()
    if not raw:
        return "", "base URL is not configured"
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return "", "external runtime base URL must use https"
    if parsed.username or parsed.password:
        return "", "external runtime base URL must not contain credentials"
    if parsed.query or parsed.fragment:
        return "", "external runtime base URL must not contain query or fragment components"
    if not parsed.hostname:
        return "", "external runtime base URL must include a hostname"
    try:
        port = parsed.port
    except ValueError:
        return "", "external runtime base URL has an invalid port"

    path = parsed.path.rstrip("/")
    path_parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in path_parts):
        return "", "external runtime base URL path must not contain relative segments"

    hostname = parsed.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        resolver = resolver or resolve_hostname_addresses
        try:
            resolved_addresses = resolver(hostname, port)
        except OSError:
            return "", "external runtime hostname could not be resolved safely"
        if not resolved_addresses:
            return "", "external runtime hostname could not be resolved safely"
        for address in resolved_addresses:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                return "", "external runtime hostname resolved to an invalid address"
            if not runtime_ip_is_public(resolved_ip):
                return "", "external runtime hostname must not resolve to private, loopback, link-local, or reserved IP ranges"
    else:
        if not runtime_ip_is_public(ip):
            return "", "external runtime base URL must not target private, loopback, link-local, or reserved IP ranges"

    if ":" in hostname:
        netloc = f"[{hostname}]"
        if port is not None:
            netloc = f"{netloc}:{port}"
    else:
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", "")), None


@dataclass(frozen=True)
class RuntimeAdapter:
    name: str
    base_url: str
    modalities: tuple[str, ...]
    operations: tuple[str, ...]
    requires_gpu: bool
    external: bool = False
    health_path: str = "/healthz"
    native_api: bool = False
    openai_compatible: bool = False
    configured: bool = True
    configuration_error: str | None = None
    api_key: str = ""

    def supports(self, alias: CatalogAlias, operation: str | None = None) -> bool:
        if not self.configured:
            return False
        if alias.alias.modality not in self.modalities:
            return False
        if operation and not operation_is_supported(operation, alias.operations, alias.alias.modality):
            return False
        if operation and not operation_is_supported(operation, self.operations, alias.alias.modality):
            return False
        return self.name in alias.runtimes

    def url_for(self, path: str) -> str:
        if not self.configured:
            raise RuntimeResolutionError(f"runtime {self.name} is not configured: {self.configuration_error or 'missing configuration'}")
        base = self.base_url.rstrip("/")
        suffix = path.lstrip("/")
        if base.endswith("/v1") and suffix.startswith("v1/"):
            suffix = suffix[3:]
        return f"{base}/{suffix}"

    def request_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self, timeout_seconds: float = 3.0) -> dict[str, Any]:
        import httpx

        if not self.configured:
            return {
                "name": self.name,
                "status": "unconfigured",
                "details": {"configuration_error": self.configuration_error or "missing configuration"},
                **self.public_dict(),
            }
        url = self.url_for(self.health_path)
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                headers = self.request_headers()
                response = await client.get(url, headers=headers) if headers else await client.get(url)
            details: Any
            try:
                details = response.json()
            except ValueError:
                details = response.text[:200]
            status = "ok" if response.status_code < 400 else "degraded"
            if response.status_code < 400 and isinstance(details, dict):
                reported_status = details.get("status")
                if isinstance(reported_status, str) and reported_status.strip():
                    status = "ok" if reported_status.strip().lower() in {"ok", "healthy"} else reported_status.strip().lower()
            return {"name": self.name, "status": status, "http_status": response.status_code, "details": details, **self.public_dict()}
        except httpx.HTTPError as exc:
            return {"name": self.name, "status": "unreachable", "error": exc.__class__.__name__, **self.public_dict()}

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("base_url")
        data.pop("api_key")
        data["capabilities"] = self.capability_discovery()
        data["adapter_contract"] = self.adapter_contract()
        return data

    def capability_discovery(self) -> dict[str, Any]:
        return {
            "modalities": list(self.modalities),
            "operations": list(self.operations),
            "requires_gpu": self.requires_gpu,
            "external": self.external,
            "native_api": self.native_api,
            "openai_compatible": self.openai_compatible,
            "configured": self.configured,
        }

    def adapter_contract(self) -> dict[str, Any]:
        scheduler_surface = "global-gpu-lease" if self.requires_gpu and not self.external else "cpu-or-external"
        if self.openai_compatible:
            submit_surface = "openai-compatible-http"
        elif self.native_api:
            submit_surface = "native-http-websocket"
        elif self.name == "generic-http":
            submit_surface = "reserved-generic-http"
        else:
            submit_surface = "control-plane-managed"
        runtime_control = "runtime-agent-predefined-actions" if not self.external else "not-available-for-external-runtime"
        lifecycle = "b1-runtime-hooks" if not self.external else "not-available-for-external-runtime"
        events = "native-websocket-bridge" if self.native_api else "job-sse"
        return {
            "version": ADAPTER_CONTRACT_VERSION,
            "surfaces": {
                "scheduler": scheduler_surface,
                "submit": submit_surface,
                "events": events,
                "runtime_control": runtime_control,
                "lifecycle": lifecycle,
                "health": self.health_path,
            },
            "methods": {
                "capability_discovery": "implemented",
                "model_listing": lifecycle if not self.external else "health-endpoint-only",
                "health": "implemented",
                "validate_request_profile": "resolver-and-admission-policy",
                "load_warm_model": lifecycle,
                "submit_stream_inference": submit_surface,
                "progress_events": events,
                "cancel_interrupt": "native-proxy-or-job-state",
                "unload_free_memory": runtime_control,
                "active_queued_work_discovery": "runtime-state-and-native-queue",
                "metrics": "runtime-agent-metrics" if not self.external else "health-endpoint-only",
                "failure_classification": "implemented",
                "graceful_forced_recovery": runtime_control,
            },
        }

    def openai_url(self, path: str) -> str:
        if not self.openai_compatible:
            raise RuntimeResolutionError(f"runtime {self.name} is not configured for OpenAI-compatible HTTP forwarding")
        return self.url_for(path)

    def openai_payload(self, payload: dict[str, Any], resolution: "RuntimeResolution") -> dict[str, Any]:
        if not self.openai_compatible:
            raise RuntimeResolutionError(f"runtime {self.name} is not configured for OpenAI-compatible HTTP forwarding")
        forwarded = {key: value for key, value in payload.items() if key != "runtime_policy" and value is not None}
        forwarded["model"] = resolution.model_id
        if not self.external:
            forwarded["b1_resolved_model_version"] = resolution.resolved_model_version
        return forwarded

    async def post_openai_json(self, path: str, payload: dict[str, Any], timeout_seconds: float = 120.0) -> tuple[int, dict[str, str], Any]:
        import httpx

        headers = {"Accept": "application/json", **self.request_headers()}
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(self.openai_url(path), json=payload, headers=headers)
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "x-request-id", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
        }
        try:
            body: Any = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:1000], "type": "runtime_response_error"}}
        return response.status_code, response_headers, body


@dataclass(frozen=True)
class RuntimeResolution:
    public_alias: str
    model_id: str
    model_version: str
    resolved_model_version: str
    runtime: str
    preferred_runtime: str
    requires_gpu: bool
    resource_label: str
    runtime_policy: str

    def to_job_fields(self) -> dict[str, str]:
        return {
            "resolved_model_version": self.resolved_model_version,
            "runtime": self.runtime,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeRegistry:
    def __init__(self, adapters: list[RuntimeAdapter], allow_external: bool = False) -> None:
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.allow_external = allow_external

    def public_adapters(self) -> list[dict[str, Any]]:
        return [self.adapters[name].public_dict() for name in sorted(self.adapters)]

    def adapter(self, name: str) -> RuntimeAdapter | None:
        return self.adapters.get(name)

    def compatible_runtimes(self, alias: CatalogAlias, operation: str | None = None, runtime_policy: str = "any") -> list[RuntimeAdapter]:
        candidates: list[RuntimeAdapter] = []
        for runtime_name in alias.runtimes:
            adapter = self.adapter(runtime_name)
            if adapter is None:
                continue
            if not adapter.configured:
                continue
            if adapter.external and not self.allow_external:
                continue
            if runtime_policy == "non_comfy_only" and adapter.name == "comfyui":
                continue
            if adapter.supports(alias, operation):
                candidates.append(adapter)
        return candidates

    def resolve(self, alias: CatalogAlias, operation: str | None = None, runtime_policy: str = "any") -> RuntimeResolution:
        if alias.manifest is None:
            raise RuntimeResolutionError(f"alias {alias.alias.alias} is not backed by an installed model manifest")
        candidates = self.compatible_runtimes(alias, operation, runtime_policy)
        if not candidates:
            details = [f"policy {runtime_policy}"]
            if operation:
                details.append(f"operation {operation}")
                details.append(f"manifest operations {', '.join(alias.operations) or 'none'}")
            details.append(f"candidate runtimes {', '.join(alias.runtimes) or 'none'}")
            raise RuntimeResolutionError(f"alias {alias.alias.alias} has no compatible runtime for {'; '.join(details)}")

        preferred = alias.preferred_runtime
        selected = next((adapter for adapter in candidates if adapter.name == preferred), candidates[0])
        return RuntimeResolution(
            public_alias=alias.alias.alias,
            model_id=alias.manifest.id,
            model_version=alias.manifest.version,
            resolved_model_version=f"{alias.manifest.id}@{alias.manifest.version}",
            runtime=selected.name,
            preferred_runtime=preferred,
            requires_gpu=selected.requires_gpu and alias.manifest.resource_estimate.vram_gib > 0,
            resource_label=alias.decision.label,
            runtime_policy=runtime_policy,
        )


def build_runtime_registry(
    *,
    localai_url: str,
    comfyui_url: str,
    voicebox_url: str,
    audio_cpu_url: str,
    allow_external: bool,
    openai_compatible_base_url: str = "",
    openai_compatible_api_key: str = "",
    openai_compatible_configuration_error: str | None = None,
    generic_http_base_url: str = "",
    generic_http_configuration_error: str | None = None,
) -> RuntimeRegistry:
    openai_url, openai_error = validate_external_runtime_base_url(openai_compatible_base_url)
    generic_url, generic_error = validate_external_runtime_base_url(generic_http_base_url)
    if openai_compatible_configuration_error:
        openai_error = openai_compatible_configuration_error
    if generic_http_configuration_error:
        generic_error = generic_http_configuration_error
    return RuntimeRegistry(
        [
            RuntimeAdapter(
                name="localai",
                base_url=localai_url,
                modalities=("llm", "vlm", "embedding", "tts", "stt", "image", "video"),
                operations=(
                    "chat",
                    "responses",
                    "embedding",
                    "text-to-speech",
                    "transcription",
                    "image-generation",
                    "image-edit",
                    "video-generation",
                    "image-to-video",
                ),
                requires_gpu=True,
                health_path="/readyz",
                openai_compatible=True,
            ),
            RuntimeAdapter(
                name="comfyui",
                base_url=comfyui_url,
                modalities=("image", "video", "workflow"),
                operations=(
                    "comfyui-prompt",
                    "workflow",
                    "image-generation",
                    "image-edit",
                    "background-removal",
                    "upscaling",
                    "video-generation",
                    "image-to-video",
                    "frame-interpolation",
                ),
                requires_gpu=True,
                native_api=True,
            ),
            RuntimeAdapter(
                name="voicebox",
                base_url=voicebox_url,
                modalities=("tts",),
                operations=("text-to-speech", "voice-cloning"),
                requires_gpu=True,
                native_api=True,
            ),
            RuntimeAdapter(
                name="audio-cpu",
                base_url=audio_cpu_url,
                modalities=("embedding", "tts", "stt"),
                operations=("embedding", "text-to-speech", "transcription"),
                requires_gpu=False,
                openai_compatible=True,
            ),
            RuntimeAdapter(
                name="openai-compatible",
                base_url=openai_url,
                modalities=("llm", "vlm", "embedding", "tts", "stt", "image", "video"),
                operations=(
                    "chat",
                    "responses",
                    "embedding",
                    "text-to-speech",
                    "transcription",
                    "image-generation",
                    "image-edit",
                    "video-generation",
                    "image-to-video",
                ),
                requires_gpu=False,
                external=True,
                health_path="/v1/models",
                openai_compatible=True,
                configured=openai_error is None,
                configuration_error=openai_error,
                api_key=openai_compatible_api_key,
            ),
            RuntimeAdapter(
                name="generic-http",
                base_url=generic_url,
                modalities=("llm", "vlm", "embedding", "tts", "stt", "image", "video"),
                operations=(
                    "chat",
                    "responses",
                    "embedding",
                    "text-to-speech",
                    "transcription",
                    "image-generation",
                    "image-edit",
                    "video-generation",
                    "image-to-video",
                ),
                requires_gpu=False,
                external=True,
                configured=generic_error is None,
                configuration_error=generic_error,
            ),
        ],
        allow_external=allow_external,
    )
