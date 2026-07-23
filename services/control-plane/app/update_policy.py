from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
SHA256_DIGEST_RE = re.compile(r"@sha256:[a-fA-F0-9]{64}(?:$|[/?#])")


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


def validate_source_url(value: str) -> str:
    source_url = value.strip()
    if not source_url:
        return ""
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdatePolicyError("source_url must be empty or an HTTPS URL")
    if parsed.username or parsed.password:
        raise UpdatePolicyError("source_url must not contain credentials")
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
