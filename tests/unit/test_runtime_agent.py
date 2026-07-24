from __future__ import annotations

import asyncio
import sys
import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DOCKER_API_PATH = ROOT / "services" / "runtime-agent" / "app" / "docker_api.py"
METRICS_PATH = ROOT / "services" / "runtime-agent" / "app" / "metrics.py"
RUNTIME_AGENT_APP_ROOT = ROOT / "services" / "runtime-agent" / "app"
spec = importlib.util.spec_from_file_location("runtime_agent_docker_api", DOCKER_API_PATH)
docker_api = importlib.util.module_from_spec(spec)
sys.modules["runtime_agent_docker_api"] = docker_api
assert spec.loader is not None
spec.loader.exec_module(docker_api)
metrics_spec = importlib.util.spec_from_file_location("runtime_agent_metrics", METRICS_PATH)
runtime_metrics = importlib.util.module_from_spec(metrics_spec)
sys.modules["runtime_agent_metrics"] = runtime_metrics
assert metrics_spec.loader is not None
metrics_spec.loader.exec_module(runtime_metrics)

DockerEngineClient = docker_api.DockerEngineClient
bound_log_lines = docker_api.bound_log_lines
parse_allowed_services = docker_api.parse_allowed_services
redact_line = docker_api.redact_line
require_allowed_service = docker_api.require_allowed_service
require_runtime_action_service = docker_api.require_runtime_action_service
strip_docker_stream_headers = docker_api.strip_docker_stream_headers
validate_pinned_image_reference = docker_api.validate_pinned_image_reference
parse_meminfo = runtime_metrics.parse_meminfo
parse_metric_paths = runtime_metrics.parse_metric_paths
parse_nvidia_smi_csv = runtime_metrics.parse_nvidia_smi_csv
disk_snapshot = runtime_metrics.disk_snapshot

try:
    agent_pkg_spec = importlib.util.spec_from_file_location(
        "runtime_agent_app",
        RUNTIME_AGENT_APP_ROOT / "__init__.py",
        submodule_search_locations=[str(RUNTIME_AGENT_APP_ROOT)],
    )
    if agent_pkg_spec is None or agent_pkg_spec.loader is None:
        raise ModuleNotFoundError("runtime_agent_app")
    runtime_agent_pkg = importlib.util.module_from_spec(agent_pkg_spec)
    sys.modules["runtime_agent_app"] = runtime_agent_pkg
    agent_pkg_spec.loader.exec_module(runtime_agent_pkg)
    agent_main_spec = importlib.util.spec_from_file_location("runtime_agent_app.main", RUNTIME_AGENT_APP_ROOT / "main.py")
    if agent_main_spec is None or agent_main_spec.loader is None:
        raise ModuleNotFoundError("runtime_agent_app.main")
    runtime_agent_main = importlib.util.module_from_spec(agent_main_spec)
    sys.modules["runtime_agent_app.main"] = runtime_agent_main
    agent_main_spec.loader.exec_module(runtime_agent_main)
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "pydantic"}:
        raise
    runtime_agent_main = None
    MISSING_RUNTIME_AGENT_DEPENDENCY = exc.name
else:
    MISSING_RUNTIME_AGENT_DEPENDENCY = ""


class FakeDockerEngineClient(DockerEngineClient):
    def __init__(self) -> None:
        super().__init__(Path("/tmp/missing-docker.sock"))
        self.requests: list[tuple[str, str]] = []
        self.containers = [
            {
                "Id": "abcdef1234567890",
                "Names": ["/b1-ai-hub-control-plane-1"],
                "Image": "b1-control-plane:test",
                "ImageID": "sha256:" + "d" * 64,
                "State": "running",
                "Status": "Up 1 minute",
                "Labels": {
                    "com.docker.compose.project": "b1-ai-hub",
                    "com.docker.compose.service": "control-plane",
                },
            }
        ]

    def json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, path))
        if path.startswith("/containers/json"):
            return self.containers
        if path.startswith("/images/"):
            return {
                "Id": "sha256:test",
                "RepoTags": ["ghcr.io/b1/control-plane:0.2.0"],
                "RepoDigests": ["ghcr.io/b1/control-plane@sha256:" + "a" * 64],
                "Size": 1234,
                "Created": "2026-07-22T12:00:00Z",
            }
        return {"Version": "test"}

    def raw_request(self, method: str, path: str) -> bytes:
        self.requests.append((method, path))
        if path.startswith("/images/create?"):
            return b'{"status":"Pulling from b1/control-plane"}\n{"status":"Digest: sha256:' + b"a" * 64 + b'"}\n'
        if "/logs?" in path:
            line = b"2026-07-22T09:00:00Z Authorization: Bearer b1k_public.secret\n"
            return b"\x01\x00\x00\x00" + len(line).to_bytes(4, "big") + line
        return b""


