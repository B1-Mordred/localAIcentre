#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import old_stack_backup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.private_files import PrivateFileError, write_private_json  # noqa: E402


PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
INVENTORY_FORMAT = old_stack_backup.INVENTORY_FORMAT
OPEN_WEBUI_PLAN_FORMAT = "b1-ai-hub-open-webui-migration-plan/v1"
BLOCKED_CONTAINER_CLASSIFICATIONS = {"b1-ai-hub-current-preserve", "preserve-unrelated"}
BLOCKED_SERVICE_CLASSIFICATIONS = {"b1-ai-hub-current-preserve", "preserve-unrelated"}
PRODUCTION_HOSTS = (
    "ai.b1.germering",
    "control.ai.b1.germering",
    "media.ai.b1.germering",
    "comfy.ai.b1.germering",
    "voice.ai.b1.germering",
    "models.ai.b1.germering",
    "api.ai.b1.germering",
)
OPTIONAL_PRODUCTION_HOSTS = ("monitoring.ai.b1.germering",)
LEGACY_COMFY_PORT = 8188


class CutoverPlanError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CutoverPlanError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CutoverPlanError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CutoverPlanError(f"{path} must contain a JSON object")
    return payload


def load_inventory(path: Path) -> dict[str, Any]:
    inventory = load_json_file(path)
    if inventory.get("format") != INVENTORY_FORMAT:
        raise CutoverPlanError("unsupported inventory format")
    return inventory


def load_open_webui_plan(path: Path) -> dict[str, Any]:
    plan = load_json_file(path)
    if plan.get("format") != OPEN_WEBUI_PLAN_FORMAT:
        raise CutoverPlanError("unsupported Open WebUI migration plan format")
    return plan


def scope_names(scope: dict[str, Any], key: str) -> list[str]:
    names: list[str] = []
    for item in scope.get(key, []):
        if not isinstance(item, dict):
            continue
        value = item.get("name")
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    return names


def scope_paths(scope: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in scope.get("include_paths", []):
        if not isinstance(item, dict):
            continue
        value = item.get("path")
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)
    return paths


def inventory_container_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    classification = inventory.get("classification") if isinstance(inventory.get("classification"), dict) else {}
    containers = classification.get("containers")
    if not isinstance(containers, list):
        return {}
    mapped: dict[str, dict[str, Any]] = {}
    for item in containers:
        if not isinstance(item, dict):
            continue
        name = item.get("container")
        if isinstance(name, str) and name:
            mapped[name.lstrip("/")] = item
            mapped[name] = item
    return mapped


def inventory_systemd_service_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    classification = inventory.get("classification") if isinstance(inventory.get("classification"), dict) else {}
    services = classification.get("systemd_services")
    if not isinstance(services, list):
        return {}
    mapped: dict[str, dict[str, Any]] = {}
    for item in services:
        if not isinstance(item, dict):
            continue
        name = item.get("service")
        if isinstance(name, str) and name:
            mapped[name] = item
    return mapped


def validate_systemd_service_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CutoverPlanError("systemd service name is required")
    if not value.endswith(".service") or not all(char.isalnum() or char in "_.@:-" for char in value):
        raise CutoverPlanError(f"unsafe systemd service name: {value}")
    return value


