from __future__ import annotations

from datetime import UTC, datetime
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any

from fastapi import Depends, Header, HTTPException
from fastapi import FastAPI
from pydantic import BaseModel, Field

from .docker_api import (
    AgentConfig,
    DEFAULT_RUNTIME_ACTION_SERVICES,
    DockerApiError,
    DockerEngineClient,
    bound_log_lines,
    parse_allowed_services,
    redact_line,
    require_allowed_service,
    require_runtime_action_service,
    validate_pinned_image_reference,
)
from .metrics import metrics_snapshot, parse_metric_paths


app = FastAPI(title="B1 AI Hub Runtime Agent", version="0.1.0")


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 600) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


ALLOWED_SERVICES = parse_allowed_services(
    os.getenv(
        "B1_ALLOWED_SERVICES",
        "control-plane,localai,comfyui,voicebox,audio-cpu,artifact-server,open-webui,gateway",
    )
)
RUNTIME_ACTION_SERVICES = parse_allowed_services(os.getenv("B1_RUNTIME_ACTION_SERVICES", ",".join(sorted(DEFAULT_RUNTIME_ACTION_SERVICES))))
MUTATIONS_ENABLED = bool_env("B1_ENABLE_MUTATIONS", False)
MUTATION_RATE_LIMIT_PER_MINUTE = int_env("B1_RUNTIME_AGENT_MUTATION_RATE_LIMIT_PER_MINUTE", 12)
MTLS_ENABLED = bool_env("B1_RUNTIME_AGENT_MTLS_ENABLED", True)
CLIENT_CERT_REQUIRED = bool_env("B1_RUNTIME_AGENT_CLIENT_CERT_REQUIRED", True)
ALLOW_MISSING_AUTH = bool_env("B1_RUNTIME_AGENT_ALLOW_MISSING_AUTH", False)
DOCKER_SOCKET = Path("/var/run/docker.sock")
COMPOSE_PROJECT = os.getenv("B1_COMPOSE_PROJECT") or None
B1_METRIC_PATHS = parse_metric_paths(os.getenv("B1_METRIC_PATHS", "/srv/b1-ai-hub,/tmp"))
ROLLBACK_SERVICES = tuple(
    item.strip()
    for item in os.getenv("B1_ROLLBACK_SERVICES", "localai,comfyui,voicebox,audio-cpu,artifact-server,open-webui,gateway").split(",")
    if item.strip()
)
RUNTIME_AGENT_TOKEN_FILE = os.getenv("B1_RUNTIME_AGENT_TOKEN_FILE", "")
RUNTIME_AGENT_TOKEN = os.getenv("B1_RUNTIME_AGENT_TOKEN", "")
CONFIG = AgentConfig(allowed_services=ALLOWED_SERVICES, docker_socket=DOCKER_SOCKET, compose_project=COMPOSE_PROJECT)
DOCKER = DockerEngineClient(DOCKER_SOCKET)
MUTATION_RATE_WINDOW: list[float] = []


class ServiceMutation(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    dry_run: bool = False


class ImageAction(BaseModel):
    image: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=500)
    dry_run: bool = False


def read_token() -> str:
    if RUNTIME_AGENT_TOKEN:
        return RUNTIME_AGENT_TOKEN.strip()
    if not RUNTIME_AGENT_TOKEN_FILE:
        return ""
    try:
        return Path(RUNTIME_AGENT_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


async def require_agent_auth(authorization: str | None = Header(default=None)) -> None:
    expected = read_token()
    if not expected:
        if ALLOW_MISSING_AUTH:
            return
        raise HTTPException(status_code=503, detail="runtime-agent bearer token is not configured")
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="runtime-agent bearer token is invalid")
    if len(expected) > 4096:
        raise HTTPException(status_code=503, detail="runtime-agent bearer token is invalid")
    if not authorization:
        raise HTTPException(status_code=401, detail="missing runtime-agent bearer token")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        raise HTTPException(status_code=401, detail="missing runtime-agent bearer token")
    supplied = supplied.strip()
    if len(supplied) > 4096:
        raise HTTPException(status_code=403, detail="invalid runtime-agent bearer token")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="invalid runtime-agent bearer token")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "runtime-agent"}


@app.get("/v1/status")
async def status(_: None = Depends(require_agent_auth)) -> dict[str, Any]:
    docker_version: dict[str, Any] | None = None
    docker_error: str | None = None
    try:
        docker_version = DOCKER.version()
    except DockerApiError as exc:
        docker_error = str(exc)
    return {
        "docker_socket_present": DOCKER_SOCKET.exists(),
        "mutations_enabled": MUTATIONS_ENABLED,
        "mutation_rate_limit_per_minute": MUTATION_RATE_LIMIT_PER_MINUTE,
        "allowed_services": sorted(ALLOWED_SERVICES),
        "runtime_action_services": sorted(RUNTIME_ACTION_SERVICES),
        "compose_project": COMPOSE_PROJECT,
        "auth_configured": bool(read_token()),
        "allow_missing_auth": ALLOW_MISSING_AUTH,
        "mtls_enabled": MTLS_ENABLED,
        "client_cert_required": CLIENT_CERT_REQUIRED if MTLS_ENABLED else False,
        "docker": {
            "version": docker_version,
            "error": docker_error,
        },
    }


