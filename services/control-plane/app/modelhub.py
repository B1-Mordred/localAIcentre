from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .catalog import CatalogError, ModelCatalog

ACCEPTED_LICENSE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}@[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")


def manifest_is_downloadable(record: dict[str, Any]) -> bool:
    return bool(record.get("downloadable"))


def parse_accepted_license_refs(header_value: str | None) -> set[str]:
    if not header_value:
        return set()
    refs: set[str] = set()
    for raw_ref in header_value.split(","):
        ref = raw_ref.strip()
        if not ref:
            continue
        if len(ref) > 256 or not ACCEPTED_LICENSE_REF_PATTERN.match(ref):
            raise CatalogError(f"invalid accepted licence reference: {ref}")
        refs.add(ref)
    return refs


def model_ref_for_record(record: dict[str, Any]) -> str | None:
    model_id = record.get("id")
    version = record.get("version")
    if isinstance(model_id, str) and model_id and isinstance(version, str) and version:
        return f"{model_id}@{version}"
    return None


def record_requires_license_acceptance(record: dict[str, Any]) -> bool:
    license_info = record.get("license") if isinstance(record.get("license"), dict) else {}
    return bool(record.get("requires_license_acceptance") or license_info.get("acceptance_required"))


def record_license_acceptance_satisfied(record: dict[str, Any], accepted_refs: set[str]) -> bool:
    if not record_requires_license_acceptance(record):
        return True
    model_ref = model_ref_for_record(record)
    return bool(model_ref and model_ref in accepted_refs)


def redacted_source_metadata(source: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    metadata = {key: value for key, value in source.items() if key != "url"}
    url = source.get("url")
    if isinstance(url, str) and url:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            netloc = hostname
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except ValueError:
            metadata["url"] = ""
            metadata["url_redacted"] = True
            return metadata
        metadata["url"] = safe_url
        metadata["url_redacted"] = safe_url != url
    return metadata


def public_modelhub_metadata(value: Any) -> Any:
    if isinstance(value, list):
        return [public_modelhub_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    public: dict[str, Any] = {}
    for key, item in value.items():
        if key == "source" and isinstance(item, dict):
            public[key] = redacted_source_metadata(item)
        else:
            public[key] = public_modelhub_metadata(item)
    return public


def sync_plan_model_metadata(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    resolved = record.get("resolved_model") if isinstance(record.get("resolved_model"), dict) else {}
    license_info = dict(record.get("license") or {})
    model_id = record.get("id") or resolved.get("id") or record.get("root")
    version = record.get("version") or resolved.get("version")
    display_name = record.get("display_name") or resolved.get("display_name") or model_id
    return _without_none(
        {
            "id": model_id,
            "version": version,
            "display_name": display_name,
            "modality": record.get("modality"),
            "operations": list(record.get("operations") or []),
            "preferred_runtime": record.get("preferred_runtime"),
            "source": redacted_source_metadata(record.get("source")),
            "license": license_info,
            "execution_modes": list(record.get("execution_modes") or []),
            "resource_estimate": record.get("resource_estimate") or {},
            "resource_label": record.get("resource_label"),
            "downloadable": manifest_is_downloadable(record),
            "requires_license_acceptance": bool(license_info.get("acceptance_required")),
            "aliases": list(record.get("aliases") or []),
        }
    )


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def sync_plan_action_metadata(record: dict[str, Any] | None) -> dict[str, Any]:
    metadata = sync_plan_model_metadata(record)
    return {
        "model_metadata": metadata,
        "license": metadata.get("license", {}),
        "source": metadata.get("source", {}),
        "resource_estimate": metadata.get("resource_estimate", {}),
        "requires_license_acceptance": bool(metadata.get("requires_license_acceptance")),
    }


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


def manifest_permission_roles(record: dict[str, Any], action: str) -> set[str]:
    permissions = record.get("permissions") if isinstance(record.get("permissions"), dict) else {}
    permission_key = {
        "read": "visible_to",
        "download": "downloadable_by",
        "install": "installable_by",
        "inference": "inference_roles",
    }.get(action)
    roles: Any = permissions.get(permission_key, []) if permission_key else []
    if action == "read" and not roles:
        roles = record.get("visibility_roles", [])
    if not isinstance(roles, list):
        return set()
    return {role for role in roles if isinstance(role, str) and role}


def role_allowed_by_manifest_permissions(record: dict[str, Any], role: str, action: str) -> bool:
    roles = manifest_permission_roles(record, action)
    return not roles or role in roles


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


def build_sync_plan(
    catalog: ModelCatalog,
    models: list[str],
    installed_blob_sizes: dict[str, int],
    *,
    record_filter: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    total_download_bytes = 0
    for model_id in models:
        record = catalog.model_or_alias_record(model_id)
        if record is None:
            raise CatalogError(f"model not found: {model_id}")
        version_records = catalog.versions_for(model_id)
        allowed_version_records = [item for item in version_records if record_filter is None or record_filter(model_id, item)]
        if record_filter is not None and not allowed_version_records:
            raise CatalogError(f"model not permitted by Model Hub policy: {model_id}")
        versions = [item for item in allowed_version_records if manifest_is_downloadable(item)]
        if not versions:
            actions.append(
                {
                    "model": model_id,
                    "action": "skip",
                    "reason": "model is not downloadable",
                    **sync_plan_action_metadata(allowed_version_records[0] if allowed_version_records else record),
                }
            )
            continue
        version = versions[0]
        model_metadata = sync_plan_action_metadata(version)
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
                    **model_metadata,
                }
            )
    return {
        "models": models,
        "actions": actions,
        "total_download_bytes": total_download_bytes,
        "status": "planned",
    }
