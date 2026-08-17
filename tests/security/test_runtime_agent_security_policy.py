from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_AGENT_MAIN = ROOT / "services" / "runtime-agent" / "app" / "main.py"
RUNTIME_AGENT_DOCKER_API = ROOT / "services" / "runtime-agent" / "app" / "docker_api.py"

EXPECTED_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/v1/status"),
    ("GET", "/v1/services"),
    ("GET", "/v1/metrics"),
    ("POST", "/v1/services/{service_name}/restart"),
    ("POST", "/v1/services/{service_name}/start"),
    ("POST", "/v1/services/{service_name}/stop"),
    ("POST", "/v1/images/{service_name}/inspect"),
    ("POST", "/v1/images/{service_name}/pull"),
    ("POST", "/v1/runtime-actions/{service_name}/recover"),
    ("POST", "/v1/runtime-actions/{service_name}/unload"),
    ("GET", "/v1/services/{service_name}/logs"),
    ("POST", "/v1/rollback"),
}
FORBIDDEN_ROUTE_FRAGMENTS = {
    "build",
    "command",
    "container",
    "create",
    "docker",
    "env",
    "exec",
    "file",
    "mount",
    "plugin",
    "secret",
    "socket",
    "volume",
}
FORBIDDEN_DOCKER_API_FRAGMENTS = {
    "/build",
    "/commit",
    "/containers/create",
    "/exec",
    "/plugins",
    "/secrets",
    "/swarm",
    "/volumes/create",
}
EXPECTED_DOCKER_API_PREFIXES = {
    "/version",
    "/containers/json?{query}",
    "/containers/{container['id']}/logs?{query}",
    "/images/{quote(image_ref, safe='')}/json",
    "/images/create?{query}",
}


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def decorator_route(decorator: ast.AST) -> tuple[str, str] | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name) or func.value.id != "app":
        return None
    method = func.attr.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "WEBSOCKET"}:
        return None
    if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
        return None
    return method, decorator.args[0].value


def function_routes(tree: ast.Module) -> dict[str, set[tuple[str, str]]]:
    routes: dict[str, set[tuple[str, str]]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            route = decorator_route(decorator)
            if route:
                routes.setdefault(node.name, set()).add(route)
    return routes


def call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def function_by_name(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def string_constants(tree: ast.Module) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.add(node.value)
    return values


def render_string_expression(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + ast.unparse(value.value) + "}")
    return "".join(parts)


def docker_request_path_templates(tree: ast.Module) -> set[str]:
    templates: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {"json_request", "raw_request"}:
            continue
        if len(node.args) < 2:
            continue
        rendered = render_string_expression(node.args[1])
        if rendered and rendered.startswith("/"):
            templates.add(rendered)
    return templates


class RuntimeAgentSecurityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_tree = parsed(RUNTIME_AGENT_MAIN)
        cls.docker_tree = parsed(RUNTIME_AGENT_DOCKER_API)
        cls.routes_by_function = function_routes(cls.main_tree)

    def test_runtime_agent_route_surface_is_fixed_and_narrow(self) -> None:
        actual = {route for routes in self.routes_by_function.values() for route in routes}

        self.assertEqual(actual, EXPECTED_ROUTES)
        for method, path in actual:
            normalized = path.lower().replace("service_name", "")
            for fragment in FORBIDDEN_ROUTE_FRAGMENTS:
                self.assertNotIn(fragment, normalized, f"{method} {path} exposes forbidden route fragment {fragment!r}")

    def test_runtime_agent_mutating_routes_keep_required_guards(self) -> None:
        required_calls = {
            "restart_service": {"require_service", "check_mutation_rate_limit", "audit_mutation_event"},
            "start_service": {"require_service", "check_mutation_rate_limit", "audit_mutation_event"},
            "stop_service": {"require_service", "check_mutation_rate_limit", "audit_mutation_event"},
            "inspect_service_image": {"require_service", "require_pinned_image"},
            "pull_service_image": {"require_service", "require_pinned_image", "check_mutation_rate_limit", "audit_mutation_event"},
            "recover_runtime": {"require_runtime_service"},
            "unload_runtime": {"require_runtime_service"},
            "service_logs": {"require_service", "bound_log_lines"},
            "rollback": {"apply_predefined_rollback"},
        }
        for function_name, expected in required_calls.items():
            calls = call_names(function_by_name(self.main_tree, function_name))
            self.assertLessEqual(expected, calls, f"{function_name} is missing required guard calls")

        for function_name in ("run_runtime_restart_action", "apply_predefined_rollback"):
            calls = call_names(function_by_name(self.main_tree, function_name))
            self.assertIn("check_mutation_rate_limit", calls, f"{function_name} must rate-limit real mutations")
            self.assertIn("audit_mutation_event", calls, f"{function_name} must audit mutation outcomes")

    def test_runtime_agent_does_not_expose_arbitrary_docker_operations(self) -> None:
        source = RUNTIME_AGENT_DOCKER_API.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_DOCKER_API_FRAGMENTS:
            self.assertNotIn(fragment, source)

        templates = docker_request_path_templates(self.docker_tree)
        self.assertEqual(templates, EXPECTED_DOCKER_API_PREFIXES)

    def test_runtime_agent_does_not_accept_unbounded_host_inputs(self) -> None:
        constants = string_constants(self.main_tree) | string_constants(self.docker_tree)
        forbidden_terms = {"command", "entrypoint", "environment", "host_path", "mounts", "privileged"}
        for term in forbidden_terms:
            self.assertNotIn(term, constants, f"runtime-agent should not expose configurable {term}")

    def test_runtime_agent_docker_lookups_require_b1_compose_project(self) -> None:
        source = RUNTIME_AGENT_DOCKER_API.read_text(encoding="utf-8")

        self.assertIn("validate_compose_project", source)
        self.assertIn("com.docker.compose.project={project}", source)
        self.assertIn("validate_container_ownership", source)
        self.assertIn("no B1 AI Hub containers found", source)


if __name__ == "__main__":
    unittest.main()
