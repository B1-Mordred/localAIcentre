from __future__ import annotations

import http.client
import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode


SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA256_DIGEST_RE = re.compile(r"@sha256:[a-fA-F0-9]{64}(?:$|[/?#])")
DEFAULT_RUNTIME_ACTION_SERVICES = frozenset({"localai", "comfyui", "voicebox", "audio-cpu"})
SECRET_PATTERNS = [
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"\bb1k_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bb1adm_[A-Za-z0-9_-]+\b"),
]


class DockerApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    allowed_services: frozenset[str]
    docker_socket: Path = Path("/var/run/docker.sock")
    compose_project: str | None = None


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self.sock = sock


class DockerEngineClient:
    def __init__(self, socket_path: Path = Path("/var/run/docker.sock"), timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        if not self.socket_path.exists():
            raise DockerApiError("Docker socket is not mounted")
        conn = UnixSocketHTTPConnection(self.socket_path, timeout=self.timeout)
        headers = {"Host": "docker"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
        except OSError as exc:
            raise DockerApiError(f"Docker socket request failed: {exc.__class__.__name__}") from exc
        finally:
            conn.close()
        if response.status >= 400:
            detail = payload.decode("utf-8", errors="replace")[:500]
            raise DockerApiError(f"Docker API {method} {path} returned {response.status}: {detail}")
        return response.status, payload

    def json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        _, response = self._request(method, path, body)
        if not response:
            return None
        return json.loads(response.decode("utf-8"))

    def raw_request(self, method: str, path: str) -> bytes:
        _, response = self._request(method, path)
        return response

    def version(self) -> dict[str, Any]:
        return self.json_request("GET", "/version")

    def containers_for_service(self, service: str, compose_project: str | None = None) -> list[dict[str, Any]]:
        filters = {"label": [f"com.docker.compose.service={service}"]}
        if compose_project:
            filters["label"].append(f"com.docker.compose.project={compose_project}")
        query = urlencode({"all": "true", "filters": json.dumps(filters)})
        containers = self.json_request("GET", f"/containers/json?{query}") or []
        return [container_summary(container) for container in containers]

    def service_statuses(self, services: list[str], compose_project: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "name": service,
                "containers": self.containers_for_service(service, compose_project),
            }
            for service in services
        ]

    def restart_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
        return self._mutate_service("restart", service, compose_project, timeout_seconds)

    def start_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
        return self._mutate_service("start", service, compose_project, timeout_seconds)

    def stop_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
        return self._mutate_service("stop", service, compose_project, timeout_seconds)

    def _mutate_service(self, action: str, service: str, compose_project: str | None, timeout_seconds: int) -> dict[str, Any]:
        containers = self.containers_for_service(service, compose_project)
        changed: list[dict[str, str]] = []
        for container in containers:
            container_id = container["id"]
            if action == "restart":
                path = f"/containers/{container_id}/restart?{urlencode({'t': str(timeout_seconds)})}"
            elif action == "stop":
                path = f"/containers/{container_id}/stop?{urlencode({'t': str(timeout_seconds)})}"
            elif action == "start":
                path = f"/containers/{container_id}/start"
            else:
                raise DockerApiError(f"unsupported service action: {action}")
            self.raw_request("POST", path)
            changed.append({"id": container_id, "name": container.get("name", ""), "action": action})
        return {"service": service, "action": action, "containers": changed}

    def service_logs(self, service: str, lines: int, compose_project: str | None = None) -> list[str]:
        containers = self.containers_for_service(service, compose_project)
        entries: list[str] = []
        for container in containers:
            query = urlencode({"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(lines)})
            raw = self.raw_request("GET", f"/containers/{container['id']}/logs?{query}")
            text = strip_docker_stream_headers(raw).decode("utf-8", errors="replace")
            entries.extend(redact_line(line) for line in text.splitlines() if line)
        return entries[-lines:]

    def inspect_image(self, image: str) -> dict[str, Any]:
        image_ref = validate_pinned_image_reference(image)
        try:
            details = self.json_request("GET", f"/images/{quote(image_ref, safe='')}/json")
        except DockerApiError as exc:
            if " returned 404:" in str(exc):
                return {"image": image_ref, "present": False}
            raise
        repo_digests = details.get("RepoDigests") or []
        return {
            "image": image_ref,
            "present": True,
            "id": details.get("Id", ""),
            "repo_tags": details.get("RepoTags") or [],
            "repo_digests": repo_digests,
            "size": details.get("Size"),
            "created": details.get("Created", ""),
            "digest_verified": image_ref.split("@", 1)[1] in {str(item).split("@", 1)[-1] for item in repo_digests if "@" in str(item)},
        }

    def pull_image(self, image: str) -> dict[str, Any]:
        image_ref = validate_pinned_image_reference(image)
        query = urlencode({"fromImage": image_ref})
        raw = self.raw_request("POST", f"/images/create?{query}")
        events = parse_docker_json_stream(raw)
        inspect_result = self.inspect_image(image_ref)
        if not inspect_result.get("present") or not inspect_result.get("digest_verified"):
            raise DockerApiError(f"pulled image digest could not be verified: {image_ref}")
        return {
            "image": image_ref,
            "action": "pull",
            "status": "ok",
            "events": summarize_pull_events(events),
            "inspect": inspect_result,
        }


def parse_allowed_services(value: str) -> frozenset[str]:
    services = frozenset(item.strip() for item in value.split(",") if item.strip())
    for service in services:
        validate_service_name(service)
    return services


def validate_service_name(service: str) -> str:
    if not SERVICE_RE.match(service):
        raise ValueError(f"invalid service name: {service}")
    return service


def validate_pinned_image_reference(image: str) -> str:
    image_ref = image.strip()
    if not image_ref:
        raise ValueError("image reference cannot be blank")
    tag_component = image_ref.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":latest" in tag_component:
        raise ValueError(f"image reference must not use latest: {image_ref}")
    if not SHA256_DIGEST_RE.search(image_ref):
        raise ValueError(f"image reference must include an immutable sha256 digest: {image_ref}")
    return image_ref


def require_allowed_service(service: str, allowed_services: frozenset[str]) -> str:
    validate_service_name(service)
    if service not in allowed_services:
        raise KeyError(service)
    return service


def require_runtime_action_service(
    service: str,
    allowed_services: frozenset[str],
    runtime_action_services: frozenset[str] = DEFAULT_RUNTIME_ACTION_SERVICES,
) -> str:
    require_allowed_service(service, allowed_services)
    if service not in runtime_action_services:
        raise PermissionError(service)
    return service


def bound_log_lines(lines: int, maximum: int = 500) -> int:
    return max(1, min(lines, maximum))


def redact_line(line: str) -> str:
    redacted = line
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + "<redacted>" if match.lastindex else "<redacted>", redacted)
    return redacted


def strip_docker_stream_headers(payload: bytes) -> bytes:
    frames: list[bytes] = []
    offset = 0
    while offset + 8 <= len(payload):
        header = payload[offset : offset + 8]
        stream_type = header[0]
        if stream_type not in {1, 2, 3} or header[1:4] != b"\x00\x00\x00":
            return payload
        size = int.from_bytes(header[4:8], "big")
        start = offset + 8
        end = start + size
        if end > len(payload):
            return payload
        frames.append(payload[start:end])
        offset = end
    if offset != len(payload):
        return payload
    return b"".join(frames)


def parse_docker_json_stream(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            events.append({"stream": redact_line(line[:500])})
            continue
        if isinstance(item, dict):
            events.append(redact_json_event(item))
    return events


def redact_json_event(event: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, str):
            redacted[key] = redact_line(value)
        elif isinstance(value, dict):
            redacted[key] = redact_json_event(value)
        else:
            redacted[key] = value
    return redacted


def summarize_pull_events(events: list[dict[str, Any]], maximum: int = 25) -> dict[str, Any]:
    errors = [str(event.get("error") or event.get("errorDetail") or "") for event in events if event.get("error") or event.get("errorDetail")]
    statuses: list[str] = []
    seen: set[str] = set()
    for event in events:
        status = str(event.get("status") or event.get("stream") or "").strip()
        if status and status not in seen:
            statuses.append(status[:200])
            seen.add(status)
    return {
        "event_count": len(events),
        "statuses": statuses[-maximum:],
        "errors": [redact_line(error)[:500] for error in errors[-maximum:]],
    }


def container_summary(container: dict[str, Any]) -> dict[str, Any]:
    names = container.get("Names") or []
    name = names[0].lstrip("/") if names else ""
    return {
        "id": str(container.get("Id", "")),
        "short_id": str(container.get("Id", ""))[:12],
        "name": name,
        "image": container.get("Image", ""),
        "image_id": container.get("ImageID", ""),
        "state": container.get("State", ""),
        "status": container.get("Status", ""),
        "labels": {
            "com.docker.compose.project": (container.get("Labels") or {}).get("com.docker.compose.project", ""),
            "com.docker.compose.service": (container.get("Labels") or {}).get("com.docker.compose.service", ""),
        },
    }
