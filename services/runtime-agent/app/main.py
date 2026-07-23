from __future__ import annotations

import hmac
import os
from pathlib import Path
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


ALLOWED_SERVICES = parse_allowed_services(
    os.getenv(
        "B1_ALLOWED_SERVICES",
        "control-plane,localai,comfyui,voicebox,audio-cpu,artifact-server,open-webui,gateway",
    )
)
RUNTIME_ACTION_SERVICES = parse_allowed_services(os.getenv("B1_RUNTIME_ACTION_SERVICES", ",".join(sorted(DEFAULT_RUNTIME_ACTION_SERVICES))))
MUTATIONS_ENABLED = bool_env("B1_ENABLE_MUTATIONS", False)
MTLS_ENABLED = bool_env("B1_RUNTIME_AGENT_MTLS_ENABLED", True)
CLIENT_CERT_REQUIRED = bool_env("B1_RUNTIME_AGENT_CLIENT_CERT_REQUIRED", True)
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


class ServiceMutation(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    dry_run: bool = False


class ImageAction(BaseModel):
    image: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=500)


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
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing runtime-agent bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
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
        "allowed_services": sorted(ALLOWED_SERVICES),
        "runtime_action_services": sorted(RUNTIME_ACTION_SERVICES),
        "compose_project": COMPOSE_PROJECT,
        "auth_configured": bool(read_token()),
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


def mutation_disabled_response(service: str, action: str, payload: ServiceMutation) -> dict[str, Any]:
    return {
        "service": service,
        "action": action,
        "status": "dry_run",
        "reason": payload.reason,
        "message": "runtime-agent mutations are disabled",
    }


def image_mutation_disabled_response(service: str, action: str, payload: ImageAction, image: str) -> dict[str, Any]:
    return {
        "service": service,
        "action": action,
        "status": "dry_run",
        "image": image,
        "reason": payload.reason,
        "message": "runtime-agent mutations are disabled",
    }


def runtime_action_disabled_response(service: str, action: str, payload: ServiceMutation) -> dict[str, Any]:
    return {
        **mutation_disabled_response(service, action, payload),
        "strategy": "restart_service",
        "runtime_action": True,
    }


def run_runtime_restart_action(service: str, action: str, payload: ServiceMutation) -> dict[str, Any]:
    if payload.dry_run or not MUTATIONS_ENABLED:
        return runtime_action_disabled_response(service, action, payload)
    try:
        return {
            **DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds),
            "status": "ok",
            "service": service,
            "action": action,
            "strategy": "restart_service",
            "runtime_action": True,
        }
    except DockerApiError as exc:
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
    results: list[dict[str, Any]] = []
    try:
        for service in services:
            results.append(DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds))
    except DockerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    if not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "restart", payload)
    try:
        return {"status": "ok", **DOCKER.restart_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
    except DockerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/services/{service_name}/start")
async def start_service(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    if not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "start", payload)
    try:
        return {"status": "ok", **DOCKER.start_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
    except DockerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/services/{service_name}/stop")
async def stop_service(service_name: str, payload: ServiceMutation, _: None = Depends(require_agent_auth)) -> dict[str, Any]:
    service = require_service(service_name)
    if not MUTATIONS_ENABLED:
        return mutation_disabled_response(service, "stop", payload)
    try:
        return {"status": "ok", **DOCKER.stop_service(service, COMPOSE_PROJECT, payload.timeout_seconds)}
    except DockerApiError as exc:
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
    if not MUTATIONS_ENABLED:
        return image_mutation_disabled_response(service, "pull", payload, image)
    try:
        return {
            "service": service,
            "reason": payload.reason,
            **DOCKER.pull_image(image),
        }
    except DockerApiError as exc:
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
