from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

VALID_RUNTIME_DEPLOYMENT_MODES = {"development", "production"}
COMPOSE_SELECTION_FORMAT = "b1-ai-hub-compose-selection/v1"
B1_PLACEHOLDER_LABEL = "b1.ai-hub.placeholder"
B1_RUNTIME_KIND_LABEL = "b1.ai-hub.runtime.kind"
TRUE_LABEL_VALUES = {"1", "true", "yes", "on"}
FALSE_LABEL_VALUES = {"0", "false", "no", "off"}
INACTIVE_CONTAINER_STATES = {"dead", "exited", "removing"}
REQUIRED_GATEWAY_SECURITY_HEADERS = {
    "strict-transport-security": ("max-age=", "includesubdomains"),
    "x-content-type-options": ("nosniff",),
    "x-frame-options": ("deny",),
    "referrer-policy": ("no-referrer",),
}
SAFE_PERMISSIONS_POLICY_DIRECTIVES = {
    "camera": ("camera=()", "camera=(self)"),
    "microphone": ("microphone=()", "microphone=(self)"),
    "geolocation": ("geolocation=()",),
}
MIB_PER_GIB = 1024
BYTES_PER_GIB = 1024**3


def check(name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "data": data or {}}


def summarize(checks: list[dict[str, Any]]) -> str:
    statuses = {item["status"] for item in checks}
    if "failed" in statuses:
        return "failed"
    if statuses & {"degraded", "warning"}:
        return "degraded"
    return "ok"