@app.get("/v1/services")
async def services(_: None = Depends(require_agent_auth)) -> dict[str, Any]:
    try:
        statuses = DOCKER.service_statuses(sorted(ALLOWED_SERVICES), COMPOSE_PROJECT)
        return {"services": [{**service, "mutable": MUTATIONS_ENABLED} for service in statuses]}
    except DockerApiError as exc:
        return {
            "services": [
                {"name": name, "mutable": MUTATIONS_ENABLED, "containers": [], "docker_error": str(exc)}
                for name in sorted(ALLOWED_SERVICES)
            ]
        }


@app.get("/v1/metrics")
async def metrics(_: None = Depends(require_agent_auth)) -> dict[str, Any]:
    return metrics_snapshot(B1_METRIC_PATHS)


def require_service(name: str) -> str:
    try:
        return require_allowed_service(name, ALLOWED_SERVICES)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid service name") from None
    except KeyError:
        raise HTTPException(status_code=404, detail="service is not in the B1 AI Hub allowlist")


def require_runtime_service(name: str) -> str:
    try:
        return require_runtime_action_service(name, ALLOWED_SERVICES, RUNTIME_ACTION_SERVICES)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid service name") from None
    except KeyError:
        raise HTTPException(status_code=404, detail="service is not in the B1 AI Hub allowlist")
    except PermissionError:
        raise HTTPException(status_code=403, detail="service is not approved for runtime recovery actions") from None


def require_pinned_image(image: str) -> str:
    try:
        return validate_pinned_image_reference(image)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def emit_runtime_audit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def audit_mutation_event(
    action: str,
    status: str,
    *,
    service: str | None = None,
    reason: str = "",
    runtime_action: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": "runtime-agent.mutation",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "action": action,
        "status": status,
        "reason": redact_line(reason)[:500],
        "mutations_enabled": MUTATIONS_ENABLED,
        "rate_limit_per_minute": MUTATION_RATE_LIMIT_PER_MINUTE,
    }
    if service:
        payload["service"] = service
    if runtime_action:
        payload["runtime_action"] = True
    if details:
        payload["details"] = details
    emit_runtime_audit(payload)


def check_mutation_rate_limit(action: str, *, service: str | None = None, reason: str = "") -> None:
    now = time.monotonic()
    cutoff = now - 60.0
    MUTATION_RATE_WINDOW[:] = [item for item in MUTATION_RATE_WINDOW if item >= cutoff]
    if len(MUTATION_RATE_WINDOW) >= MUTATION_RATE_LIMIT_PER_MINUTE:
        audit_mutation_event(action, "rate_limited", service=service, reason=reason)
        raise HTTPException(status_code=429, detail="runtime-agent mutation rate limit exceeded")
    MUTATION_RATE_WINDOW.append(now)


def mutation_disabled_response(service: str, action: str, payload: ServiceMutation, *, runtime_action: bool = False) -> dict[str, Any]:
    audit_mutation_event(action, "dry_run", service=service, reason=payload.reason, runtime_action=runtime_action)
    return {
        "service": service,
        "action": action,
        "status": "dry_run",
        "reason": payload.reason,
        "message": "runtime-agent dry run requested" if payload.dry_run else "runtime-agent mutations are disabled",
    }


def image_mutation_disabled_response(service: str, action: str, payload: ImageAction, image: str) -> dict[str, Any]:
    audit_mutation_event(action, "dry_run", service=service, reason=payload.reason, details={"image": image})
    return {
        "service": service,
        "action": action,
        "status": "dry_run",
        "image": image,
        "reason": payload.reason,
        "message": "runtime-agent dry run requested" if payload.dry_run else "runtime-agent mutations are disabled",
    }


def runtime_action_disabled_response(service: str, action: str, payload: ServiceMutation) -> dict[str, Any]:
    return {
        **mutation_disabled_response(service, action, payload, runtime_action=True),
        "strategy": "restart_service",
        "runtime_action": True,
    }


