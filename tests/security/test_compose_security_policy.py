from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
LEGACY_COMPOSE = ROOT / "compose.legacy-comfy.yaml"
LEGACY_CADDYFILE = ROOT / "deploy" / "caddy" / "Caddyfile.legacy-comfy"
COMPOSE_FILES = [
    ROOT / "compose.yaml",
    LEGACY_COMPOSE,
    ROOT / "compose.monitoring.yaml",
    ROOT / "compose.production-localai.yaml",
    ROOT / "compose.production-comfyui.yaml",
    ROOT / "compose.production-voicebox.yaml",
]
BACKEND_SERVICES = {
    "open-webui",
    "control-plane",
    "control-center",
    "media-studio",
    "runtime-agent",
    "localai",
    "comfyui",
    "voicebox",
    "audio-cpu",
    "artifact-server",
    "prometheus",
    "grafana",
    "postgres",
    "redis",
}
ALLOWED_BACKEND_CAP_ADD = {
    "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
    "redis": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
}


class ComposeLoader(yaml.SafeLoader):
    pass


def construct_unknown_tag(loader: ComposeLoader, _tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", construct_unknown_tag)


def load_compose(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.load(handle, Loader=ComposeLoader)
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path} did not parse as a Compose mapping")
    return loaded


def compose_default(value: str, variable_name: str) -> str:
    marker = f"${{{variable_name}:-"
    if marker not in value:
        raise AssertionError(f"{value!r} does not define {variable_name} with a required default")
    return value.split(marker, 1)[1].split("}", 1)[0]


class ComposeSecurityPolicyTests(unittest.TestCase):
    def test_only_gateway_publishes_host_ports(self) -> None:
        for path in COMPOSE_FILES:
            services = load_compose(path).get("services") or {}
            self.assertIsInstance(services, dict, path)
            for service_name, service in services.items():
                if not isinstance(service, dict):
                    continue
                ports = service.get("ports") or []
                if not ports:
                    continue
                self.assertEqual(
                    service_name,
                    "gateway",
                    f"{path.name}:{service_name} publishes host ports; backend APIs must stay internal",
                )
                if path.name != "compose.legacy-comfy.yaml":
                    self.assertNotIn(":8188", " ".join(str(port) for port in ports))

    def test_backend_services_are_not_privileged(self) -> None:
        for path in COMPOSE_FILES:
            services = load_compose(path).get("services") or {}
            for service_name, service in services.items():
                if service_name not in BACKEND_SERVICES or not isinstance(service, dict):
                    continue
                self.assertFalse(service.get("privileged"), f"{path.name}:{service_name} must not run privileged")
                cap_add = service.get("cap_add") or []
                expected = ALLOWED_BACKEND_CAP_ADD.get(service_name, set())
                self.assertLessEqual(
                    set(cap_add),
                    expected,
                    f"{path.name}:{service_name} adds unexpected Linux capabilities: {cap_add}",
                )

    def test_legacy_comfy_listener_is_profile_gated_gateway_override(self) -> None:
        services = load_compose(LEGACY_COMPOSE).get("services") or {}

        self.assertEqual(set(services), {"gateway"})
        gateway = services["gateway"]
        self.assertEqual(gateway["profiles"], ["legacy-comfy"])
        self.assertIn("/etc/caddy/Caddyfile.legacy-comfy", gateway["command"])
        self.assertTrue(any("Caddyfile.legacy-comfy" in volume for volume in gateway["volumes"]))
        self.assertEqual(
            gateway["ports"],
            ["${B1_LEGACY_COMFY_PUBLISH:-8188:8188}"],
        )

    def test_legacy_comfy_listener_uses_dhcp_safe_publish_and_cidr_defaults(self) -> None:
        gateway = load_compose(LEGACY_COMPOSE)["services"]["gateway"]
        port_mapping = gateway["ports"][0]
        publish_default = compose_default(port_mapping, "B1_LEGACY_COMFY_PUBLISH")

        self.assertEqual(publish_default, "8188:8188")
        self.assertEqual(publish_default.count(":"), 1)
        self.assertNotIn("192.168.2.100", port_mapping)
        self.assertNotIn("B1_LEGACY_COMFY_BIND", port_mapping)

        allow_cidrs = gateway["environment"]["B1_LEGACY_COMFY_ALLOW_CIDRS"]
        cidr_defaults = set(compose_default(allow_cidrs, "B1_LEGACY_COMFY_ALLOW_CIDRS").split())
        self.assertTrue(cidr_defaults)
        self.assertNotIn("0.0.0.0/0", cidr_defaults)
        self.assertNotIn("::/0", cidr_defaults)

    def test_legacy_comfy_listener_proxies_only_to_scheduler_compatibility_proxy(self) -> None:
        caddyfile = LEGACY_CADDYFILE.read_text(encoding="utf-8")

        self.assertIn(":8188", caddyfile)
        self.assertIn("@denied not remote_ip {$B1_LEGACY_COMFY_ALLOW_CIDRS", caddyfile)
        self.assertIn('respond @denied "legacy ComfyUI listener denied by B1 AI Hub CIDR policy" 403', caddyfile)
        self.assertIn("reverse_proxy control-plane:8000", caddyfile)
        self.assertIn("header_up -Authorization", caddyfile)
        self.assertIn("header_up -Cookie", caddyfile)
        self.assertIn("header_up -X-B1-CSRF", caddyfile)
        self.assertIn("header_up X-B1-Compatibility comfyui-legacy-8188", caddyfile)
        self.assertNotIn("reverse_proxy comfyui", caddyfile)
        self.assertNotIn("comfyui:8188", caddyfile)


if __name__ == "__main__":
    unittest.main()