def check_directory_exists(name: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return check(name, "failed", f"{path} does not exist")
    if not path.is_dir():
        return check(name, "failed", f"{path} is not a directory")
    return check(name, "ok", f"{path} exists", {"path": str(path)})


def check_writable_directory(name: str, path: Path) -> dict[str, Any]:
    exists = check_directory_exists(name, path)
    if exists["status"] != "ok":
        return exists
    probe = path / f".b1-self-test-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("b1-self-test\n", encoding="utf-8")
        content = probe.read_text(encoding="utf-8")
        if content != "b1-self-test\n":
            return check(name, "failed", f"{path} write/read probe returned unexpected content", {"path": str(path)})
    except OSError as exc:
        return check(name, "failed", f"{path} is not writable: {exc.__class__.__name__}", {"path": str(path)})
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return check(name, "ok", f"{path} is writable", {"path": str(path)})


def check_http_result(name: str, payload: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    if error:
        return check(name, "degraded", error)
    if payload is None:
        return check(name, "degraded", "no response payload")
    return check(name, "ok", "response received", payload)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _largest_gpu_device(gpu: dict[str, Any]) -> dict[str, Any] | None:
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    candidates = [device for device in devices if isinstance(device, dict) and _number(device.get("memory_total_mib")) is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda item: float(item.get("memory_total_mib") or 0))


def hardware_resource_policy_check(
    agent_metrics: dict[str, Any] | None,
    resource_policy: dict[str, Any],
    deployment_mode: str,
) -> dict[str, Any]:
    name = "hardware:resource-policy"
    mode = deployment_mode.strip().lower()
    failure_status = "failed" if mode == "production" else "warning"
    policy_gpu_total = _number(resource_policy.get("gpu_total_vram_gib")) or 0.0
    policy_gpu_reserve = _number(resource_policy.get("gpu_reserve_vram_gib")) or 0.0
    policy_host_total = _number(resource_policy.get("host_total_ram_gib")) or 0.0
    policy_host_reserve = _number(resource_policy.get("host_reserve_ram_gib")) or 0.0
    required_gpu_total_mib = int(policy_gpu_total * MIB_PER_GIB)
    required_gpu_reserve_mib = int(policy_gpu_reserve * MIB_PER_GIB)
    required_host_total_bytes = int(policy_host_total * BYTES_PER_GIB)
    required_host_reserve_bytes = int(policy_host_reserve * BYTES_PER_GIB)

    data: dict[str, Any] = {
        "deployment_mode": mode,
        "policy": {
            "gpu_total_vram_gib": policy_gpu_total,
            "gpu_reserve_vram_gib": policy_gpu_reserve,
            "host_total_ram_gib": policy_host_total,
            "host_reserve_ram_gib": policy_host_reserve,
        },
        "observed": {},
        "warnings": [],
    }

    if not isinstance(agent_metrics, dict):
        data["warnings"].append("runtime-agent metrics are unavailable")
        detail = "runtime-agent metrics are unavailable; cannot verify host/GPU policy"
        if mode != "production":
            detail += "; development mode permits bootstrapping only"
        return check(name, failure_status, detail, data)

    gpu = agent_metrics.get("gpu") if isinstance(agent_metrics.get("gpu"), dict) else {}
    memory = agent_metrics.get("memory") if isinstance(agent_metrics.get("memory"), dict) else {}
    largest_gpu = _largest_gpu_device(gpu)
    observed_gpu_total_mib = _number(largest_gpu.get("memory_total_mib")) if largest_gpu else None
    observed_gpu_free_mib = _number(largest_gpu.get("memory_free_mib")) if largest_gpu else None
    observed_host_total_bytes = _number(memory.get("total_bytes"))
    observed_host_available_bytes = _number(memory.get("available_bytes"))
    data["observed"] = {
        "gpu_available": gpu.get("available"),
        "gpu_device_count": len(gpu.get("devices") or []) if isinstance(gpu.get("devices"), list) else 0,
        "largest_gpu_name": largest_gpu.get("name") if largest_gpu else None,
        "largest_gpu_memory_total_mib": int(observed_gpu_total_mib) if observed_gpu_total_mib is not None else None,
        "largest_gpu_memory_free_mib": int(observed_gpu_free_mib) if observed_gpu_free_mib is not None else None,
        "host_memory_total_bytes": int(observed_host_total_bytes) if observed_host_total_bytes is not None else None,
        "host_memory_available_bytes": int(observed_host_available_bytes) if observed_host_available_bytes is not None else None,
    }

    warnings: list[str] = data["warnings"]
    if gpu.get("available") is not True or largest_gpu is None:
        warnings.append("GPU metrics are unavailable")
    elif observed_gpu_total_mib is not None and observed_gpu_total_mib < required_gpu_total_mib:
        warnings.append(
            f"largest GPU VRAM is {int(observed_gpu_total_mib)} MiB; policy requires {required_gpu_total_mib} MiB"
        )
    if largest_gpu is not None and observed_gpu_free_mib is None:
        warnings.append("largest GPU free VRAM is unavailable")
    elif observed_gpu_free_mib is not None and observed_gpu_free_mib < required_gpu_reserve_mib:
        warnings.append(
            f"largest GPU free VRAM is {int(observed_gpu_free_mib)} MiB; policy reserve requires {required_gpu_reserve_mib} MiB"
        )

    if observed_host_total_bytes is None:
        warnings.append("host total RAM is unavailable")
    elif observed_host_total_bytes < required_host_total_bytes:
        observed_mib = int(observed_host_total_bytes / (1024**2))
        required_mib = int(required_host_total_bytes / (1024**2))
        warnings.append(f"host RAM is {observed_mib} MiB; policy requires {required_mib} MiB")
    if observed_host_available_bytes is None:
        warnings.append("host available RAM is unavailable")
    elif observed_host_available_bytes < required_host_reserve_bytes:
        observed_mib = int(observed_host_available_bytes / (1024**2))
        required_mib = int(required_host_reserve_bytes / (1024**2))
        warnings.append(f"host available RAM is {observed_mib} MiB; policy reserve requires {required_mib} MiB")

    if not warnings:
        return check(name, "ok", "observed GPU/RAM satisfy the effective resource policy and reserves", data)

    detail = "; ".join(warnings)
    if mode != "production":
        detail += "; development mode permits bootstrapping only"
    return check(name, failure_status, detail, data)


def compose_selection_check(selection: dict[str, Any] | None, deployment_mode: str) -> dict[str, Any]:
    name = "deployment:compose-selection"
    mode = deployment_mode.strip().lower()
    failure_status = "failed" if mode == "production" else "warning"
    data: dict[str, Any] = {
        "deployment_mode": mode,
        "format": None,
        "status": "unavailable",
        "raw_compose_file": "",
        "raw_compose_profiles": "",
        "selected_file_basenames": [],
        "selected_profiles": [],
        "production_required_runtimes": [],
        "required_files": [],
        "required_profiles": [],
        "missing_files": [],
        "missing_profiles": [],
    }

    if not isinstance(selection, dict):
        detail = "Compose selection snapshot is unavailable"
        if mode != "production":
            detail += "; development mode permits bootstrapping only"
        return check(name, failure_status, detail, data)

    data.update(
        {
            "format": str(selection.get("format") or ""),
            "status": str(selection.get("status") or "unknown"),
            "raw_compose_file": str(selection.get("raw_compose_file") or ""),
            "raw_compose_profiles": str(selection.get("raw_compose_profiles") or ""),
            "selected_file_basenames": _string_list(selection.get("selected_file_basenames")),
            "selected_profiles": _string_list(selection.get("selected_profiles")),
            "production_required_runtimes": _string_list(selection.get("production_required_runtimes")),
            "required_files": _string_list(selection.get("required_files")),
            "required_profiles": _string_list(selection.get("required_profiles")),
            "missing_files": _string_list(selection.get("missing_files")),
            "missing_profiles": _string_list(selection.get("missing_profiles")),
        }
    )

    if data["format"] != COMPOSE_SELECTION_FORMAT:
        detail = f"Compose selection snapshot has unsupported format {data['format'] or 'missing'}"
        return check(name, "failed", detail, data)

    blockers: list[str] = []
    if data["missing_files"]:
        blockers.append("missing Compose files: " + ", ".join(data["missing_files"]))
    if data["missing_profiles"]:
        blockers.append("missing Compose profiles: " + ", ".join(data["missing_profiles"]))
    if data["status"] != "ok" and not blockers:
        blockers.append(f"Compose selection status is {data['status']}")

    if not blockers:
        return check(name, "ok", "required production Compose files and profiles are selected", data)

    detail = "; ".join(blockers)
    if mode != "production":
        detail += "; development mode permits bootstrapping only"
    return check(name, failure_status, detail, data)


def runtime_agent_mutation_guard_check(payload: dict[str, Any] | None) -> dict[str, Any]:
    name = "runtime-agent:mutation-guard"
    if not isinstance(payload, dict):
        return check(name, "degraded", "runtime-agent status payload is unavailable")

    allowed_services = payload.get("allowed_services")
    runtime_action_services = payload.get("runtime_action_services")
    rate_limit = payload.get("mutation_rate_limit_per_minute")
    data = {
        "auth_configured": payload.get("auth_configured"),
        "allow_missing_auth": payload.get("allow_missing_auth"),
        "mtls_enabled": payload.get("mtls_enabled"),
        "client_cert_required": payload.get("client_cert_required"),
        "mutations_enabled": payload.get("mutations_enabled"),
        "mutation_rate_limit_per_minute": rate_limit,
        "allowed_services": allowed_services if isinstance(allowed_services, list) else [],
        "runtime_action_services": runtime_action_services if isinstance(runtime_action_services, list) else [],
    }

    failures: list[str] = []
    if payload.get("auth_configured") is not True:
        failures.append("bearer token is not configured")
    if payload.get("allow_missing_auth") is True:
        failures.append("missing-auth bypass is enabled")
    if payload.get("mtls_enabled") is not True or payload.get("client_cert_required") is not True:
        failures.append("runtime-agent mTLS client certificate enforcement is not active")
    if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or rate_limit <= 0:
        failures.append("mutation rate limit is not a positive integer")
    if not isinstance(allowed_services, list) or not allowed_services:
        failures.append("service allowlist is empty or unavailable")
        allowed_service_set: set[str] = set()
    else:
        allowed_service_set = {str(item).strip() for item in allowed_services if str(item).strip()}
        if len(allowed_service_set) != len(allowed_services) or "*" in allowed_service_set:
            failures.append("service allowlist contains unsafe entries")
    if not isinstance(runtime_action_services, list) or not runtime_action_services:
        failures.append("runtime-action allowlist is empty or unavailable")
    else:
        runtime_action_set = {str(item).strip() for item in runtime_action_services if str(item).strip()}
        if len(runtime_action_set) != len(runtime_action_services) or "*" in runtime_action_set:
            failures.append("runtime-action allowlist contains unsafe entries")
        elif allowed_service_set and not runtime_action_set <= allowed_service_set:
            failures.append("runtime-action allowlist contains services outside the service allowlist")

    if failures:
        return check(name, "failed", "; ".join(failures), data)
    return check(name, "ok", "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited", data)


def gateway_security_header_failures(headers: Mapping[str, str]) -> list[str]:
    normalized = {str(name).lower(): str(value).lower() for name, value in headers.items()}
    failures: list[str] = []
    for header, required_tokens in REQUIRED_GATEWAY_SECURITY_HEADERS.items():
        value = normalized.get(header, "")
        if not value:
            failures.append(f"missing {header}")
            continue
        missing_tokens = [token for token in required_tokens if token not in value]
        if missing_tokens:
            failures.append(f"{header} missing {', '.join(missing_tokens)}")
    permissions_policy = "".join(normalized.get("permissions-policy", "").split())
    if not permissions_policy:
        failures.append("missing permissions-policy")
    else:
        directives: dict[str, set[str]] = {}
        for raw_directive in permissions_policy.split(","):
            directive_name, separator, _directive_value = raw_directive.partition("=")
            if separator:
                directives.setdefault(directive_name, set()).add(raw_directive)
        for directive, allowed_tokens in SAFE_PERMISSIONS_POLICY_DIRECTIVES.items():
            configured = directives.get(directive, set())
            if not configured:
                failures.append(f"permissions-policy missing safe {directive} directive")
            elif not configured <= set(allowed_tokens):
                failures.append(f"permissions-policy has unsafe {directive} directive")
    return failures


def runtime_health_name(item: dict[str, Any]) -> str:
    name = item.get("name")
    return name.strip().lower() if isinstance(name, str) else ""


def runtime_health_is_placeholder(item: dict[str, Any]) -> bool:
    details = item.get("details")
    if not isinstance(details, dict):
        details = {}
    kind = details.get("kind")
    return (
        item.get("placeholder") is True
        or details.get("placeholder") is True
        or details.get("b1_placeholder") is True
        or (isinstance(kind, str) and kind.strip().lower().startswith("placeholder"))
    )


def service_inventory_items(service_inventory: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(service_inventory, dict):
        return []
    services = service_inventory.get("services")
    return [item for item in services if isinstance(item, dict)] if isinstance(services, list) else []


def _label_string(labels: dict[str, Any], key: str) -> str:
    value = labels.get(key)
    return value.strip().lower() if isinstance(value, str) else ""


def _container_state_is_relevant(container: dict[str, Any]) -> bool:
    state = container.get("state")
    if not isinstance(state, str) or not state.strip():
        return True
    return state.strip().lower() not in INACTIVE_CONTAINER_STATES


def _looks_like_placeholder_reference(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return (
        "mock-runtime" in text
        or "placeholder-" in text
        or "-placeholder" in text
        or "/placeholder" in text
        or text.endswith(":placeholder")
    )


def runtime_service_placeholder_reasons(service_inventory: dict[str, Any] | None) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    for service in service_inventory_items(service_inventory):
        name = runtime_health_name(service)
        if not name:
            continue
        containers = service.get("containers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict) or not _container_state_is_relevant(container):
                continue
            labels = container.get("labels") if isinstance(container.get("labels"), dict) else {}
            placeholder_label = _label_string(labels, B1_PLACEHOLDER_LABEL)
            runtime_kind = _label_string(labels, B1_RUNTIME_KIND_LABEL)
            container_reasons: list[str] = []
            if placeholder_label in TRUE_LABEL_VALUES:
                container_reasons.append("container label b1.ai-hub.placeholder=true")
            elif placeholder_label and placeholder_label not in FALSE_LABEL_VALUES:
                container_reasons.append("container placeholder label is invalid")
            if runtime_kind.startswith("placeholder"):
                container_reasons.append(f"container runtime kind is {runtime_kind}")
            if _looks_like_placeholder_reference(container.get("image")):
                container_reasons.append("container image reference looks like a placeholder runtime")
            if _looks_like_placeholder_reference(container.get("name")):
                container_reasons.append("container name looks like a placeholder runtime")
            if container_reasons:
                reasons.setdefault(name, []).extend(container_reasons)
    return {name: sorted(set(items)) for name, items in reasons.items()}


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _service_inventory_by_runtime(service_inventory: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for service in service_inventory_items(service_inventory):
        name = runtime_health_name(service)
        if name:
            services[name] = service
    return services


def normalize_runtime_names(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for name in names:
        value = str(name).strip().lower()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def runtime_production_readiness_rows(
    runtime_health: list[dict[str, Any]],
    required_runtimes: tuple[str, ...] | list[str],
    service_inventory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    required = normalize_runtime_names(required_runtimes)
    required_set = set(required)
    health_by_name = {runtime_health_name(item): item for item in runtime_health if runtime_health_name(item)}
    services_by_name = _service_inventory_by_runtime(service_inventory)
    service_inventory_available = bool(service_inventory_items(service_inventory))
    service_placeholder_reasons = runtime_service_placeholder_reasons(service_inventory)
    runtime_names = sorted(required_set | set(health_by_name) | set(services_by_name))
    rows: list[dict[str, Any]] = []

    for runtime in runtime_names:
        health = health_by_name.get(runtime)
        status = "missing"
        if health is not None:
            raw_status = health.get("status")
            status = raw_status.strip().lower() if isinstance(raw_status, str) and raw_status.strip() else "unknown"

        service = services_by_name.get(runtime)
        containers = service.get("containers") if isinstance(service, dict) and isinstance(service.get("containers"), list) else []
        active_containers = [container for container in containers if isinstance(container, dict) and _container_state_is_relevant(container)]
        service_reasons = service_placeholder_reasons.get(runtime, [])
        health_placeholder = runtime_health_is_placeholder(health) if health is not None else False
        placeholder_reasons = list(service_reasons)
        if health_placeholder:
            placeholder_reasons.append("runtime health payload reports a placeholder runtime")
        placeholder_reasons = sorted(set(placeholder_reasons))

        blockers: list[str] = []
        remediation: list[str] = []
        if runtime in required_set:
            if health is None:
                blockers.append("runtime health payload missing")
                remediation.append("verify the runtime adapter is configured and reachable from the control plane")
            elif status != "ok":
                blockers.append(f"runtime health is {status}")
                remediation.append("inspect runtime logs and use the Control Center recover action after fixing the service")
            if placeholder_reasons:
                blockers.append("runtime is still using placeholder evidence")
                remediation.append("select the pinned production Compose overlay/profile and remove placeholder images or labels")
            if not service_inventory_available:
                blockers.append("runtime-agent service inventory unavailable")
                remediation.append("start runtime-agent with Docker socket access so production readiness can verify service labels and images")
            elif service is None:
                blockers.append("runtime-agent service inventory has no service record")
                remediation.append("include the runtime service in the Compose project selected for production mode")
            elif not active_containers:
                blockers.append("runtime-agent service inventory has no active container")
                remediation.append("run docker compose up -d with the production runtime overlays selected")

        rows.append(
            {
                "runtime": runtime,
                "required": runtime in required_set,
                "ready": runtime in required_set and not blockers,
                "health_status": status,
                "health_placeholder": health_placeholder,
                "service_observed": service is not None,
                "service_inventory_available": service_inventory_available,
                "active_container_count": len(active_containers),
                "container_names": _unique_strings([container.get("name") for container in active_containers]),
                "container_images": _unique_strings([container.get("image") for container in active_containers]),
                "placeholder_reasons": placeholder_reasons,
                "blockers": blockers,
                "remediation": _unique_strings(remediation) if blockers else ["ready"],
            }
        )

    return rows


def runtime_production_readiness_check(
    runtime_health: list[dict[str, Any]],
    deployment_mode: str,
    required_runtimes: tuple[str, ...] | list[str],
    service_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = deployment_mode.strip().lower()
    required = normalize_runtime_names(required_runtimes)
    health_by_name = {runtime_health_name(item): item for item in runtime_health if runtime_health_name(item)}
    service_inventory_available = bool(service_inventory_items(service_inventory))
    service_placeholder_reasons = runtime_service_placeholder_reasons(service_inventory)
    placeholder_runtimes = sorted(
        {name for name, item in health_by_name.items() if runtime_health_is_placeholder(item)}
        | set(service_placeholder_reasons)
    )
    required_unhealthy: list[dict[str, str]] = []
    required_placeholders: list[str] = []

    for runtime in required:
        item = health_by_name.get(runtime)
        if item is None:
            required_unhealthy.append({"runtime": runtime, "status": "missing"})
            continue
        status = item.get("status")
        normalized_status = status.strip().lower() if isinstance(status, str) and status.strip() else "unknown"
        if normalized_status != "ok":
            required_unhealthy.append({"runtime": runtime, "status": normalized_status})
        if runtime_health_is_placeholder(item) or runtime in service_placeholder_reasons:
            required_placeholders.append(runtime)

    data = {
        "deployment_mode": mode,
        "required_runtimes": list(required),
        "placeholder_runtimes": placeholder_runtimes,
        "required_placeholders": required_placeholders,
        "required_unhealthy": required_unhealthy,
        "service_inventory_available": service_inventory_available,
        "service_placeholder_reasons": service_placeholder_reasons,
        "runtime_readiness": runtime_production_readiness_rows(runtime_health, required, service_inventory),
    }

    if mode not in VALID_RUNTIME_DEPLOYMENT_MODES:
        return check(
            "runtimes:production-readiness",
            "failed",
            f"invalid runtime deployment mode {deployment_mode!r}; use development or production",
            data,
        )
    if not required:
        return check("runtimes:production-readiness", "warning", "no production-required runtimes are configured", data)

    missing_inventory = not service_inventory_available
    blockers = bool(required_placeholders or required_unhealthy or missing_inventory)
    if not blockers:
        return check("runtimes:production-readiness", "ok", "required runtimes are production-ready", data)

    detail = "required runtimes are placeholders, missing, unhealthy, or unverifiable by runtime-agent inventory"
    if mode == "production":
        return check("runtimes:production-readiness", "failed", detail, data)
    return check("runtimes:production-readiness", "warning", f"{detail}; development mode permits bootstrapping only", data)
