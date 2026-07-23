from __future__ import annotations

import ipaddress
from typing import Any, Callable

from .catalog import CatalogError, ModelCatalog


def manifest_is_downloadable(record: dict[str, Any]) -> bool:
    return bool(record.get("downloadable"))


def downloadable_versions_for(catalog: ModelCatalog, model_id: str) -> list[dict[str, Any]]:
    versions = catalog.versions_for(model_id)
    return [record for record in versions if manifest_is_downloadable(record)]


def downloadable_records_for_blob(catalog: ModelCatalog, sha256: str) -> list[dict[str, Any]]:
    digest = sha256.lower()
    records: list[dict[str, Any]] = []
    for manifest in catalog.list_manifests():
        record = catalog.manifest_record(manifest)
        if not manifest_is_downloadable(record):
            continue
        if any(file.sha256.lower() == digest for file in manifest.files):
            records.append(record)
    return records


def model_identifiers(model_id: str, record: dict[str, Any]) -> set[str]:
    identifiers = {model_id}
    if record.get("id"):
        identifiers.add(record["id"])
    if record.get("root"):
        identifiers.add(record["root"])
    if record.get("resolved_model"):
        identifiers.add(record["resolved_model"].get("id", ""))
    identifiers.update(record.get("aliases", []))
    identifiers.discard("")
    return identifiers


def model_allowed_by_allowed_set(allowed_models: list[str], model_id: str, record: dict[str, Any]) -> bool:
    allowed = set(allowed_models or [])
    if "*" in allowed:
        return True
    return bool(model_identifiers(model_id, record) & allowed)


def validate_allowed_models(
    allowed_models: list[str],
    record_provider: Callable[[str], dict[str, Any] | None],
) -> list[str]:
    if not allowed_models:
        raise CatalogError("allowed_models must contain at least one model ID, alias, or '*'")
    if "*" in allowed_models and len(allowed_models) > 1:
        raise CatalogError("'*' cannot be mixed with explicit allowed_models")
    for model_id in allowed_models:
        if model_id == "*":
            continue
        if record_provider(model_id) is None:
            raise CatalogError(f"model not found: {model_id}")
    return sorted(set(allowed_models))


def validate_cidr_allowlist(cidr_allowlist: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in cidr_allowlist:
        value = raw_value.strip()
        if not value:
            raise CatalogError("CIDR allowlist entries cannot be empty")
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise CatalogError(f"invalid CIDR allowlist entry: {raw_value}") from exc
        canonical = network.with_prefixlen
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return normalized


def normalized_ip_literal(remote_ip: str | None) -> str | None:
    if remote_ip is None:
        return None
    try:
        parsed = ipaddress.ip_address(remote_ip.strip())
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def client_ip_allowed_by_cidr(cidr_allowlist: list[str], remote_ip: str | None) -> bool:
    if not cidr_allowlist:
        return True
    normalized = normalized_ip_literal(remote_ip)
    if normalized is None:
        return False
    client_ip = ipaddress.ip_address(normalized)
    try:
        networks = [ipaddress.ip_network(entry, strict=False) for entry in cidr_allowlist]
    except ValueError:
        return False
    return any(client_ip in network for network in networks)


def build_sync_plan(catalog: ModelCatalog, models: list[str], installed_blob_sizes: dict[str, int]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    total_download_bytes = 0
    for model_id in models:
        if catalog.model_or_alias_record(model_id) is None:
            raise CatalogError(f"model not found: {model_id}")
        versions = downloadable_versions_for(catalog, model_id)
        if not versions:
            actions.append({"model": model_id, "action": "skip", "reason": "model is not downloadable"})
            continue
        version = versions[0]
        for file in version["files"]:
            digest = file["sha256"].lower()
            expected_size = int(file["size_bytes"])
            installed_size = installed_blob_sizes.get(digest)
            if installed_size == expected_size:
                action = "keep"
                bytes_to_download = 0
            elif installed_size is None:
                action = "download"
                bytes_to_download = expected_size
            else:
                action = "replace"
                bytes_to_download = expected_size
            total_download_bytes += bytes_to_download
            actions.append(
                {
                    "model": model_id,
                    "model_id": version["id"],
                    "version": version["version"],
                    "path": file["path"],
                    "blob": digest,
                    "action": action,
                    "expected_size": expected_size,
                    "installed_size": installed_size,
                    "download_bytes": bytes_to_download,
                    "url": f"/modelhub/v1/blobs/{digest}",
                    "etag": f"\"sha256:{digest}\"",
                }
            )
    return {
        "models": models,
        "actions": actions,
        "total_download_bytes": total_download_bytes,
        "status": "planned",
    }
