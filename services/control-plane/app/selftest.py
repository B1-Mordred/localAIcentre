from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

VALID_RUNTIME_DEPLOYMENT_MODES = {"development", "production"}
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


def normalize_runtime_names(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for name in names:
        value = str(name).strip().lower()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def runtime_production_readiness_check(
    runtime_health: list[dict[str, Any]],
    deployment_mode: str,
    required_runtimes: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    mode = deployment_mode.strip().lower()
    required = normalize_runtime_names(required_runtimes)
    health_by_name = {runtime_health_name(item): item for item in runtime_health if runtime_health_name(item)}
    placeholder_runtimes = sorted(name for name, item in health_by_name.items() if runtime_health_is_placeholder(item))
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
        if runtime_health_is_placeholder(item):
            required_placeholders.append(runtime)

    data = {
        "deployment_mode": mode,
        "required_runtimes": list(required),
        "placeholder_runtimes": placeholder_runtimes,
        "required_placeholders": required_placeholders,
        "required_unhealthy": required_unhealthy,
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

    blockers = bool(required_placeholders or required_unhealthy)
    if not blockers:
        return check("runtimes:production-readiness", "ok", "required runtimes are production-ready", data)

    detail = "required runtimes are placeholders, missing, or unhealthy"
    if mode == "production":
        return check("runtimes:production-readiness", "failed", detail, data)
    return check("runtimes:production-readiness", "warning", f"{detail}; development mode permits bootstrapping only", data)
