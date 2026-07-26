from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
SHA256_DIGEST_RE = re.compile(r"@sha256:[a-fA-F0-9]{64}(?:$|[/?#])")
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PRIVATE_SOURCE_NETS = [
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
UPDATE_HEALTH_GATE_FORMAT = "b1-ai-hub-update-health-gate/v1"
UPDATE_HEALTH_REQUIRED_CHECKS = (
    "database",
    "redis",
    "storage:data-root",
    "storage:control-plane-data",
    "storage:artifact-temporary",
    "storage:backup-root",
    "storage:restore-test-root",
    "runtimes",
    "runtime-agent:status",
    "runtime-agent:mutation-guard",
    "runtime-agent:services",
    "deployment:compose-selection",
    "runtimes:production-readiness",
    "runtime-agent:metrics",
    "gpu:nvml",
    "hardware:resource-policy",
    "tls:caddy-ca",
    "tls:routing",
    "runtime:comfyui-build-info",
    "runtime:comfyui-status",
    "inference:tiny",
    "runtime-agent:unload",
    "artifact:delivery",
)


class UpdatePolicyError(ValueError):
    pass


def validate_target_version(value: str) -> str:
    version = value.strip()
    if not VERSION_RE.fullmatch(version):
        raise UpdatePolicyError("target_version must be 1..128 characters and contain only letters, numbers, '.', '_', ':', '+', or '-'")
    return version


def validate_service_name(value: str) -> str:
    service = value.strip()
    if not SERVICE_RE.fullmatch(service):
        raise UpdatePolicyError(f"invalid service name: {value}")
    return service


def validate_pinned_image_ref(value: str) -> str:
    image = value.strip()
    if not image:
        raise UpdatePolicyError("image reference cannot be blank")
    if ":latest" in image.split("@", 1)[0].rsplit("/", 1)[-1]:
        raise UpdatePolicyError(f"image reference must not use latest: {image}")
    if not SHA256_DIGEST_RE.search(image):
        raise UpdatePolicyError(f"image reference must include an immutable sha256 digest: {image}")
    return image


def source_ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global:
        return False
    return not any(ip in network for network in PRIVATE_SOURCE_NETS)


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


def source_url_path_is_safe(path: str) -> bool:
    for part in [item for item in path.split("/") if item]:
        if BAD_PERCENT_ESCAPE_RE.search(part):
            return False
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError:
            return False
        if decoded in {".", ".."}:
            return False
        if "/" in decoded or "\\" in decoded:
            return False
        if any(ord(character) < 32 for character in decoded):
            return False
    return True


def validate_source_url(value: str, *, resolver: HostnameResolver | None = None) -> str:
    source_url = value.strip()
    if not source_url:
        return ""
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdatePolicyError("source_url must be empty or an HTTPS URL")
    if parsed.username or parsed.password:
        raise UpdatePolicyError("source_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise UpdatePolicyError("source_url must not contain query strings or fragments")
    try:
        parsed.port
    except ValueError as exc:
        raise UpdatePolicyError("source_url contains an invalid port") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UpdatePolicyError("source_url must include a hostname")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UpdatePolicyError("source_url must not target localhost")
    if not source_url_path_is_safe(parsed.path):
        raise UpdatePolicyError("source_url path must not contain relative or encoded path-control segments")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        resolver = resolver or resolve_hostname_addresses
        try:
            resolved_addresses = resolver(hostname, parsed.port)
        except OSError as exc:
            raise UpdatePolicyError("source_url hostname could not be resolved safely") from exc
        if not resolved_addresses:
            raise UpdatePolicyError("source_url hostname could not be resolved safely")
        for address in resolved_addresses:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UpdatePolicyError("source_url hostname resolved to an invalid address") from exc
            if not source_ip_is_public(resolved_ip):
                raise UpdatePolicyError("source_url hostname must not resolve to private, loopback, link-local, or reserved IP ranges")
    else:
        if not source_ip_is_public(ip):
            raise UpdatePolicyError("source_url must not target private, loopback, link-local, or reserved IP ranges")
    return source_url


def normalize_image_refs(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not items:
        raise UpdatePolicyError("at least one pinned image reference is required")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise UpdatePolicyError("image_refs entries must be objects")
        service = validate_service_name(str(item.get("service") or ""))
        image = validate_pinned_image_ref(str(item.get("image") or ""))
        if service in seen:
            raise UpdatePolicyError(f"duplicate service in image_refs: {service}")
        seen.add(service)
        normalized.append({"service": service, "image": image})
    return sorted(normalized, key=lambda item: item["service"])


def build_update_preflight(target_version: str, image_refs: list[dict[str, Any]], source_url: str = "") -> dict[str, Any]:
    normalized_version = validate_target_version(target_version)
    normalized_refs = normalize_image_refs(image_refs)
    normalized_source_url = validate_source_url(source_url)
    return {
        "target_version": normalized_version,
        "source_url": normalized_source_url,
        "image_refs": normalized_refs,
        "image_count": len(normalized_refs),
        "pinned_images": True,
        "floating_tags_rejected": True,
        "requires_maintenance": True,
        "requires_backup": True,
        "requires_image_stage": True,
        "requires_self_test": True,
        "runtime_agent_image_action": "pinned_image_pull",
        "runtime_agent_rollback_action": "predefined_rollback",
    }


def _check_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    checks = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(checks, list):
        return []
    return [item for item in checks if isinstance(item, dict)]


def update_health_gate(report: dict[str, Any]) -> dict[str, Any]:
    report_status = str((report or {}).get("status") or "unknown").strip().lower()
    checks = _check_rows(report or {})
    by_name = {str(item.get("name") or ""): item for item in checks if str(item.get("name") or "").strip()}
    missing = [name for name in UPDATE_HEALTH_REQUIRED_CHECKS if name not in by_name]
    non_ok = []
    for name in UPDATE_HEALTH_REQUIRED_CHECKS:
        check = by_name.get(name)
        if check is None:
            continue
        status = str(check.get("status") or "unknown").strip().lower()
        if status != "ok":
            non_ok.append(
                {
                    "name": name,
                    "status": status,
                    "detail": str(check.get("detail") or "")[:500],
                }
            )

    blockers: list[str] = []
    if report_status != "ok":
        blockers.append(f"self-test status is {report_status}")
    if missing:
        blockers.append("missing required checks: " + ", ".join(missing))
    if non_ok:
        blockers.append(
            "non-ok required checks: "
            + ", ".join(f"{item['name']}={item['status']}" for item in non_ok)
        )

    ready = not blockers
    return {
        "format": UPDATE_HEALTH_GATE_FORMAT,
        "status": "ready" if ready else "blocked",
        "ready": ready,
        "report_status": report_status,
        "required_checks": list(UPDATE_HEALTH_REQUIRED_CHECKS),
        "missing_checks": missing,
        "non_ok_checks": non_ok,
        "blockers": blockers,
    }