class RuntimeAgentTests(unittest.TestCase):
    def test_allowed_service_parser_rejects_invalid_names(self) -> None:
        self.assertEqual(parse_allowed_services("control-plane, localai"), frozenset({"control-plane", "localai"}))
        with self.assertRaises(ValueError):
            parse_allowed_services("control-plane,../../docker")

    def test_pinned_image_reference_validation_rejects_floating_images(self) -> None:
        pinned = "ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64
        self.assertEqual(validate_pinned_image_reference(pinned), pinned)
        with self.assertRaises(ValueError):
            validate_pinned_image_reference("ghcr.io/b1/control-plane:latest")
        with self.assertRaises(ValueError):
            validate_pinned_image_reference("ghcr.io/b1/control-plane:0.2.0")

    def test_require_allowed_service_rejects_unknown_service(self) -> None:
        allowed = frozenset({"control-plane"})
        self.assertEqual(require_allowed_service("control-plane", allowed), "control-plane")
        with self.assertRaises(KeyError):
            require_allowed_service("postgres", allowed)

    def test_require_runtime_action_service_only_allows_runtime_subset(self) -> None:
        allowed = frozenset({"control-plane", "localai", "comfyui"})
        runtime_services = frozenset({"localai", "comfyui"})

        self.assertEqual(require_runtime_action_service("localai", allowed, runtime_services), "localai")
        with self.assertRaises(PermissionError):
            require_runtime_action_service("control-plane", allowed, runtime_services)
        with self.assertRaises(KeyError):
            require_runtime_action_service("voicebox", allowed, runtime_services)

    def test_log_line_bounds_and_redaction(self) -> None:
        self.assertEqual(bound_log_lines(-10), 1)
        self.assertEqual(bound_log_lines(999), 500)
        redacted = redact_line("Authorization: Bearer b1adm_secret b1k_public.secret")
        self.assertNotIn("b1adm_secret", redacted)
        self.assertNotIn("b1k_public.secret", redacted)
        self.assertIn("<redacted>", redacted)

    def test_strips_docker_multiplex_headers(self) -> None:
        payload = b"hello\n"
        framed = b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
        self.assertEqual(strip_docker_stream_headers(framed), payload)
        self.assertEqual(strip_docker_stream_headers(payload), payload)

    def test_docker_client_filters_by_compose_service_and_project(self) -> None:
        client = FakeDockerEngineClient()
        containers = client.containers_for_service("control-plane", "b1-ai-hub")
        self.assertEqual(containers[0]["id"], "abcdef1234567890")
        self.assertEqual(containers[0]["short_id"], "abcdef123456")
        self.assertEqual(containers[0]["image_id"], "sha256:" + "d" * 64)
        method, path = client.requests[0]
        self.assertEqual(method, "GET")
        self.assertIn("com.docker.compose.service", path)
        self.assertIn("com.docker.compose.project", path)

    def test_docker_client_restart_uses_allowlisted_container_ids(self) -> None:
        client = FakeDockerEngineClient()
        result = client.restart_service("control-plane", "b1-ai-hub", timeout_seconds=7)
        self.assertEqual(result["containers"][0]["id"], "abcdef1234567890")
        self.assertEqual(client.requests[-1], ("POST", "/containers/abcdef1234567890/restart?t=7"))

    def test_docker_client_logs_are_bounded_and_redacted(self) -> None:
        client = FakeDockerEngineClient()
        entries = client.service_logs("control-plane", 10, "b1-ai-hub")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("b1k_public.secret", entries[0])
        self.assertIn("<redacted>", entries[0])

    def test_docker_client_inspects_and_pulls_pinned_images(self) -> None:
        client = FakeDockerEngineClient()
        image = "ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64

        inspect_result = client.inspect_image(image)
        pull_result = client.pull_image(image)

        self.assertTrue(inspect_result["present"])
        self.assertTrue(inspect_result["digest_verified"])
        self.assertEqual(pull_result["status"], "ok")
        self.assertEqual(pull_result["events"]["event_count"], 2)
        self.assertTrue(any(path.startswith("/images/create?") for _, path in client.requests))

    def test_metrics_parse_meminfo_paths_and_nvidia_csv(self) -> None:
        meminfo = parse_meminfo("MemTotal:       1024 kB\nMemAvailable:    256 kB\nSwapTotal:         0 kB\n")
        self.assertEqual(meminfo["MemTotal"], 1024 * 1024)
        self.assertEqual(meminfo["MemAvailable"], 256 * 1024)
        self.assertEqual(parse_metric_paths(" /srv/b1-ai-hub, /tmp "), [Path("/srv/b1-ai-hub"), Path("/tmp")])
        devices = parse_nvidia_smi_csv("0, NVIDIA RTX 3060, GPU-abc, 55, 105.5, 76, 12288, 4096, 8192\n")
        self.assertEqual(devices[0]["name"], "NVIDIA RTX 3060")
        self.assertEqual(devices[0]["memory_total_mib"], 12288)
        self.assertEqual(devices[0]["power_watts"], 105.5)

    def test_disk_snapshot_reports_missing_paths_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp)
            missing = existing / "missing"
            snapshots = disk_snapshot([existing, missing])
            self.assertTrue(snapshots[0]["available"])
            self.assertFalse(snapshots[1]["available"])