def run_runtime_restart_action(service: str, action: str, payload: ServiceMutation) -> dict[str, Any]:
    if payload.dry_run or not MUTATIONS_ENABLED:
        return runtime_action_disabled_response(service, action, payload)
    check_mutation_rate_limit(action, service=service, reason=payload.reason)
    try:
        result = {
            **DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds),
            "status": "ok",
            "service": service,
            "action": action,
            "strategy": "restart_service",
            "runtime_action": True,
        }
        audit_mutation_event(action, "ok", service=service, reason=payload.reason, runtime_action=True)
        return result
    except DockerApiError as exc:
        audit_mutation_event(action, "failed", service=service, reason=payload.reason, runtime_action=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def rollback_plan_services() -> list[str]:
    services: list[str] = []
    seen: set[str] = set()
    for service_name in ROLLBACK_SERVICES:
        try:
            service = require_allowed_service(service_name, ALLOWED_SERVICES)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid rollback service name: {service_name}") from None
        except KeyError:
            raise HTTPException(status_code=422, detail=f"rollback service is not in the B1 AI Hub allowlist: {service_name}") from None
        if service not in seen:
            services.append(service)
            seen.add(service)
    if not services:
        raise HTTPException(status_code=422, detail="rollback plan must include at least one allowlisted service")
    return services


def rollback_disabled_response(payload: ServiceMutation, services: list[str]) -> dict[str, Any]:
    audit_mutation_event("rollback", "dry_run", reason=payload.reason, details={"services": services})
    return {
        "status": "dry_run",
        "action": "rollback",
        "reason": payload.reason,
        "strategy": "restart_services",
        "services": services,
        "message": "rollback mutations are disabled",
    }


def apply_predefined_rollback(payload: ServiceMutation) -> dict[str, Any]:
    services = rollback_plan_services()
    if payload.dry_run or not MUTATIONS_ENABLED:
        return rollback_disabled_response(payload, services)
    check_mutation_rate_limit("rollback", reason=payload.reason)
    results: list[dict[str, Any]] = []
    try:
        for service in services:
            results.append(DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds))
    except DockerApiError as exc:
        audit_mutation_event("rollback", "failed", reason=payload.reason, details={"services": services})
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit_mutation_event("rollback", "ok", reason=payload.reason, details={"services": services})
    return {
        "status": "ok",
        "action": "rollback",
        "reason": payload.reason,
        "strategy": "restart_services",
        "services": services,
        "results": results,
    }


@app.post("/v1/services/{service_name}/restart")
async def restart_service(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    if payload.dry_run or not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "restart", payload)
    check_mutation_rate_limit("restart", service=service, reason=payload.reason)
    try:
        result = {"status": "ok", **DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
        audit_mutation_event("restart", "ok", service=service, reason=payload.reason)
        return result
    except DockerApiError as exc:
        audit_mutation_event("restart", "failed", service=service, reason=payload.reason)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/services/{service_name}/start")
async def start_service(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    if payload.dry_run or not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "start", payload)
    check_mutation_rate_limit("start", service=service, reason=payload.reason)
    try:
        result = {"status": "ok", **DOCKER.start_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
        audit_mutation_event("start", "ok", service=service, reason=payload.reason)
        return result
    except DockerApiError as exc:
        audit_mutation_event("start", "failed", service=service, reason=payload.reason)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/services/{service_name}/stop")
async def stop_service(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    if payload.dry_run or not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "stop", payload)
    check_mutation_rate_limit("stop", service=service, reason=payload.reason)
    try:
        result = {"status": "ok", **DOCKER.stop_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
        audit_mutation_event("stop", "ok", service=service, reason=payload.reason)
        return result
    except DockerApiError as exc:
        audit_mutation_event("stop", "failed", service=service, reason=payload.reason)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/images/{service_name}/inspect")
async def inspect_service_image(service_name: str, payload: ImageAction, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    image = require_pinned_image(payload.image)
    try:
        return {
            "service": service,
            "action": "inspect",
            "status": "ok",
            "reason": payload.reason,
            "result": DOCKER.inspect_image(image),
        }
    except DockerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/images/{service_name}/pull")
async def pull_service_image(service_name: str, payload: ImageAction, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    image = require_pinned_image(payload.image)
    if payload.dry_run or not MUTATIONS_ENABLED:
        return image_mutation_disabled_response(service, "pull", payload, image)
    check_mutation_rate_limit("pull", service=service, reason=payload.reason)
    try:
        result = {
            "service": service,
            "reason": payload.reason,
            **DOCKER.pull_image(image),
        }
        audit_mutation_event("pull", "ok", service=service, reason=payload.reason, details={"image": image})
        return result
    except DockerApiError as exc:
        audit_mutation_event("pull", "failed", service=service, reason=payload.reason, details={"image": image})
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/runtime-actions/{service_name}/recover")
async def recover_runtime(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_runtime_service(service_name)
    return run_runtime_restart_action(service, "recover", payload)


@app.post("/v1/runtime-actions/{service_name}/unload")
async def unload_runtime(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_runtime_service(service_name)
    return run_runtime_restart_action(service, "unload", payload)


@app.get("/v1/services/{service_name}/logs")
async def service_logs(service_name: str, lines: int = 100, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    bounded_lines = bound_log_lines(lines)
    try:
        entries = DOCKER.service_logs(service, bounded_lines, COMPOSE_PROJECT)
        return {"service": service, "lines": bounded_lines, "entries": entries}
    except DockerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/rollback")
async def rollback(payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    return apply_predefined_rollback(payload)
