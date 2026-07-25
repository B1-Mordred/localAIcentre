from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = [
    ROOT / "compose.yaml",
    ROOT / "compose.legacy-comfy.yaml",
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


if __name__ == "__main__":
    unittest.main()