@unittest.skipIf(runtime_agent_main is None, f"{MISSING_RUNTIME_AGENT_DEPENDENCY} is not installed in this lightweight test environment")
class RuntimeAgentRollbackTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(runtime_agent_main, name)
        setattr(runtime_agent_main, name, value)
        self.addCleanup(lambda: setattr(runtime_agent_main, name, original))

    def test_predefined_rollback_dry_run_uses_static_allowlisted_plan(self) -> None:
        self.patch_attr("ALLOWED_SERVICES", frozenset({"localai", "comfyui", "gateway"}))
        self.patch_attr("ROLLBACK_SERVICES", ("localai", "comfyui", "gateway"))
        self.patch_attr("MUTATIONS_ENABLED", False)

        payload = runtime_agent_main.ServiceMutation(reason="operator rollback", timeout_seconds=5)
        result = runtime_agent_main.apply_predefined_rollback(payload)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["strategy"], "restart_services")
        self.assertEqual(result["services"], ["localai", "comfyui", "gateway"])

    def test_predefined_rollback_rejects_services_outside_allowlist(self) -> None:
        self.patch_attr("ALLOWED_SERVICES", frozenset({"localai"}))
        self.patch_attr("ROLLBACK_SERVICES", ("localai", "postgres"))
        self.patch_attr("MUTATIONS_ENABLED", False)

        payload = runtime_agent_main.ServiceMutation(reason="bad rollback", timeout_seconds=5)
        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            runtime_agent_main.apply_predefined_rollback(payload)
        self.assertEqual(raised.exception.status_code, 422)

    def test_predefined_rollback_restarts_plan_when_mutations_enabled(self) -> None:
        class FakeDocker:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str | None, int]] = []

            def restart_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
                self.calls.append((service, compose_project, timeout_seconds))
                return {"service": service, "action": "restart", "containers": [{"id": f"{service}_1"}]}

        fake_docker = FakeDocker()
        self.patch_attr("ALLOWED_SERVICES", frozenset({"localai", "gateway"}))
        self.patch_attr("ROLLBACK_SERVICES", ("localai", "gateway", "localai"))
        self.patch_attr("MUTATIONS_ENABLED", True)
        self.patch_attr("COMPOSE_PROJECT", "b1-ai-hub")
        self.patch_attr("DOCKER", fake_docker)

        payload = runtime_agent_main.ServiceMutation(reason="apply rollback", timeout_seconds=9)
        result = runtime_agent_main.apply_predefined_rollback(payload)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["services"], ["localai", "gateway"])
        self.assertEqual(fake_docker.calls, [("localai", "b1-ai-hub", 9), ("gateway", "b1-ai-hub", 9)])

    def test_service_mutation_dry_run_does_not_restart_when_mutations_enabled(self) -> None:
        class FakeDocker:
            def restart_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
                raise AssertionError("dry-run restart must not mutate containers")

        self.patch_attr("ALLOWED_SERVICES", frozenset({"localai"}))
        self.patch_attr("MUTATIONS_ENABLED", True)
        self.patch_attr("DOCKER", FakeDocker())

        payload = runtime_agent_main.ServiceMutation(reason="operator test", timeout_seconds=5, dry_run=True)
        result = asyncio.run(runtime_agent_main.restart_service("localai", payload))

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["message"], "runtime-agent dry run requested")

    def test_runtime_unload_dry_run_does_not_restart_when_mutations_enabled(self) -> None:
        class FakeDocker:
            def restart_service(self, service: str, compose_project: str | None = None, timeout_seconds: int = 10) -> dict[str, Any]:
                raise AssertionError("dry-run unload must not restart containers")

        self.patch_attr("MUTATIONS_ENABLED", True)
        self.patch_attr("DOCKER", FakeDocker())

        payload = runtime_agent_main.ServiceMutation(reason="self-test", timeout_seconds=5, dry_run=True)
        result = runtime_agent_main.run_runtime_restart_action("localai", "unload", payload)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["service"], "localai")
        self.assertEqual(result["action"], "unload")
        self.assertTrue(result["runtime_action"])

    def test_mutation_rate_limit_rejects_excessive_mutations_and_audits(self) -> None:
        events: list[dict[str, Any]] = []
        self.patch_attr("MUTATION_RATE_LIMIT_PER_MINUTE", 1)
        self.patch_attr("MUTATION_RATE_WINDOW", [])
        self.patch_attr("emit_runtime_audit", events.append)

        runtime_agent_main.check_mutation_rate_limit("restart", service="localai", reason="first")
        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            runtime_agent_main.check_mutation_rate_limit("restart", service="localai", reason="second")

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(events[0]["status"], "rate_limited")
        self.assertEqual(events[0]["action"], "restart")
        self.assertEqual(events[0]["service"], "localai")

    def test_mutation_audit_redacts_sensitive_reason_values(self) -> None:
        events: list[dict[str, Any]] = []
        self.patch_attr("emit_runtime_audit", events.append)

        runtime_agent_main.mutation_disabled_response(
            "localai",
            "restart",
            runtime_agent_main.ServiceMutation(reason="Authorization: Bearer b1k_public.secret", timeout_seconds=5),
        )

        self.assertEqual(events[0]["event"], "runtime-agent.mutation")
        self.assertNotIn("b1k_public.secret", events[0]["reason"])
        self.assertIn("<redacted>", events[0]["reason"])

    def test_image_pull_endpoint_is_dry_run_when_mutations_disabled(self) -> None:
        self.patch_attr("ALLOWED_SERVICES", frozenset({"control-plane"}))
        self.patch_attr("MUTATIONS_ENABLED", False)

        payload = runtime_agent_main.ImageAction(
            image="ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64,
            reason="stage update",
        )
        result = asyncio.run(runtime_agent_main.pull_service_image("control-plane", payload))

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["action"], "pull")
        self.assertEqual(result["service"], "control-plane")

    def test_image_endpoint_rejects_non_pinned_images(self) -> None:
        self.patch_attr("ALLOWED_SERVICES", frozenset({"control-plane"}))

        payload = runtime_agent_main.ImageAction(image="ghcr.io/b1/control-plane:latest", reason="stage update")
        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            asyncio.run(runtime_agent_main.pull_service_image("control-plane", payload))

        self.assertEqual(raised.exception.status_code, 422)

    def test_status_reports_mtls_posture(self) -> None:
        class FakeDocker:
            def version(self) -> dict[str, str]:
                return {"Version": "test"}

        self.patch_attr("DOCKER", FakeDocker())
        self.patch_attr("MTLS_ENABLED", True)
        self.patch_attr("CLIENT_CERT_REQUIRED", True)
        self.patch_attr("MUTATION_RATE_LIMIT_PER_MINUTE", 7)

        result = asyncio.run(runtime_agent_main.status())

        self.assertTrue(result["mtls_enabled"])
        self.assertTrue(result["client_cert_required"])
        self.assertEqual(result["mutation_rate_limit_per_minute"], 7)
        self.assertEqual(result["docker"]["version"], {"Version": "test"})

    def test_v1_auth_fails_closed_when_token_is_missing(self) -> None:
        self.patch_attr("ALLOW_MISSING_AUTH", False)
        self.patch_attr("read_token", lambda: "")

        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            asyncio.run(runtime_agent_main.require_agent_auth(None))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("not configured", raised.exception.detail)

    def test_v1_auth_allows_explicit_missing_auth_development_bypass(self) -> None:
        self.patch_attr("ALLOW_MISSING_AUTH", True)
        self.patch_attr("read_token", lambda: "")

        asyncio.run(runtime_agent_main.require_agent_auth(None))

    def test_v1_auth_rejects_short_configured_token(self) -> None:
        self.patch_attr("ALLOW_MISSING_AUTH", False)
        self.patch_attr("read_token", lambda: "short")

        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            asyncio.run(runtime_agent_main.require_agent_auth("Bearer short"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("invalid", raised.exception.detail)

    def test_v1_auth_accepts_only_matching_bearer_token(self) -> None:
        token = "t" * 48
        self.patch_attr("ALLOW_MISSING_AUTH", False)
        self.patch_attr("read_token", lambda: token)

        asyncio.run(runtime_agent_main.require_agent_auth(f"Bearer {token}"))

        with self.assertRaises(runtime_agent_main.HTTPException) as raised:
            asyncio.run(runtime_agent_main.require_agent_auth("Bearer wrong-token"))
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