def validate_scope_against_inventory(scope: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    containers = inventory_container_map(inventory)
    for name in scope_names(scope, "include_containers"):
        item = containers.get(name) or containers.get(name.lstrip("/"))
        if item is None:
            warnings.append(f"container {name} was explicitly scoped but was not present in the inventory")
            continue
        classification = item.get("classification")
        if classification in BLOCKED_CONTAINER_CLASSIFICATIONS:
            raise CutoverPlanError(f"refusing to plan cutover for container {name} classified as {classification}")
        if classification == "unknown-preserve-by-default":
            warnings.append(f"container {name} was scoped despite inventory classification unknown-preserve-by-default")
    services = inventory_systemd_service_map(inventory)
    for name in scope_names(scope, "include_systemd_services"):
        service_name = validate_systemd_service_name(name)
        item = services.get(service_name)
        if item is None:
            warnings.append(f"systemd service {service_name} was explicitly scoped but was not present in the inventory")
            continue
        classification = item.get("classification")
        if classification in BLOCKED_SERVICE_CLASSIFICATIONS:
            raise CutoverPlanError(f"refusing to plan cutover for systemd service {service_name} classified as {classification}")
        if classification == "unknown-preserve-by-default":
            warnings.append(f"systemd service {service_name} was scoped despite inventory classification unknown-preserve-by-default")
    return warnings


def validate_port_number(port: int, name: str) -> int:
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise CutoverPlanError(f"{name} must be a TCP port from 1 to 65535")
    return port


def inventory_listening_tcp(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    host = inventory.get("host") if isinstance(inventory.get("host"), dict) else {}
    entries = host.get("listening_tcp") if isinstance(host.get("listening_tcp"), list) else []
    return [dict(item) for item in entries if isinstance(item, dict)]


def listeners_on_port(inventory: dict[str, Any], port: int) -> list[dict[str, Any]]:
    return [item for item in inventory_listening_tcp(inventory) if item.get("port") == port]


def listener_description(item: dict[str, Any]) -> str:
    address = str(item.get("local_address") or "*")
    port = item.get("port")
    process = str(item.get("process") or "").strip()
    base = f"{address}:{port}"
    return f"{base} {process}".strip()


def listener_descriptions(items: list[dict[str, Any]]) -> list[str]:
    return [listener_description(item) for item in items]


def analyze_cutover_ports(
    inventory: dict[str, Any],
    *,
    temporary_http_port: int,
    temporary_https_port: int,
    production_http_port: int,
    production_https_port: int,
) -> tuple[dict[str, Any], list[str]]:
    ports = {
        "temporary_http": validate_port_number(temporary_http_port, "temporary_http_port"),
        "temporary_https": validate_port_number(temporary_https_port, "temporary_https_port"),
        "production_http": validate_port_number(production_http_port, "production_http_port"),
        "production_https": validate_port_number(production_https_port, "production_https_port"),
        "legacy_comfy": LEGACY_COMFY_PORT,
    }
    if temporary_http_port == temporary_https_port:
        raise CutoverPlanError("temporary HTTP and HTTPS ports must be different")
    if production_http_port == production_https_port:
        raise CutoverPlanError("production HTTP and HTTPS ports must be different")
    if {temporary_http_port, temporary_https_port} & {production_http_port, production_https_port}:
        raise CutoverPlanError("temporary ports must not reuse production ports")

    listeners = {name: listeners_on_port(inventory, port) for name, port in ports.items()}
    occupied_temporary = {
        name: listener_descriptions(items)
        for name, items in listeners.items()
        if name.startswith("temporary_") and items
    }
    if occupied_temporary:
        details = "; ".join(f"{name} port {ports[name]}: {', '.join(items)}" for name, items in sorted(occupied_temporary.items()))
        raise CutoverPlanError(f"temporary B1 staging port is already in use: {details}")

    warnings: list[str] = []
    occupied_production = {
        name: listener_descriptions(items)
        for name, items in listeners.items()
        if name.startswith("production_") and items
    }
    for name, items in sorted(occupied_production.items()):
        warnings.append(
            f"{name} port {ports[name]} is already listening in the inventory; cutover must stop the scoped old stack or change routes before B1 production start: {', '.join(items)}"
        )
    if listeners["legacy_comfy"]:
        warnings.append(
            f"legacy ComfyUI port {LEGACY_COMFY_PORT} is already listening; keep the B1 legacy listener disabled or resolve the conflict before enabling compose.legacy-comfy.yaml: {', '.join(listener_descriptions(listeners['legacy_comfy']))}"
        )

    return (
        {
            "temporary_ports_clear": True,
            "temporary_ports": {"http": temporary_http_port, "https": temporary_https_port},
            "production_ports": {"http": production_http_port, "https": production_https_port},
            "legacy_comfy_port": LEGACY_COMFY_PORT,
            "listeners": {name: listener_descriptions(items) for name, items in listeners.items()},
            "operator_must_resolve_production_conflicts": bool(occupied_production),
            "operator_must_review_legacy_comfy_conflict": bool(listeners["legacy_comfy"]),
        },
        warnings,
    )


def inventory_dns_records(inventory: dict[str, Any]) -> dict[str, list[str]]:
    host = inventory.get("host") if isinstance(inventory.get("host"), dict) else {}
    dns = host.get("dns") if isinstance(host.get("dns"), dict) else {}
    records = dns.get("records") if isinstance(dns.get("records"), dict) else {}
    normalized: dict[str, list[str]] = {}
    for host_name, addresses in records.items():
        if not isinstance(host_name, str) or not host_name:
            continue
        if not isinstance(addresses, list):
            continue
        normalized[host_name] = sorted({str(address) for address in addresses if isinstance(address, str) and address})
    return normalized


def dns_record_descriptions(records: dict[str, list[str]], hosts: list[str]) -> list[str]:
    return [f"{host}={','.join(records.get(host, [])) or '<missing>'}" for host in hosts]


def analyze_dns_readiness(
    inventory: dict[str, Any],
    production_hosts: tuple[str, ...] = PRODUCTION_HOSTS,
    optional_hosts: tuple[str, ...] = OPTIONAL_PRODUCTION_HOSTS,
) -> tuple[dict[str, Any], list[str]]:
    records = inventory_dns_records(inventory)
    hosts = list(production_hosts)
    optional = list(optional_hosts)
    missing_hosts = [host for host in hosts if not records.get(host)]
    resolved_hosts = [host for host in hosts if records.get(host)]
    common_addresses: list[str] = []
    if resolved_hosts:
        common = set(records[resolved_hosts[0]])
        for host in resolved_hosts[1:]:
            common &= set(records[host])
        common_addresses = sorted(common)

    reference_host = hosts[0] if hosts else ""
    reference_addresses = records.get(reference_host, [])
    divergent_hosts = [
        host
        for host in resolved_hosts
        if reference_addresses and set(records.get(host, [])) != set(reference_addresses)
    ]
    optional_missing_hosts = [host for host in optional if not records.get(host)]
    optional_resolved_hosts = [host for host in optional if records.get(host)]
    optional_divergent_hosts = [
        host
        for host in optional_resolved_hosts
        if reference_addresses and set(records.get(host, [])) != set(reference_addresses)
    ]
    all_hosts_resolve = not missing_hosts
    all_hosts_share_gateway_address = all_hosts_resolve and bool(common_addresses)
    operator_must_review = bool(missing_hosts or divergent_hosts or optional_divergent_hosts or not all_hosts_share_gateway_address)

    warnings: list[str] = []
    if missing_hosts:
        warnings.append(
            "DNS inventory did not resolve every B1 virtual host; cutover must not assume production hostnames are ready: "
            + ", ".join(missing_hosts)
        )
    if all_hosts_resolve and not common_addresses:
        warnings.append(
            "DNS inventory records for B1 virtual hosts do not share a common gateway address; verify LAN DNS before production cutover: "
            + "; ".join(dns_record_descriptions(records, hosts))
        )
    if divergent_hosts:
        warnings.append(
            f"DNS inventory records differ from {reference_host}; verify all B1 virtual hosts terminate at the Caddy gateway: "
            + "; ".join(dns_record_descriptions(records, [reference_host, *divergent_hosts]))
        )
    if optional_divergent_hosts:
        warnings.append(
            f"Optional DNS inventory records differ from {reference_host}; verify optional profile hosts terminate at the Caddy gateway before enabling them: "
            + "; ".join(dns_record_descriptions(records, [reference_host, *optional_divergent_hosts]))
        )

    return (
        {
            "all_hosts_resolve": all_hosts_resolve,
            "all_hosts_share_gateway_address": all_hosts_share_gateway_address,
            "operator_must_review_dns": operator_must_review,
            "intended_hosts": hosts,
            "core_hosts": hosts,
            "optional_hosts": optional,
            "records": {host: records.get(host, []) for host in hosts},
            "optional_records": {host: records.get(host, []) for host in optional},
            "missing_hosts": missing_hosts,
            "optional_missing_hosts": optional_missing_hosts,
            "common_addresses": common_addresses,
            "reference_host": reference_host,
            "reference_addresses": reference_addresses,
            "divergent_hosts": divergent_hosts,
            "optional_divergent_hosts": optional_divergent_hosts,
        },
        warnings,
    )


def analyze_target_identity_readiness(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    target_identity = readiness.get("target_identity") if isinstance(readiness.get("target_identity"), dict) else {}
    host = inventory.get("host") if isinstance(inventory.get("host"), dict) else {}
    if not target_identity:
        target_identity = host.get("target_identity") if isinstance(host.get("target_identity"), dict) else {}
    if not target_identity:
        identity = host.get("identity") if isinstance(host.get("identity"), dict) else {}
        target_identity = {
            "available": False,
            "accepted": False,
            "hostname_authority": "b1-appliance-config",
            "expected_target_host": PRODUCTION_HOSTS[0],
            "observed_hostname": identity.get("hostname"),
            "observed_fqdn": identity.get("fqdn"),
            "observed_platform_node": identity.get("platform_node"),
            "operator_must_review_target_identity": True,
            "warnings": ["target host identity readiness was not present in inventory"],
        }
        return target_identity, ["Target host identity readiness was not present in inventory; rerun inventory before cutover"]

    warnings = [str(item) for item in target_identity.get("warnings", []) if isinstance(item, str)]
    if target_identity.get("hostname_authority") != "b1-appliance-config":
        warnings.append("inventory target hostname authority is not the B1 appliance configuration")
    if target_identity.get("accepted") is not True and not warnings:
        warnings.append("inventory host identity does not match the expected target host")
    if target_identity.get("operator_must_review_target_identity") is True and not warnings:
        warnings.append("inventory target host identity is marked for operator review")
    if not any(
        target_identity.get(key) is True
        for key in ("hostname_matches_expected", "fqdn_matches_expected", "platform_node_matches_expected")
    ) and not warnings:
        warnings.append("inventory host identity does not match the expected target host")
    cutover_warnings = [f"Target host identity requires operator review before cutover: {warning}" for warning in warnings]
    return {
        **target_identity,
        "available": True,
        "operator_must_review_target_identity": bool(cutover_warnings),
        "warnings": warnings,
    }, cutover_warnings


def analyze_networking_readiness(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    networking = readiness.get("networking") if isinstance(readiness.get("networking"), dict) else {}
    if not networking:
        host = inventory.get("host") if isinstance(inventory.get("host"), dict) else {}
        network = host.get("network") if isinstance(host.get("network"), dict) else {}
        networking = network.get("dhcp_policy") if isinstance(network.get("dhcp_policy"), dict) else {}
    if not networking:
        return (
            {
                "available": False,
                "hostname_authority": "b1-appliance-config",
                "hostname_source": "system-hostname",
                "network_property_source": "host-dhcp-client",
                "b1_manages_host_networking": False,
                "b1_static_ip_configures": False,
                "operator_must_review_networking": True,
                "warnings": ["host DHCP/networking readiness was not present in inventory"],
            },
            ["Host networking readiness was not present in inventory; rerun inventory before cutover"],
        )

    warnings = [str(item) for item in networking.get("warnings", []) if isinstance(item, str)]
    if networking.get("hostname_authority") != "b1-appliance-config":
        warnings.append("target hostname policy must be defined by B1 appliance configuration, not DHCP")
    if networking.get("b1_static_ip_configures") is not False:
        warnings.append("B1 networking policy must not configure a static host IP address")
    if networking.get("hostname_source") != "system-hostname":
        warnings.append("observed system hostname must match the expected target hostname")
    if networking.get("network_property_source") != "host-dhcp-client":
        warnings.append("B1 network properties must be acquired by the host DHCP client")
    try:
        default_route_address_count = int(str(networking.get("default_route_address_count") or "0"))
    except ValueError:
        default_route_address_count = 0
    if default_route_address_count <= 0:
        warnings.append("Default-route interface address evidence is missing from the inventory")
    if networking.get("has_dhcp_default_route") is not True and not any("DHCP" in warning for warning in warnings):
        warnings.append("DHCP default-route evidence is missing from the inventory")
    cutover_warnings = [f"Host DHCP/networking requires operator review before cutover: {warning}" for warning in warnings]
    return {**networking, "available": True, "operator_must_review_networking": bool(cutover_warnings), "warnings": warnings}, cutover_warnings


def same_resolved_path(left: str | None, right: Path) -> bool:
    if not isinstance(left, str) or not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def analyze_open_webui_preservation(
    *,
    open_webui_plan_path: Path | None,
    inventory_path: Path,
    backup_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    if open_webui_plan_path is None:
        return (
            {
                "plan_supplied": False,
                "operator_must_review_open_webui": True,
                "recommended_strategy": "generate-open-webui-migration-plan-before-cutover",
            },
            [
                "Open WebUI preservation plan was not supplied; cutover must not assume accounts, chats, files, or settings are preserved"
            ],
        )

    plan = load_open_webui_plan(open_webui_plan_path)
    inputs = plan.get("inputs") if isinstance(plan.get("inputs"), dict) else {}
    if not same_resolved_path(inputs.get("inventory"), inventory_path):
        raise CutoverPlanError("Open WebUI preservation plan was generated from a different inventory")
    if not same_resolved_path(inputs.get("old_stack_backup"), backup_dir):
        raise CutoverPlanError("Open WebUI preservation plan was generated from a different old-stack backup")

    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    if safety.get("does_not_import_automatically") is not True or safety.get("old_stack_deletion_allowed") is not False:
        raise CutoverPlanError("Open WebUI preservation plan does not carry the required non-destructive safety invariants")

    open_webui = plan.get("open_webui") if isinstance(plan.get("open_webui"), dict) else {}
    version_evidence = open_webui.get("version_evidence") if isinstance(open_webui.get("version_evidence"), dict) else {}
    source_containers = version_evidence.get("source_containers") if isinstance(version_evidence.get("source_containers"), list) else []
    backed_container_metadata = (
        version_evidence.get("backed_up_container_metadata")
        if isinstance(version_evidence.get("backed_up_container_metadata"), list)
        else []
    )
    plan_warnings = [str(item) for item in plan.get("warnings", []) if isinstance(item, str)]
    strategy = str(open_webui.get("recommended_strategy") or "unknown")
    data_domains = open_webui.get("data_domains") if isinstance(open_webui.get("data_domains"), dict) else {}
    cutover_warnings = [f"Open WebUI preservation plan warning: {warning}" for warning in plan_warnings]
    if strategy != "preserve-backed-up-sqlite-and-test-supported-open-webui-import":
        cutover_warnings.append(f"Open WebUI preservation strategy requires operator review before cutover: {strategy}")

    return (
        {
            "plan_supplied": True,
            "plan": str(open_webui_plan_path.resolve()),
            "created_at": plan.get("created_at"),
            "operator_must_review_open_webui": bool(cutover_warnings),
            "recommended_strategy": strategy,
            "readable_database_count": open_webui.get("readable_database_count", 0),
            "backed_up_database_candidate_count": open_webui.get("backed_up_database_candidate_count", 0),
            "data_domains": data_domains,
            "backup_database_artifact_count": len(open_webui.get("backup_database_artifacts", [])) if isinstance(open_webui.get("backup_database_artifacts"), list) else 0,
            "compatibility_status": version_evidence.get("compatibility_status") or "unknown",
            "source_version_evidence_count": len(source_containers),
            "backed_up_container_metadata_count": len(backed_container_metadata),
            "direct_database_reuse_approved_by_plan": bool(version_evidence.get("direct_database_reuse_approved_by_plan")),
            "requires_temporary_instance_validation": version_evidence.get("requires_temporary_instance_validation") is not False,
            "plan_warnings": plan_warnings,
        },
        cutover_warnings,
    )


def analyze_hardware_readiness(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    hardware = readiness.get("hardware_profile") if isinstance(readiness.get("hardware_profile"), dict) else {}
    if not hardware:
        return {"available": False, "accepted": False, "warnings": ["hardware profile readiness was not present in inventory"]}, [
            "Hardware profile readiness was not present in inventory; rerun inventory before cutover"
        ]
    warnings = [str(item) for item in hardware.get("warnings", []) if isinstance(item, str)]
    if hardware.get("accepted") is not True and not warnings:
        warnings.append("hardware profile did not satisfy the initial B1 AI Hub resource baseline")
    cutover_warnings = [f"Hardware profile requires operator review before cutover: {warning}" for warning in warnings]
    return {**hardware, "available": True, "operator_must_review_hardware": bool(cutover_warnings)}, cutover_warnings


def analyze_gpu_runtime_readiness(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    gpu_runtime = readiness.get("gpu_container_runtime") if isinstance(readiness.get("gpu_container_runtime"), dict) else {}
    if not gpu_runtime:
        docker = inventory.get("docker") if isinstance(inventory.get("docker"), dict) else {}
        host = inventory.get("host") if isinstance(inventory.get("host"), dict) else {}
        docker_info = docker.get("info") if isinstance(docker.get("info"), dict) else {}
        gpu = host.get("gpu") if isinstance(host.get("gpu"), dict) else {}
        toolkit = host.get("nvidia_container_toolkit") if isinstance(host.get("nvidia_container_toolkit"), dict) else {}
        if docker_info or gpu or toolkit:
            gpu_runtime = {
                "available": True,
                "accepted": bool(
                    gpu.get("nvidia_smi_available") is True
                    and gpu.get("devices")
                    and docker_info.get("nvidia_runtime_available") is True
                    and toolkit.get("available") is True
                    and toolkit.get("returncode") == 0
                ),
                "nvidia_smi_available": gpu.get("nvidia_smi_available"),
                "detected_gpu_count": len(gpu.get("devices") or []) if isinstance(gpu.get("devices"), list) else 0,
                "docker_nvidia_runtime_available": docker_info.get("nvidia_runtime_available"),
                "nvidia_container_toolkit_available": toolkit.get("available") is True and toolkit.get("returncode") == 0,
                "nvidia_container_toolkit_returncode": toolkit.get("returncode"),
                "nvidia_container_toolkit_version": toolkit.get("version"),
                "warnings": [],
            }
    if not gpu_runtime:
        return (
            {
                "available": False,
                "accepted": False,
                "warnings": ["GPU container runtime readiness was not present in inventory"],
                "operator_must_review_gpu_runtime": True,
            },
            ["GPU container runtime readiness was not present in inventory; rerun inventory before cutover"],
        )
    warnings = [str(item) for item in gpu_runtime.get("warnings", []) if isinstance(item, str)]
    if gpu_runtime.get("accepted") is not True and not warnings:
        warnings.append("GPU container runtime did not satisfy NVIDIA driver, Docker runtime, and toolkit readiness")
    cutover_warnings = [f"GPU container runtime requires operator review before cutover: {warning}" for warning in warnings]
    return {**gpu_runtime, "available": True, "operator_must_review_gpu_runtime": bool(cutover_warnings), "warnings": warnings}, cutover_warnings


def analyze_runtime_agent_socket_readiness(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    socket = readiness.get("runtime_agent_docker_socket") if isinstance(readiness.get("runtime_agent_docker_socket"), dict) else {}
    if not socket:
        docker = inventory.get("docker") if isinstance(inventory.get("docker"), dict) else {}
        socket = docker.get("socket") if isinstance(docker.get("socket"), dict) else {}
    if not socket:
        return (
            {
                "available": False,
                "runtime_agent_group_access_ready": False,
                "warnings": ["runtime-agent Docker socket readiness was not present in inventory"],
                "operator_must_review_runtime_agent_socket": True,
            },
            ["Runtime-agent Docker socket readiness was not present in inventory; rerun inventory before cutover"],
        )
    warnings = [str(item) for item in socket.get("warnings", []) if isinstance(item, str)]
    ready = socket.get("runtime_agent_group_access_ready") is True
    if not ready and not warnings:
        warnings.append("runtime-agent Docker socket group access is not ready")
    cutover_warnings = [f"Runtime-agent Docker socket requires operator review before cutover: {warning}" for warning in warnings]
    return {**socket, "available": True, "operator_must_review_runtime_agent_socket": bool(cutover_warnings), "warnings": warnings}, cutover_warnings


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def env_command(env: dict[str, str], command: list[str]) -> dict[str, Any]:
    argv = [f"{key}={value}" for key, value in sorted(env.items())] + command
    return {"argv": argv, "shell": shell_join(argv)}


def docker_command(action: str, containers: list[str]) -> dict[str, Any] | None:
    if not containers:
        return None
    argv = ["docker", action, *containers]
    return {"argv": argv, "shell": shell_join(argv)}


def systemctl_command(action: str, services: list[str]) -> dict[str, Any] | None:
    if not services:
        return None
    safe_services = [validate_systemd_service_name(service) for service in services]
    argv = ["systemctl", action, *safe_services]
    return {"argv": argv, "shell": shell_join(argv)}


def validation_commands(temporary_https_port: int) -> list[dict[str, Any]]:
    base_url = f"https://127.0.0.1:{temporary_https_port}"
    return [
        {"name": "gateway health", **env_command({}, ["curl", "-kfsS", f"{base_url}/healthz"])},
        {"name": "control plane health", **env_command({}, ["curl", "-kfsS", "-H", "Host: api.ai.b1.germering", f"{base_url}/healthz"])},
        {"name": "model catalog", **env_command({}, ["curl", "-kfsS", "-H", "Host: api.ai.b1.germering", f"{base_url}/v1/models"])},
        {
            "name": "native ComfyUI metadata through gateway",
            **env_command({}, ["curl", "-kfsS", "-H", "Host: comfy.ai.b1.germering", f"{base_url}/object_info"]),
        },
    ]


def build_plan(
    *,
    inventory_path: Path,
    scope_path: Path,
    backup_dir: Path,
    b1_data_root: str,
    project_name: str,
    temporary_http_port: int,
    temporary_https_port: int,
    production_http_port: int,
    production_https_port: int,
    open_webui_plan_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    inventory = load_inventory(inventory_path)
    scope = old_stack_backup.load_scope(scope_path)
    backup_verification = old_stack_backup.verify_backup(backup_dir)
    warnings = validate_scope_against_inventory(scope, inventory)
    port_readiness, port_warnings = analyze_cutover_ports(
        inventory,
        temporary_http_port=temporary_http_port,
        temporary_https_port=temporary_https_port,
        production_http_port=production_http_port,
        production_https_port=production_https_port,
    )
    warnings.extend(port_warnings)
    dns_readiness, dns_warnings = analyze_dns_readiness(inventory)
    warnings.extend(dns_warnings)
    target_identity_readiness, target_identity_warnings = analyze_target_identity_readiness(inventory)
    warnings.extend(target_identity_warnings)
    networking_readiness, networking_warnings = analyze_networking_readiness(inventory)
    warnings.extend(networking_warnings)
    hardware_readiness, hardware_warnings = analyze_hardware_readiness(inventory)
    warnings.extend(hardware_warnings)
    gpu_runtime_readiness, gpu_runtime_warnings = analyze_gpu_runtime_readiness(inventory)
    warnings.extend(gpu_runtime_warnings)
    runtime_agent_socket_readiness, runtime_agent_socket_warnings = analyze_runtime_agent_socket_readiness(inventory)
    warnings.extend(runtime_agent_socket_warnings)
    open_webui_preservation, open_webui_warnings = analyze_open_webui_preservation(
        open_webui_plan_path=open_webui_plan_path,
        inventory_path=inventory_path,
        backup_dir=backup_dir,
    )
    warnings.extend(open_webui_warnings)
    old_containers = scope_names(scope, "include_containers")
    old_systemd_services = scope_names(scope, "include_systemd_services")
    old_volumes = scope_names(scope, "include_docker_volumes")
    old_paths = scope_paths(scope)
    created_at = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    temp_env = {
        "B1_DATA_ROOT": b1_data_root,
        "B1_PROJECT_NAME": f"{project_name}-staging",
        "B1_HTTP_PORT": str(temporary_http_port),
        "B1_HTTPS_PORT": str(temporary_https_port),
    }
    prod_env = {
        "B1_DATA_ROOT": b1_data_root,
        "B1_PROJECT_NAME": project_name,
        "B1_HTTP_PORT": str(production_http_port),
        "B1_HTTPS_PORT": str(production_https_port),
    }
    stop_command = docker_command("stop", old_containers)
    start_command = docker_command("start", old_containers)
    systemd_stop_command = systemctl_command("stop", old_systemd_services)
    systemd_start_command = systemctl_command("start", old_systemd_services)
    return {
        "format": PLAN_FORMAT,
        "created_at": created_at,
        "inputs": {
            "inventory": str(inventory_path.resolve()),
            "scope": str(scope_path.resolve()),
            "old_stack_backup": str(backup_dir.resolve()),
            "old_stack_backup_verification": backup_verification,
            "open_webui_migration_plan": str(open_webui_plan_path.resolve()) if open_webui_plan_path else None,
        },
        "safety": {
            "read_only_plan": True,
            "stops_nothing_automatically": True,
            "deletes_nothing": True,
            "old_stack_deletion_allowed": False,
            "operator_must_review_before_execution": True,
            "unknown_resources_preserved_by_default": True,
        },
        "old_stack_scope": {
            "reviewed_by": scope.get("reviewed_by"),
            "review_notes": scope.get("review_notes"),
            "containers_to_stop_during_cutover": old_containers,
            "containers_to_restart_for_rollback": old_containers,
            "systemd_services_to_stop_during_cutover": old_systemd_services,
            "systemd_services_to_restart_for_rollback": old_systemd_services,
            "docker_volumes_preserved": old_volumes,
            "host_paths_preserved": old_paths,
        },
        "b1_ai_hub": {
            "data_root": b1_data_root,
            "staging_project_name": temp_env["B1_PROJECT_NAME"],
            "production_project_name": project_name,
            "temporary_ports": {"http": temporary_http_port, "https": temporary_https_port},
            "production_ports": {"http": production_http_port, "https": production_https_port},
            "production_hosts": list(PRODUCTION_HOSTS),
            "optional_hosts": list(OPTIONAL_PRODUCTION_HOSTS),
            "expected_target_host": target_identity_readiness.get("expected_target_host"),
        },
        "port_readiness": port_readiness,
        "target_identity_readiness": target_identity_readiness,
        "hardware_readiness": hardware_readiness,
        "gpu_runtime_readiness": gpu_runtime_readiness,
        "runtime_agent_socket_readiness": runtime_agent_socket_readiness,
        "dns_readiness": dns_readiness,
        "networking_readiness": networking_readiness,
        "open_webui_preservation": open_webui_preservation,
        "warnings": warnings,
        "phases": [
            {
                "name": "preflight",
                "operator_actions": [
                    "Confirm the inventory is current and the old-stack scope includes only old AI resources.",
                    "Confirm no unrelated Hermes, Yggdrasil, Discord, DNS, database, or automation services are scoped.",
                    "Confirm target_identity_readiness proves the inventory came from the intended appliance host.",
                    "Confirm temporary B1 staging ports are free and production port listeners are expected old-stack routes or reverse proxies.",
                    "Confirm hardware_readiness satisfies the initial 12 GB VRAM / 32 GB RAM profile or document a reduced-resource plan before cutover.",
                    "Confirm gpu_runtime_readiness proves nvidia-smi, Docker's nvidia runtime, and NVIDIA Container Toolkit are healthy.",
                    "Confirm runtime_agent_socket_readiness shows B1_DOCKER_GID matches the Docker socket GID so runtime-agent can inspect and recover managed runtimes.",
                    "Confirm dns_readiness shows the intended B1 virtual hosts resolving to the expected LAN gateway address or record the required DNS changes.",
                    "Confirm networking_readiness shows the B1-defined hostname is present as the system hostname and host IP/gateway/route/resolver properties are acquired by DHCP or an operator-reviewed DHCP reservation.",
                    "Confirm open_webui_preservation has been reviewed and the temporary B1 instance will validate the chosen preservation/import path.",
                    "Confirm the verified old-stack backup is stored outside the old stack and is restorable.",
                    "Enable B1 maintenance mode before staging, cutover, rollback, or DNS route changes.",
                ],
            },
            {
                "name": "temporary-b1-start",
                "commands": [
                    {"name": "bootstrap temporary B1 root", **env_command({"B1_DATA_ROOT": b1_data_root}, ["make", "bootstrap"])},
                    {"name": "start B1 on temporary ports", **env_command(temp_env, ["docker", "compose", "-p", temp_env["B1_PROJECT_NAME"], "up", "-d"])},
                ],
            },
            {
                "name": "temporary-validation",
                "commands": validation_commands(temporary_https_port),
                "operator_actions": [
                    "Validate first-admin setup, preserved Open WebUI data or export/import guidance, chat/API, TTS/STT, image, video, native ComfyUI, Model Hub, external ComfyUI nodes, and Voicebox paths appropriate to installed models.",
                    "Verify the old GPU stack is not running GPU inference concurrently with B1 validation.",
                ],
            },
            {
                "name": "cutover-window",
                "commands": [command for command in (stop_command, systemd_stop_command) if command],
                "operator_actions": [
                    "Stop only the explicitly scoped old-stack containers and systemd services listed in this plan.",
                    "Resolve target_identity_readiness warnings before treating the inventory as target-host cutover evidence.",
                    "Resolve any production port listener warnings from port_readiness before starting the production Compose project.",
                    "Resolve dns_readiness warnings before switching users or external clients to the B1 virtual hosts.",
                    "Resolve networking_readiness warnings before treating the inventory as target-host cutover evidence.",
                    "Resolve open_webui_preservation warnings before switching ordinary users to the production Open WebUI hostname.",
                    "Activate production DNS, reverse-proxy routes, or port bindings for the B1 virtual hosts.",
                    "Start or update the B1 production Compose project on the production ports.",
                ],
                "production_start": env_command(prod_env, ["docker", "compose", "-p", project_name, "up", "-d"]),
            },
            {
                "name": "post-cutover-validation",
                "operator_actions": [
                    "Run the Control Center system self-test.",
                    "Verify accounts/chats, unified API, media jobs, ComfyUI REST/WebSocket compatibility, Model Hub downloads, and external integrations.",
                    "Keep old stack volumes, databases, model files, and Compose files stopped/read-only for rollback.",
                ],
            },
            {
                "name": "rollback",
                "commands": [command for command in (systemd_start_command, start_command) if command],
                "operator_actions": [
                    "Revert DNS, reverse-proxy routes, or port bindings to the old stack.",
                    "Restart only the old-stack containers and systemd services explicitly scoped in this plan.",
                    "Leave B1 data and old-stack backups intact for diagnosis.",
                ],
            },
        ],
    }


def write_plan(plan: dict[str, Any], output: Path) -> Path:
    try:
        return write_private_json(output, plan, mode=0o600, label="cutover plan")
    except PrivateFileError as exc:
        raise CutoverPlanError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a non-destructive B1 AI Hub migration cutover and rollback plan.")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--open-webui-plan", default=None)
    parser.add_argument("--output", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + f"/backups/cutover-plan-{utc_stamp()}.json")
    parser.add_argument("--b1-root", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub"))
    parser.add_argument("--project-name", default=os.getenv("B1_PROJECT_NAME", "b1-ai-hub"))
    parser.add_argument("--temporary-http-port", type=int, default=18080)
    parser.add_argument("--temporary-https-port", type=int, default=18443)
    parser.add_argument("--production-http-port", type=int, default=80)
    parser.add_argument("--production-https-port", type=int, default=443)
    args = parser.parse_args()
    try:
        plan = build_plan(
            inventory_path=Path(args.inventory),
            scope_path=Path(args.scope),
            backup_dir=Path(args.backup),
            b1_data_root=args.b1_root,
            project_name=args.project_name,
            temporary_http_port=args.temporary_http_port,
            temporary_https_port=args.temporary_https_port,
            production_http_port=args.production_http_port,
            production_https_port=args.production_https_port,
            open_webui_plan_path=Path(args.open_webui_plan) if args.open_webui_plan else None,
        )
        output = write_plan(plan, Path(args.output))
    except (CutoverPlanError, old_stack_backup.OldStackBackupError) as exc:
        raise SystemExit(f"cutover plan failed: {exc}") from exc
    print(f"wrote cutover plan: {output}")
    print(json.dumps({"format": plan["format"], "warnings": plan["warnings"], "containers_to_stop": plan["old_stack_scope"]["containers_to_stop_during_cutover"]}, indent=2))


if __name__ == "__main__":
    main()
