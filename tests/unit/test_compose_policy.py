from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COMPOSE_SERVICES = {
    "gateway",
    "open-webui",
    "control-plane",
    "control-center",
    "media-studio",
    "runtime-agent",
    "localai",
    "comfyui",
    "voicebox",
    "audio-cpu",
    "postgres",
    "redis",
    "artifact-server",
    "bootstrap",
}
SHA256_REF_RE = re.compile(r"@sha256:[a-fA-F0-9]{64}(?:\s|$)")
DOCKERFILES_REQUIRING_PINNED_BASES = [
    ROOT / "deploy" / "comfyui" / "Dockerfile",
    ROOT / "deploy" / "localai" / "Dockerfile",
    ROOT / "deploy" / "open-webui" / "Dockerfile",
    ROOT / "deploy" / "voicebox" / "Dockerfile",
    ROOT / "integrations" / "b1-model-client" / "Dockerfile",
    ROOT / "services" / "artifact-server" / "Dockerfile",
    ROOT / "services" / "audio-cpu" / "Dockerfile",
    ROOT / "services" / "control-plane" / "Dockerfile",
    ROOT / "services" / "mock-runtime" / "Dockerfile",
    ROOT / "services" / "runtime-agent" / "Dockerfile",
    ROOT / "web" / "control-center" / "Dockerfile",
    ROOT / "web" / "media-studio" / "Dockerfile",
]


class ComposePolicyLoader(yaml.SafeLoader):
    pass


def _compose_reset(loader: yaml.SafeLoader, node: yaml.Node):
    return None


def _compose_override(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AssertionError(f"{path}:{line_number} is not a KEY=VALUE line")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise AssertionError(f"{path}:{line_number} has an empty key")
        if key in values:
            raise AssertionError(f"{path}:{line_number} duplicates {key}")
        values[key] = value.strip()
    return values


ComposePolicyLoader.add_constructor("!reset", _compose_reset)
ComposePolicyLoader.add_constructor("!override", _compose_override)


class ComposePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.load((ROOT / "compose.yaml").read_text(encoding="utf-8"), Loader=ComposePolicyLoader)
        cls.legacy_compose = yaml.load((ROOT / "compose.legacy-comfy.yaml").read_text(encoding="utf-8"), Loader=ComposePolicyLoader)
        cls.monitoring_text = (ROOT / "compose.monitoring.yaml").read_text(encoding="utf-8")
        cls.production_localai_text = (ROOT / "compose.production-localai.yaml").read_text(encoding="utf-8")
        cls.production_comfyui_text = (ROOT / "compose.production-comfyui.yaml").read_text(encoding="utf-8")
        cls.production_voicebox_text = (ROOT / "compose.production-voicebox.yaml").read_text(encoding="utf-8")
        cls.production_env_text = (ROOT / ".env.production.example").read_text(encoding="utf-8")
        cls.production_env = _read_env(ROOT / ".env.production.example")
        cls.monitoring_compose = yaml.load(
            cls.monitoring_text,
            Loader=ComposePolicyLoader,
        )
        cls.production_localai_compose = yaml.load(
            cls.production_localai_text,
            Loader=ComposePolicyLoader,
        )
        cls.production_comfyui_compose = yaml.load(
            cls.production_comfyui_text,
            Loader=ComposePolicyLoader,
        )
        cls.production_voicebox_compose = yaml.load(
            cls.production_voicebox_text,
            Loader=ComposePolicyLoader,
        )

    def test_required_services_exist(self) -> None:
        services = set(self.compose["services"])
        self.assertTrue(REQUIRED_COMPOSE_SERVICES.issubset(services), REQUIRED_COMPOSE_SERVICES - services)

    def test_required_services_have_healthchecks(self) -> None:
        missing = sorted(
            service_name
            for service_name in REQUIRED_COMPOSE_SERVICES
            if "healthcheck" not in self.compose["services"][service_name]
        )
        self.assertEqual(missing, [])

    def test_runtime_services_expose_placeholder_readiness_labels(self) -> None:
        for runtime in ("localai", "comfyui", "voicebox"):
            labels = self.compose["services"][runtime]["labels"]
            self.assertEqual(labels["b1.ai-hub.runtime"], runtime)
            self.assertEqual(labels["b1.ai-hub.runtime.kind"], f"placeholder-{runtime}")
            self.assertEqual(labels["b1.ai-hub.placeholder"], "true")
        audio_labels = self.compose["services"]["audio-cpu"]["labels"]
        self.assertEqual(audio_labels["b1.ai-hub.runtime"], "audio-cpu")
        self.assertEqual(audio_labels["b1.ai-hub.runtime.kind"], "audio-cpu")
        self.assertNotIn("b1.ai-hub.placeholder", audio_labels)

    def test_open_webui_uses_generated_internal_api_key(self) -> None:
        open_webui = self.compose["services"]["open-webui"]
        control_plane = self.compose["services"]["control-plane"]
        self.assertNotIn("OPENAI_API_KEY", open_webui.get("environment", {}))
        self.assertNotIn("OPENAI_API_KEYS", open_webui.get("environment", {}))
        self.assertEqual(open_webui["build"]["context"], "./deploy/open-webui")
        self.assertEqual(open_webui["environment"]["B1_OPEN_WEBUI_API_BASE_URL"], "${B1_OPEN_WEBUI_API_BASE_URL:-http://control-plane:8000/v1}")
        self.assertEqual(open_webui["environment"]["B1_OPEN_WEBUI_API_KEY_FILE"], "/run/secrets/open_webui_api_key")
        self.assertEqual(open_webui["environment"]["B1_OPEN_WEBUI_SECRET_KEY_FILE"], "/run/secrets/open_webui_secret_key")
        self.assertEqual(open_webui["user"], "999:999")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", open_webui.get("volumes", []))
        self.assertIn("bootstrap", open_webui.get("depends_on", {}))
        self.assertEqual(control_plane["environment"]["B1_OPEN_WEBUI_API_KEY_FILE"], "/run/secrets/open_webui_api_key")
        dockerfile = (ROOT / "deploy" / "open-webui" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/open-webui/open-webui:v0.10.2@sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4", dockerfile)
        self.assertIn("chgrp -R 999 /app/backend/open_webui/static", dockerfile)

    def test_open_webui_is_local_only_by_default(self) -> None:
        environment = self.compose["services"]["open-webui"]["environment"]
        self.assertEqual(environment["ENABLE_OPENAI_API"], "true")
        self.assertEqual(environment["ENABLE_PERSISTENT_CONFIG"], "false")
        self.assertEqual(environment["ENABLE_OAUTH_PERSISTENT_CONFIG"], "false")
        self.assertEqual(environment["B1_OPEN_WEBUI_API_BASE_URL"], "${B1_OPEN_WEBUI_API_BASE_URL:-http://control-plane:8000/v1}")
        self.assertEqual(environment["ENABLE_OLLAMA_API"], "false")
        self.assertEqual(environment["OLLAMA_BASE_URL"], "http://127.0.0.1:9")
        self.assertEqual(environment["OLLAMA_BASE_URLS"], "http://127.0.0.1:9")
        self.assertEqual(environment["RAG_EMBEDDING_ENGINE"], "openai")
        self.assertEqual(environment["RAG_EMBEDDING_MODEL"], "${B1_OPEN_WEBUI_RAG_EMBEDDING_MODEL:-embedding-default}")
        self.assertEqual(environment["RAG_EMBEDDING_MODEL_AUTO_UPDATE"], "false")
        self.assertEqual(environment["RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE"], "false")
        self.assertEqual(environment["RAG_RERANKING_MODEL_AUTO_UPDATE"], "false")
        self.assertEqual(environment["RAG_RERANKING_MODEL_TRUST_REMOTE_CODE"], "false")
        self.assertEqual(environment["AUDIO_TTS_ENGINE"], "openai")
        self.assertEqual(environment["AUDIO_TTS_MODEL"], "${B1_OPEN_WEBUI_TTS_MODEL:-tts-fast}")
        self.assertEqual(environment["AUDIO_STT_ENGINE"], "openai")
        self.assertEqual(environment["AUDIO_STT_MODEL"], "${B1_OPEN_WEBUI_STT_MODEL:-stt-default}")
        self.assertEqual(environment["AUDIO_STT_OPENAI_API_REQUEST_FORMAT"], "multipart")
        self.assertEqual(environment["ENABLE_IMAGE_GENERATION"], "false")
        self.assertEqual(environment["ENABLE_IMAGE_EDIT"], "false")
        self.assertEqual(environment["USER_PERMISSIONS_FEATURES_IMAGE_GENERATION"], "false")
        self.assertEqual(environment["ENABLE_WEB_SEARCH"], "false")
        self.assertEqual(environment["ENABLE_LOCAL_WEB_FETCH"], "false")
        self.assertEqual(environment["WEB_SEARCH_TRUST_ENV"], "false")
        self.assertEqual(environment["USER_PERMISSIONS_FEATURES_WEB_SEARCH"], "false")
        self.assertEqual(environment["ENABLE_COMMUNITY_SHARING"], "false")
        self.assertEqual(environment["ENABLE_EVALUATION_ARENA_MODELS"], "false")
        self.assertEqual(environment["ENABLE_VERSION_UPDATE_CHECK"], "false")
        self.assertEqual(environment["OFFLINE_MODE"], "true")
        self.assertEqual(environment["ENABLE_OTEL"], "false")

    def test_no_latest_image_tags(self) -> None:
        for name, service in self.compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotIn(":latest", image, name)
        for name, service in self.production_localai_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotIn(":latest", image, name)
        for name, service in self.production_comfyui_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotIn(":latest", image, name)
        for name, service in self.production_voicebox_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotIn(":latest", image, name)
        for name, service in self.monitoring_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotIn(":latest", image, name)

    def test_explicit_third_party_compose_images_are_digest_pinned(self) -> None:
        for name, service in self.compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertRegex(image, SHA256_REF_RE, f"{name} image must be pinned by immutable digest")
        for name, service in self.monitoring_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertRegex(image, SHA256_REF_RE, f"{name} monitoring image must be pinned by immutable digest")

    def test_dockerfile_base_images_are_digest_pinned(self) -> None:
        for dockerfile in DOCKERFILES_REQUIRING_PINNED_BASES:
            for line_number, line in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.startswith("FROM "):
                    continue
                self.assertRegex(line, SHA256_REF_RE, f"{dockerfile}:{line_number} base image must be digest-pinned")

    def test_dockerfiles_avoid_broad_unpinned_upgrade_steps(self) -> None:
        denied_patterns = ("apt-get upgrade", "apk upgrade", "--upgrade pip")
        for dockerfile in DOCKERFILES_REQUIRING_PINNED_BASES:
            text = dockerfile.read_text(encoding="utf-8")
            for pattern in denied_patterns:
                self.assertNotIn(pattern, text, f"{dockerfile} must not run broad mutable upgrade step {pattern!r}")

    def test_only_gateway_publishes_ports(self) -> None:
        for name, service in self.compose["services"].items():
            if name == "gateway":
                continue
            self.assertNotIn("ports", service, name)
        for name, service in self.monitoring_compose["services"].items():
            self.assertNotIn("ports", service, name)

    def test_internal_networks_exist(self) -> None:
        networks = self.compose["networks"]
        for name in ("app", "data", "runtime"):
            self.assertTrue(networks[name].get("internal"), name)

    def test_only_control_plane_has_model_download_egress(self) -> None:
        networks = self.compose["networks"]
        self.assertIn("egress", networks)
        self.assertFalse(networks["egress"].get("internal", False))

        services_with_egress = sorted(
            name
            for name, service in self.compose["services"].items()
            if "egress" in service.get("networks", [])
        )
        self.assertEqual(services_with_egress, ["control-plane"])
        for runtime in ("localai", "comfyui", "voicebox", "audio-cpu"):
            self.assertNotIn("egress", self.compose["services"][runtime].get("networks", []), runtime)

    def test_bootstrap_precedes_stateful_services(self) -> None:
        for name in ("postgres", "redis", "control-plane", "runtime-agent"):
            depends_on = self.compose["services"][name].get("depends_on", {})
            self.assertIn("bootstrap", depends_on, name)

    def test_bootstrap_healthcheck_covers_all_runtime_model_views(self) -> None:
        healthcheck = "\n".join(str(item) for item in self.compose["services"]["bootstrap"]["healthcheck"]["test"])
        for runtime in ("localai", "comfyui", "voicebox", "audio-cpu"):
            self.assertIn(f"models/runtime-views/{runtime}", healthcheck)
        self.assertIn("data/prometheus", healthcheck)
        self.assertIn("data/grafana", healthcheck)
        self.assertIn("workflows/acceptance", healthcheck)
        for filename in (
            "native-comfyui-smoke-prompt.json",
            "native-comfyui-upload-save-prompt.json",
            "text-to-image-api-prompt.json",
            "image-generation-job.json",
            "image-edit-job.json",
            "short-video-job.json",
        ):
            self.assertIn(f"workflows/acceptance/{filename}", healthcheck)

    def test_bootstrap_mounts_acceptance_templates_read_only(self) -> None:
        volumes = self.compose["services"]["bootstrap"].get("volumes", [])
        self.assertIn("./workflows/acceptance:/opt/b1/workflows/acceptance:ro", volumes)

    def test_control_plane_mounts_generated_secrets_read_only(self) -> None:
        volumes = self.compose["services"]["control-plane"].get("volumes", [])
        self.assertTrue(any("/run/secrets:ro" in volume for volume in volumes))
        environment = self.compose["services"]["control-plane"].get("environment", {})
        self.assertEqual(environment["B1_RUNTIME_AGENT_TOKEN_FILE"], "/run/secrets/runtime_agent_token")
        self.assertEqual(environment["RUNTIME_AGENT_URL"], "${RUNTIME_AGENT_URL:-https://runtime-agent:8443}")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_CA_FILE"], "/run/secrets/runtime_agent_mtls_ca.crt")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_CLIENT_CERT_FILE"], "/run/secrets/runtime_agent_client.crt")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_CLIENT_KEY_FILE"], "/run/secrets/runtime_agent_client.key")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_VERIFY"], "${B1_RUNTIME_AGENT_TLS_VERIFY:-true}")
        self.assertEqual(environment["B1_PROMETHEUS_SCRAPE_TOKEN_FILE"], "/run/secrets/prometheus_scrape_token")
        self.assertEqual(environment["B1_ARTIFACT_SERVER_TOKEN_FILE"], "/run/secrets/artifact_server_token")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(environment["B1_DEV_AUTH_BYPASS"], "${B1_DEV_AUTH_BYPASS:-false}")
        self.assertEqual(environment["B1_OPENAI_COMPATIBLE_BASE_URL"], "${B1_OPENAI_COMPATIBLE_BASE_URL:-}")
        self.assertEqual(environment["B1_OPENAI_COMPATIBLE_API_KEY_FILE"], "${B1_OPENAI_COMPATIBLE_API_KEY_FILE:-}")
        self.assertEqual(environment["B1_GENERIC_HTTP_BASE_URL"], "${B1_GENERIC_HTTP_BASE_URL:-}")
        self.assertEqual(environment["B1_SESSION_COOKIE_SECURE"], "${B1_SESSION_COOKIE_SECURE:-true}")
        self.assertEqual(environment["B1_SESSION_TTL_SECONDS"], "${B1_SESSION_TTL_SECONDS:-28800}")
        self.assertIn("B1_CORS_ALLOW_ORIGINS", environment)

    def test_frontends_default_to_same_origin_api_with_public_api_snippets(self) -> None:
        expected = "https://${B1_HOST_API:-api.ai.b1.germering}"
        self.assertEqual(self.compose["services"]["control-center"]["build"]["network"], "host")
        self.assertEqual(self.compose["services"]["media-studio"]["build"]["network"], "host")
        self.assertEqual(self.compose["services"]["control-center"]["build"]["args"]["VITE_B1_API_BASE"], "")
        self.assertEqual(self.compose["services"]["media-studio"]["build"]["args"]["VITE_B1_API_BASE"], "")
        self.assertEqual(self.compose["services"]["control-center"]["build"]["args"]["VITE_B1_PUBLIC_API_BASE"], expected)
        self.assertEqual(self.compose["services"]["media-studio"]["build"]["args"]["VITE_B1_PUBLIC_API_BASE"], expected)

    def test_browser_ui_hosts_proxy_same_origin_api_routes(self) -> None:
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        control_block = caddyfile[caddyfile.index("{$B1_HOST_CONTROL"):caddyfile.index("{$B1_HOST_MEDIA")]
        media_block = caddyfile[caddyfile.index("{$B1_HOST_MEDIA"):caddyfile.index("{$B1_HOST_API")]
        for block, app_backend in ((control_block, "control-center:8080"), (media_block, "media-studio:8080")):
            self.assertIn("/auth/* /admin/* /v1/* /modelhub/* /workflows/* /healthz /openapi.json", block)
            self.assertIn("reverse_proxy control-plane:8000", block)
            self.assertIn(f"reverse_proxy {app_backend}", block)

    def test_gateway_tls_can_use_internal_ca_or_supplied_certificates(self) -> None:
        gateway = self.compose["services"]["gateway"]
        control_plane = self.compose["services"]["control-plane"]
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertEqual(gateway["environment"]["B1_CADDY_TLS_ARGS"], "${B1_CADDY_TLS_ARGS:-internal}")
        self.assertEqual(control_plane["environment"]["B1_CADDY_TLS_ARGS"], "${B1_CADDY_TLS_ARGS:-internal}")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets/caddy-certs:/etc/caddy/external-certs:ro", gateway["volumes"])
        self.assertIn("tls {$B1_CADDY_TLS_ARGS:internal}", caddyfile)
        self.assertNotIn("\ttls internal", caddyfile)
        for host_var in (
            "B1_HOST_CHAT",
            "B1_HOST_CONTROL",
            "B1_HOST_MEDIA",
            "B1_HOST_API",
            "B1_HOST_MODELS",
            "B1_HOST_COMFY",
            "B1_HOST_VOICE",
            "B1_HOST_MONITORING",
        ):
            block_start = caddyfile.index("{$" + host_var)
            block_end = caddyfile.find("\n}\n", block_start)
            self.assertIn("import b1_tls", caddyfile[block_start:block_end], host_var)
        self.assertEqual(self.production_env["B1_CADDY_TLS_ARGS"], "internal")

    def test_gateway_caddy_admin_api_is_loopback_only(self) -> None:
        gateway = self.compose["services"]["gateway"]
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("admin 127.0.0.1:2019", caddyfile)
        self.assertNotIn("admin 0.0.0.0:2019", caddyfile)
        healthcheck = "\n".join(str(item) for item in gateway["healthcheck"]["test"])
        self.assertIn("http://127.0.0.1:2019/config/", healthcheck)

    def test_caddy_ca_export_runs_after_gateway_and_writes_control_plane_copy(self) -> None:
        service = self.compose["services"]["caddy-ca-export"]
        self.assertTrue(service["read_only"])
        self.assertEqual(service["restart"], "no")
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["cap_add"], ["CHOWN", "DAC_OVERRIDE", "FOWNER"])
        self.assertEqual(service["depends_on"]["gateway"]["condition"], "service_healthy")
        self.assertIn("--export-caddy-ca-only", service["command"])
        self.assertIn(
            "${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/caddy:${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/caddy:ro",
            service["volumes"],
        )
        self.assertIn(
            "${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/control-plane:${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/control-plane",
            service["volumes"],
        )

    def test_gateway_permissions_policy_scopes_browser_capture_to_interactive_hosts(self) -> None:
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn('Permissions-Policy "camera=(), microphone=(), geolocation=()"', caddyfile)
        self.assertIn('Permissions-Policy "camera=(self), microphone=(self), geolocation=()"', caddyfile)
        for host_var in ("B1_HOST_CHAT", "B1_HOST_MEDIA", "B1_HOST_VOICE"):
            block_start = caddyfile.index("{$" + host_var)
            block_end = caddyfile.find("\n}\n", block_start)
            block = caddyfile[block_start:block_end]
            self.assertIn("import media_capture_security_headers", block, host_var)
            self.assertNotIn("import security_headers", block, host_var)
        for host_var in ("B1_HOST_CONTROL", "B1_HOST_API", "B1_HOST_MODELS", "B1_HOST_COMFY"):
            block_start = caddyfile.index("{$" + host_var)
            block_end = caddyfile.find("\n}\n", block_start)
            block = caddyfile[block_start:block_end]
            self.assertIn("import security_headers", block, host_var)
            self.assertNotIn("import media_capture_security_headers", block, host_var)
        monitoring_block_start = caddyfile.index("{$B1_HOST_MONITORING")
        monitoring_block_end = caddyfile.find("\n}\n", monitoring_block_start)
        monitoring_block = caddyfile[monitoring_block_start:monitoring_block_end]
        self.assertIn("import security_headers", monitoring_block)
        self.assertIn("reverse_proxy grafana:3000", monitoring_block)

    def test_gateway_applies_configurable_request_body_limit_to_all_entrypoints(self) -> None:
        gateway = self.compose["services"]["gateway"]
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        legacy_caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile.legacy-comfy").read_text(encoding="utf-8")

        self.assertEqual(gateway["environment"]["B1_CADDY_REQUEST_BODY_LIMIT"], "${B1_CADDY_REQUEST_BODY_LIMIT:-268435456}")
        self.assertEqual(self.production_env["B1_CADDY_REQUEST_BODY_LIMIT"], "268435456")
        self.assertIn("max_size {$B1_CADDY_REQUEST_BODY_LIMIT:268435456}", caddyfile)
        for host_var in (
            "B1_HOST_CHAT",
            "B1_HOST_CONTROL",
            "B1_HOST_MEDIA",
            "B1_HOST_API",
            "B1_HOST_MODELS",
            "B1_HOST_COMFY",
            "B1_HOST_VOICE",
            "B1_HOST_MONITORING",
        ):
            block_start = caddyfile.index("{$" + host_var)
            block_end = caddyfile.find("\n}\n", block_start)
            self.assertIn("import request_limits", caddyfile[block_start:block_end], host_var)
        self.assertIn("import request_limits", legacy_caddyfile)

    def test_optional_monitoring_profile_is_internal_and_scrapes_metrics_only_endpoint(self) -> None:
        services = self.monitoring_compose["services"]
        prometheus = services["prometheus"]
        grafana = services["grafana"]
        prometheus_config = (ROOT / "deploy" / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
        datasource = (
            ROOT / "deploy" / "monitoring" / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
        ).read_text(encoding="utf-8")
        dashboard = (ROOT / "deploy" / "monitoring" / "grafana" / "dashboards" / "b1-ai-hub-overview.json").read_text(encoding="utf-8")

        for service_name, service in services.items():
            self.assertEqual(service["profiles"], ["monitoring"], service_name)
            self.assertIn("app", service["networks"], service_name)
            self.assertNotIn("ports", service, service_name)
            self.assertIn("no-new-privileges:true", service["security_opt"], service_name)
            self.assertEqual(service["cap_drop"], ["ALL"], service_name)
            self.assertIn("healthcheck", service, service_name)

        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets/prometheus_scrape_token:/run/secrets/prometheus_scrape_token:ro", prometheus["volumes"])
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets/grafana_admin_password:/run/secrets/grafana_admin_password:ro", grafana["volumes"])
        for key in (
            "GF_ANALYTICS_REPORTING_ENABLED",
            "GF_ANALYTICS_CHECK_FOR_UPDATES",
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES",
            "GF_NEWS_NEWS_FEED_ENABLED",
        ):
            self.assertEqual(grafana["environment"][key], "false", key)
        for key in (
            "GF_PLUGINS_PREINSTALL_DISABLED",
            "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED",
            "GF_SECURITY_DISABLE_GRAVATAR",
        ):
            self.assertEqual(grafana["environment"][key], "true", key)
        self.assertIn("credentials_file: /run/secrets/prometheus_scrape_token", prometheus_config)
        self.assertIn("metrics_path: /admin/metrics.prometheus", prometheus_config)
        self.assertIn("control-plane:8000", prometheus_config)
        self.assertNotIn("admin_bootstrap_key", prometheus_config)
        self.assertNotIn("master_encryption_key", prometheus_config)
        self.assertIn("url: http://prometheus:9090", datasource)
        self.assertIn("b1_ai_hub_queue_depth_total", dashboard)
        self.assertIn("b1_ai_hub_gpu_memory_used_mib", dashboard)
        self.assertNotIn("resolved_model_version", dashboard)
        self.assertNotIn("owner", dashboard)

    def test_control_plane_mounts_artifacts_for_job_runner(self) -> None:
        service = self.compose["services"]["control-plane"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertTrue(any("/srv/b1-ai-hub/artifacts" in volume for volume in volumes))
        self.assertEqual(environment["B1_ARTIFACT_ROOT"], "/srv/b1-ai-hub/artifacts")
        self.assertIn("B1_JOB_RUNNER_ENABLED", environment)
        self.assertIn("B1_GPU_JOB_RUNNER_ENABLED", environment)
        self.assertEqual(environment["B1_GPU_JOB_RUNNER_LEASE_TTL_SECONDS"], "${B1_GPU_JOB_RUNNER_LEASE_TTL_SECONDS:-300}")
        self.assertEqual(environment["B1_COMFY_PROMPT_WAIT_TIMEOUT_SECONDS"], "${B1_COMFY_PROMPT_WAIT_TIMEOUT_SECONDS:-120}")
        self.assertEqual(environment["B1_COMFY_PROMPT_LEASE_TTL_SECONDS"], "${B1_COMFY_PROMPT_LEASE_TTL_SECONDS:-7200}")
        self.assertEqual(environment["B1_COMFY_PROMPT_POLL_SECONDS"], "${B1_COMFY_PROMPT_POLL_SECONDS:-2}")
        self.assertEqual(environment["B1_COMFY_PROMPT_COMPLETION_TIMEOUT_SECONDS"], "${B1_COMFY_PROMPT_COMPLETION_TIMEOUT_SECONDS:-7200}")
        self.assertEqual(environment["B1_COMFY_PROMPT_IDLE_GRACE_SECONDS"], "${B1_COMFY_PROMPT_IDLE_GRACE_SECONDS:-5}")

    def test_artifact_server_requires_generated_internal_token(self) -> None:
        service = self.compose["services"]["artifact-server"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertEqual(environment["B1_ARTIFACT_SERVER_TOKEN_FILE"], "/run/secrets/artifact_server_token")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", volumes)
        self.assertEqual(self.production_env["B1_ARTIFACT_SERVER_TOKEN_FILE"], "/run/secrets/artifact_server_token")

    def test_control_plane_backup_mounts_are_b1_root_scoped(self) -> None:
        service = self.compose["services"]["control-plane"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}:/srv/b1-ai-hub:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/control-plane:/srv/b1-ai-hub/data/control-plane", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/blobs:/srv/b1-ai-hub/models/blobs", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/backups:/srv/b1-ai-hub/backups", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/restore-tests:/srv/b1-ai-hub/restore-tests", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views:/srv/b1-ai-hub/models/runtime-views", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/quarantine:/srv/b1-ai-hub/models/quarantine", volumes)
        self.assertEqual(environment["B1_DATA_ROOT"], "/srv/b1-ai-hub")
        self.assertEqual(environment["B1_BACKUP_ROOT"], "/srv/b1-ai-hub/backups")
        self.assertEqual(environment["B1_RESTORE_TEST_ROOT"], "/srv/b1-ai-hub/restore-tests")
        self.assertEqual(environment["B1_RUNTIME_DEPLOYMENT_MODE"], "${B1_RUNTIME_DEPLOYMENT_MODE:-development}")
        self.assertEqual(environment["B1_RUNTIME_PRODUCTION_REQUIRED"], "${B1_RUNTIME_PRODUCTION_REQUIRED:-localai,comfyui,audio-cpu}")

    def test_runtime_containers_mount_only_runtime_model_views(self) -> None:
        expected_mounts = {
            "localai": "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/localai:/srv/b1-ai-hub/models:ro",
            "comfyui": "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/comfyui:/srv/b1-ai-hub/models:ro",
            "voicebox": "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/voicebox:/srv/b1-ai-hub/models:ro",
            "audio-cpu": "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/audio-cpu:/srv/b1-ai-hub/models:ro",
        }
        forbidden = "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models:/srv/b1-ai-hub/models:ro"
        for service_name, expected in expected_mounts.items():
            volumes = self.compose["services"][service_name].get("volumes", [])
            self.assertIn(expected, volumes, service_name)
            self.assertNotIn(forbidden, volumes, service_name)
            self.assertFalse(
                any("/models/blobs" in volume for volume in volumes),
                f"{service_name} must not mount the authoritative blob store",
            )

    def test_artifact_server_mounts_model_blobs_read_only(self) -> None:
        volumes = self.compose["services"]["artifact-server"].get("volumes", [])
        self.assertIn(
            "${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/blobs:/srv/b1-ai-hub/models/blobs:ro",
            volumes,
        )

    def test_audio_cpu_placeholder_engine_is_policy_gated(self) -> None:
        service = self.compose["services"]["audio-cpu"]
        build_args = service.get("build", {}).get("args", {})
        environment = service.get("environment", {})
        self.assertEqual(build_args["B1_INSTALL_PIPER"], "${B1_INSTALL_PIPER:-true}")
        self.assertEqual(build_args["B1_PIPER_RELEASE"], "${B1_PIPER_RELEASE:-2023.11.14-2}")
        self.assertEqual(build_args["B1_PIPER_ASSET"], "${B1_PIPER_ASSET:-piper_linux_x86_64.tar.gz}")
        self.assertEqual(
            build_args["B1_PIPER_SHA256"],
            "${B1_PIPER_SHA256:-a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992}",
        )
        self.assertEqual(environment["B1_CPU_AUDIO_ENGINE"], "${B1_CPU_AUDIO_ENGINE:-scaffold}")
        self.assertEqual(environment["B1_CPU_AUDIO_ENABLE_PLACEHOLDER"], "${B1_CPU_AUDIO_ENABLE_PLACEHOLDER:-true}")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_REQUIRE_AUTH"], "${B1_RUNTIME_CONTROL_REQUIRE_AUTH:-true}")
        self.assertEqual(environment["B1_CPU_AUDIO_MODEL_ROOT"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["B1_CPU_RESIDENCY_ENABLED"], "${B1_CPU_RESIDENCY_ENABLED:-true}")
        self.assertEqual(environment["B1_CPU_RESIDENCY_MAX_RAM_GIB"], "${B1_CPU_RESIDENCY_MAX_RAM_GIB:-2}")
        self.assertEqual(environment["B1_CPU_RESIDENT_ALIASES"], "${B1_CPU_RESIDENT_ALIASES:-embedding-default,tts-fast,stt-default}")
        self.assertEqual(environment["B1_HOST_TOTAL_RAM_GIB"], "${B1_HOST_TOTAL_RAM_GIB:-32}")
        self.assertEqual(environment["B1_HOST_RESERVE_RAM_GIB"], "${B1_HOST_RESERVE_RAM_GIB:-6}")
        self.assertEqual(environment["B1_PIPER_BINARY"], "${B1_PIPER_BINARY:-/opt/piper/piper}")
        self.assertEqual(environment["B1_PIPER_MODEL_PATH"], "${B1_PIPER_MODEL_PATH:-}")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", service.get("volumes", []))

    def test_production_env_selects_real_runtime_overlays_for_default_command(self) -> None:
        self.assertEqual(
            self.production_env["COMPOSE_FILE"].split(":"),
            [
                "compose.yaml",
                "compose.production-localai.yaml",
                "compose.production-comfyui.yaml",
                "compose.production-voicebox.yaml",
            ],
        )
        self.assertEqual(self.production_env["COMPOSE_PROFILES"], "voicebox")
        self.assertNotIn("compose.legacy-comfy.yaml", self.production_env["COMPOSE_FILE"])
        self.assertEqual(self.production_env["B1_RUNTIME_DEPLOYMENT_MODE"], "production")
        required_runtimes = {
            item
            for item in self.production_env["B1_RUNTIME_PRODUCTION_REQUIRED"].replace(",", " ").split()
            if item
        }
        self.assertEqual(required_runtimes, {"localai", "comfyui", "audio-cpu"})
        self.assertIn(
            "# B1_RUNTIME_PRODUCTION_REQUIRED=localai,comfyui,audio-cpu,voicebox",
            self.production_env_text,
        )

    def test_control_plane_receives_prepared_source_metadata(self) -> None:
        environment = self.compose["services"]["control-plane"]["environment"]
        for key in ("B1_SOURCE_COMMIT", "B1_SOURCE_REF", "B1_SOURCE_DIRTY", "B1_SOURCE_DIRTY_PATH_COUNT"):
            self.assertIn(key, self.production_env)
            self.assertEqual(environment[key], f"${{{key}:-}}")

    def test_docs_keep_compose_up_as_fresh_install_start_command(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        production_env_text = (ROOT / ".env.production.example").read_text(encoding="utf-8")

        self.assertIn("The Compose `bootstrap` service runs the same idempotent setup first", readme)
        self.assertIn("It is not required for a fresh installation", installation)
        self.assertIn("the required operator start command remains exactly `docker compose up -d`", installation)
        self.assertIn("make prepare-production-env", production_env_text)
        self.assertIn("docker compose up -d", production_env_text)
        self.assertNotIn("cp .env.production.example .env", production_env_text)
        self.assertNotIn("#   make bootstrap\n#   docker compose up -d", production_env_text)

    def test_host_network_policy_uses_system_hostname_and_dhcp(self) -> None:
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        migration = (ROOT / "docs" / "migration.md").read_text(encoding="utf-8")
        docs = "\n".join([env_text, readme, installation, migration])
        env_normalized = re.sub(r"\s+", " ", env_text)

        self.assertIn("B1_APPLIANCE_HOSTNAME is the B1-defined", docs)
        self.assertIn("B1_APPLIANCE_HOSTNAME=ai.b1.germering", env_text)
        self.assertIn("B1_EXPECTED_TARGET_HOST=ai.b1.germering", env_text)
        self.assertIn("make verify-system-hostname", docs)
        self.assertIn("make apply-system-hostname", docs)
        self.assertIn("the target OS hostname must present", docs)
        self.assertIn("hostname_authority=b1-appliance-config", installation)
        self.assertIn("Do not configure a static B1 appliance IP", installation)
        self.assertIn("B1-defined appliance hostname applied as the target OS hostname plus host-managed DHCP", migration)
        self.assertIn("Host IP/gateway/route/resolver properties", env_normalized)
        self.assertIn("should come from DHCP", env_normalized)
        self.assertIn("or a DHCP reservation", env_normalized)
        self.assertIn("does not configure a static host IP", docs)

    def test_production_env_disables_development_placeholders_and_cloud_by_default(self) -> None:
        self.assertEqual(self.production_env["B1_CPU_AUDIO_ENGINE"], "piper")
        self.assertEqual(self.production_env["B1_CPU_EMBEDDING_ENGINE"], "onnx")
        self.assertEqual(self.production_env["B1_CPU_STT_ENGINE"], "vosk")
        self.assertEqual(self.production_env["B1_CPU_AUDIO_ENABLE_PLACEHOLDER"], "false")
        self.assertEqual(self.production_env["B1_ALLOW_EXTERNAL_PROVIDERS"], "false")
        self.assertEqual(self.production_env["B1_ENABLE_MUTATIONS"], "false")
        self.assertEqual(self.production_env["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(self.production_env["B1_RUNTIME_CONTROL_REQUIRE_AUTH"], "true")

    def test_production_localai_override_builds_b1_wrapper_image(self) -> None:
        service = self.production_localai_compose["services"]["localai"]
        build_args = service["build"]["args"]
        environment = service["environment"]
        volumes = service["volumes"]
        device = service["deploy"]["resources"]["reservations"]["devices"][0]

        self.assertEqual(service["build"]["context"], "./deploy/localai")
        self.assertEqual(service["image"], "${B1_LOCALAI_IMAGE:-b1-ai-hub/localai:v4.7.1-laguna-b1}")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime"], "localai")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime.kind"], "localai")
        self.assertEqual(service["labels"]["b1.ai-hub.placeholder"], "false")
        self.assertEqual(build_args["B1_LOCALAI_UPSTREAM_VERSION"], "${B1_LOCALAI_UPSTREAM_VERSION:-v4.7.1-gpu-nvidia-cuda-12}")
        self.assertEqual(build_args["B1_LOCALAI_UPSTREAM_COMMIT"], "${B1_LOCALAI_UPSTREAM_COMMIT:-b224c96db6f4b87306a33a808650bfce63b12588}")
        self.assertNotIn("ports", service)
        self.assertFalse(service["read_only"])
        self.assertIsNone(environment["B1_RUNTIME_KIND"])
        self.assertIsNone(environment["B1_RUNTIME_NAME"])
        self.assertEqual(environment["B1_LOCALAI_PUBLIC_HOST"], "${B1_LOCALAI_PUBLIC_HOST:-0.0.0.0}")
        self.assertEqual(environment["B1_LOCALAI_PUBLIC_PORT"], "${B1_LOCALAI_PUBLIC_PORT:-8080}")
        self.assertEqual(environment["B1_LOCALAI_UPSTREAM_ADDRESS"], "${B1_LOCALAI_UPSTREAM_ADDRESS:-127.0.0.1:18080}")
        self.assertEqual(environment["B1_LOCALAI_UPSTREAM_URL"], "${B1_LOCALAI_UPSTREAM_URL:-http://127.0.0.1:18080}")
        self.assertEqual(environment["B1_LOCALAI_HOOK_STRICT_MODEL_LIST"], "${B1_LOCALAI_HOOK_STRICT_MODEL_LIST:-false}")
        self.assertEqual(environment["B1_LOCALAI_HOOK_WARM_ENABLED"], "${B1_LOCALAI_HOOK_WARM_ENABLED:-false}")
        self.assertEqual(environment["B1_LOCALAI_HOOK_SMOKE_ENABLED"], "${B1_LOCALAI_HOOK_SMOKE_ENABLED:-false}")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_REQUIRE_AUTH"], "${B1_RUNTIME_CONTROL_REQUIRE_AUTH:-true}")
        self.assertEqual(environment["B1_LOCALAI_VIDEO_SMOKE_ENABLED"], "${B1_LOCALAI_VIDEO_SMOKE_ENABLED:-false}")
        self.assertEqual(environment["LOCALAI_ADDRESS"], "${B1_LOCALAI_UPSTREAM_ADDRESS:-127.0.0.1:18080}")
        self.assertEqual(environment["LOCALAI_MODELS_PATH"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["LOCALAI_BACKENDS_PATH"], "/srv/b1-ai-hub/localai/backends")
        self.assertEqual(environment["LOCALAI_CONFIG_DIR"], "/srv/b1-ai-hub/localai/configuration")
        self.assertEqual(environment["LOCALAI_DATA_PATH"], "/srv/b1-ai-hub/localai/data")
        self.assertEqual(environment["LOCALAI_MAX_ACTIVE_BACKENDS"], "${B1_LOCALAI_MAX_ACTIVE_BACKENDS:-1}")
        self.assertEqual(environment["LOCALAI_WATCHDOG_IDLE"], "${B1_LOCALAI_WATCHDOG_IDLE:-true}")
        self.assertEqual(environment["LOCALAI_WATCHDOG_IDLE_TIMEOUT"], "${B1_LOCALAI_WATCHDOG_IDLE_TIMEOUT:-5m}")
        self.assertEqual(environment["LOCALAI_WATCHDOG_INTERVAL"], "${B1_LOCALAI_WATCHDOG_INTERVAL:-1s}")
        self.assertEqual(environment["LOCALAI_FORCE_EVICTION_WHEN_BUSY"], "${B1_LOCALAI_FORCE_EVICTION_WHEN_BUSY:-false}")
        self.assertEqual(environment["LOCALAI_DISABLE_WEBUI"], "${B1_LOCALAI_DISABLE_WEBUI:-true}")
        self.assertEqual(environment["LOCALAI_CORS"], "${B1_LOCALAI_CORS:-false}")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/localai:/srv/b1-ai-hub/models:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/localai/backends:/srv/b1-ai-hub/localai/backends", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/localai/configuration:/srv/b1-ai-hub/localai/configuration", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/localai/data:/srv/b1-ai-hub/localai/data", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", volumes)
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-f", "http://127.0.0.1:8080/readyz"])
        self.assertEqual(device["driver"], "${B1_LOCALAI_GPU_DRIVER:-nvidia}")
        self.assertEqual(device["count"], "${B1_LOCALAI_GPU_COUNT:-1}")
        self.assertEqual(device["capabilities"], ["gpu"])

    def test_production_localai_wrapper_uses_pinned_upstream_image_and_hooks(self) -> None:
        dockerfile = (ROOT / "deploy" / "localai" / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "deploy" / "localai" / "b1-localai-entrypoint.sh").read_text(encoding="utf-8")
        proxy = (ROOT / "deploy" / "localai" / "b1_localai_proxy.py").read_text(encoding="utf-8")

        self.assertIn(
            "FROM localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973",
            dockerfile,
        )
        self.assertIn("USER ${B1_LOCALAI_UID}:${B1_LOCALAI_GID}", dockerfile)
        self.assertIn("B1_LOCALAI_UPSTREAM_COMMIT=b224c96db6f4b87306a33a808650bfce63b12588", dockerfile)
        self.assertIn("B1_LOCALAI_UPSTREAM_IMAGE=", dockerfile)
        self.assertIn("python3 /usr/local/bin/b1_localai_proxy.py", entrypoint)
        self.assertIn('export LOCALAI_ADDRESS="${LOCALAI_ADDRESS:-${upstream_address}}"', entrypoint)
        for route in ("status", "build-info", "load", "warm", "smoke", "unload"):
            self.assertIn(f'if action == "{route}"', proxy)
        self.assertIn('"/backend/shutdown"', proxy)
        self.assertIn("LOCALAI_MAX_ACTIVE_BACKENDS must be 1", proxy)
        self.assertIn("model_count", proxy)

    def test_production_localai_override_points_control_plane_to_official_port(self) -> None:
        control_plane = self.production_localai_compose["services"]["control-plane"]

        self.assertEqual(control_plane["environment"]["LOCALAI_URL"], "http://localai:8080")

    def test_production_localai_override_resets_development_mock_fields(self) -> None:
        self.assertIn("B1_RUNTIME_KIND: !reset null", self.production_localai_text)
        self.assertIn("B1_RUNTIME_NAME: !reset null", self.production_localai_text)

    def test_production_comfyui_override_builds_pinned_b1_image(self) -> None:
        service = self.production_comfyui_compose["services"]["comfyui"]
        build_args = service["build"]["args"]
        environment = service["environment"]
        volumes = service["volumes"]
        device = service["deploy"]["resources"]["reservations"]["devices"][0]

        self.assertEqual(service["image"], "${B1_COMFYUI_IMAGE:-b1-ai-hub/comfyui:v0.3.77-b1}")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime"], "comfyui")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime.kind"], "comfyui")
        self.assertEqual(service["labels"]["b1.ai-hub.placeholder"], "false")
        self.assertEqual(service["build"]["context"], "./deploy/comfyui")
        self.assertEqual(build_args["B1_COMFYUI_VERSION"], "${B1_COMFYUI_VERSION:-v0.3.77}")
        self.assertEqual(build_args["B1_COMFYUI_COMMIT"], "${B1_COMFYUI_COMMIT:-59afc3984868289f808d02fa5cd180edfb2de240}")
        self.assertEqual(
            build_args["B1_COMFYUI_TARBALL_SHA256"],
            "${B1_COMFYUI_TARBALL_SHA256:-0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1}",
        )
        self.assertNotIn("ports", service)
        self.assertTrue(service["read_only"])
        self.assertIsNone(environment["B1_RUNTIME_KIND"])
        self.assertIsNone(environment["B1_RUNTIME_NAME"])
        self.assertEqual(environment["B1_COMFYUI_LISTEN"], "${B1_COMFYUI_LISTEN:-0.0.0.0}")
        self.assertEqual(environment["B1_COMFYUI_PORT"], "${B1_COMFYUI_PORT:-8188}")
        self.assertEqual(environment["B1_COMFYUI_RESERVE_VRAM_GIB"], "${B1_COMFYUI_RESERVE_VRAM_GIB:-1.0}")
        self.assertEqual(environment["B1_COMFYUI_DISABLE_API_NODES"], "${B1_COMFYUI_DISABLE_API_NODES:-true}")
        self.assertEqual(environment["B1_COMFYUI_CACHE_NONE"], "${B1_COMFYUI_CACHE_NONE:-true}")
        self.assertEqual(environment["B1_COMFYUI_HOOK_STRICT_MODEL_LIST"], "${B1_COMFYUI_HOOK_STRICT_MODEL_LIST:-false}")
        self.assertEqual(
            environment["B1_COMFYUI_HOOK_MODEL_FOLDERS"],
            "${B1_COMFYUI_HOOK_MODEL_FOLDERS:-checkpoints,diffusion_models,text_encoders,clip_vision,vae,loras,controlnet,upscale_models,embeddings}",
        )
        self.assertEqual(environment["B1_COMFYUI_HOOK_WARM_ENABLED"], "${B1_COMFYUI_HOOK_WARM_ENABLED:-false}")
        self.assertEqual(environment["B1_COMFYUI_HOOK_SMOKE_ENABLED"], "${B1_COMFYUI_HOOK_SMOKE_ENABLED:-false}")
        self.assertEqual(environment["B1_COMFYUI_HOOK_VERSION"], "${B1_COMFYUI_HOOK_VERSION:-b1-comfyui-hooks/v0.3.77-b1}")
        self.assertEqual(environment["B1_COMFYUI_UPSTREAM_VERSION"], "${B1_COMFYUI_VERSION:-v0.3.77}")
        self.assertEqual(environment["B1_COMFYUI_UPSTREAM_COMMIT"], "${B1_COMFYUI_COMMIT:-59afc3984868289f808d02fa5cd180edfb2de240}")
        self.assertEqual(
            environment["B1_COMFYUI_SOURCE_ARCHIVE_SHA256"],
            "${B1_COMFYUI_TARBALL_SHA256:-0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1}",
        )
        self.assertEqual(environment["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_REQUIRE_AUTH"], "${B1_RUNTIME_CONTROL_REQUIRE_AUTH:-true}")
        self.assertEqual(environment["B1_COMFYUI_HOOK_SMOKE_TIMEOUT_SECONDS"], "${B1_COMFYUI_HOOK_SMOKE_TIMEOUT_SECONDS:-30}")
        self.assertEqual(environment["B1_COMFYUI_HOOK_UNLOAD_IMMEDIATE_IDLE"], "${B1_COMFYUI_HOOK_UNLOAD_IMMEDIATE_IDLE:-true}")
        self.assertEqual(environment["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(environment["DO_NOT_TRACK"], "1")
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-fsS", "http://127.0.0.1:8188/system_stats"])
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/comfyui:/srv/b1-ai-hub/models:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/comfyui/input:/srv/b1-ai-hub/comfyui/input", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/comfyui/user:/srv/b1-ai-hub/comfyui/user", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/artifacts/temporary/comfyui-output:/srv/b1-ai-hub/comfyui/output", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/artifacts/temporary/comfyui-temp:/srv/b1-ai-hub/comfyui/temp", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/cache/comfyui:/srv/b1-ai-hub/cache", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", volumes)
        self.assertEqual(device["driver"], "${B1_COMFYUI_GPU_DRIVER:-nvidia}")
        self.assertEqual(device["count"], "${B1_COMFYUI_GPU_COUNT:-1}")
        self.assertEqual(device["capabilities"], ["gpu"])

    def test_production_comfyui_build_includes_b1_runtime_hooks(self) -> None:
        dockerfile = (ROOT / "deploy" / "comfyui" / "Dockerfile").read_text(encoding="utf-8")
        hooks = (ROOT / "deploy" / "comfyui" / "b1_runtime_hooks" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn("COPY b1_runtime_hooks /opt/comfyui/custom_nodes/b1_runtime_hooks", dockerfile)
        self.assertIn('@PromptServer.instance.routes.post("/b1/runtime/{action}")', hooks)
        self.assertIn('action == "status"', hooks)
        self.assertIn('action == "build-info"', hooks)
        self.assertIn('"actions": ["status", "build-info", "load", "warm", "smoke", "unload"]', hooks)
        self.assertIn("Comfy-Org/ComfyUI", hooks)
        self.assertIn("model_management.unload_all_models()", hooks)
        self.assertIn("B1RuntimeSmoke", hooks)
        self.assertIn("B1RuntimeTinyImage", hooks)

    def test_production_comfyui_installation_docs_include_status_hooks(self) -> None:
        installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")

        self.assertIn("POST /b1/runtime/status", installation)
        self.assertIn("/build-info", installation)
        self.assertIn("memory availability, model-folder counts, lifecycle capabilities", installation)
        self.assertIn("runtime lifecycle status", installation)

    def test_production_comfyui_override_points_control_plane_to_native_port(self) -> None:
        control_plane = self.production_comfyui_compose["services"]["control-plane"]

        self.assertEqual(control_plane["environment"]["COMFYUI_URL"], "http://comfyui:8188")

    def test_production_comfyui_override_resets_development_mock_fields_and_volumes(self) -> None:
        self.assertIn("B1_RUNTIME_KIND: !reset null", self.production_comfyui_text)
        self.assertIn("B1_RUNTIME_NAME: !reset null", self.production_comfyui_text)
        self.assertIn("volumes: !override", self.production_comfyui_text)

    def test_production_voicebox_override_builds_pinned_b1_image(self) -> None:
        service = self.production_voicebox_compose["services"]["voicebox"]
        build_args = service["build"]["args"]
        environment = service["environment"]
        volumes = service["volumes"]
        device = service["deploy"]["resources"]["reservations"]["devices"][0]

        self.assertEqual(service["image"], "${B1_VOICEBOX_IMAGE:-b1-ai-hub/voicebox:v0.5.0-b1}")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime"], "voicebox")
        self.assertEqual(service["labels"]["b1.ai-hub.runtime.kind"], "voicebox")
        self.assertEqual(service["labels"]["b1.ai-hub.placeholder"], "false")
        self.assertEqual(service["build"]["context"], "./deploy/voicebox")
        self.assertEqual(build_args["B1_VOICEBOX_VERSION"], "${B1_VOICEBOX_VERSION:-v0.5.0}")
        self.assertEqual(build_args["B1_VOICEBOX_COMMIT"], "${B1_VOICEBOX_COMMIT:-2bcb98d1a8b6fe05e15fbc1559e3085669e4035d}")
        self.assertEqual(
            build_args["B1_VOICEBOX_TARBALL_SHA256"],
            "${B1_VOICEBOX_TARBALL_SHA256:-d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083}",
        )
        self.assertEqual(build_args["B1_QWEN3_TTS_COMMIT"], "${B1_QWEN3_TTS_COMMIT:-022e286b98fbec7e1e916cb940cdf532cd9f488e}")
        self.assertEqual(build_args["B1_LINACODEC_COMMIT"], "${B1_LINACODEC_COMMIT:-c0ae7c7285e121475c27592cfbb600624b714290}")
        self.assertEqual(build_args["B1_LUXTTS_COMMIT"], "${B1_LUXTTS_COMMIT:-28ae6a61151684fffc9d1a7aa15eafa02286fe0b}")
        self.assertNotIn("ports", service)
        self.assertTrue(service["read_only"])
        self.assertIsNone(environment["B1_RUNTIME_KIND"])
        self.assertIsNone(environment["B1_RUNTIME_NAME"])
        self.assertEqual(environment["B1_VOICEBOX_HOST"], "${B1_VOICEBOX_HOST:-0.0.0.0}")
        self.assertEqual(environment["B1_VOICEBOX_PORT"], "${B1_VOICEBOX_PORT:-17493}")
        self.assertEqual(environment["B1_VOICEBOX_UPSTREAM_HOST"], "${B1_VOICEBOX_UPSTREAM_HOST:-127.0.0.1}")
        self.assertEqual(environment["B1_VOICEBOX_UPSTREAM_PORT"], "${B1_VOICEBOX_UPSTREAM_PORT:-17494}")
        self.assertEqual(environment["B1_VOICEBOX_DATA_DIR"], "/srv/b1-ai-hub/voicebox")
        self.assertEqual(environment["B1_VOICEBOX_MODELS_DIR"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["VOICEBOX_MODELS_DIR"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["B1_VOICEBOX_HOOK_STRICT_MODEL_LIST"], "${B1_VOICEBOX_HOOK_STRICT_MODEL_LIST:-false}")
        self.assertEqual(environment["B1_VOICEBOX_HOOK_MODEL_ROOTS"], "${B1_VOICEBOX_HOOK_MODEL_ROOTS:-/srv/b1-ai-hub/models}")
        self.assertEqual(environment["B1_VOICEBOX_HOOK_WARM_ENABLED"], "${B1_VOICEBOX_HOOK_WARM_ENABLED:-false}")
        self.assertEqual(environment["B1_VOICEBOX_HOOK_SMOKE_ENABLED"], "${B1_VOICEBOX_HOOK_SMOKE_ENABLED:-false}")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_TOKEN_FILE"], "/run/secrets/runtime_control_token")
        self.assertEqual(environment["B1_RUNTIME_CONTROL_REQUIRE_AUTH"], "${B1_RUNTIME_CONTROL_REQUIRE_AUTH:-true}")
        self.assertEqual(environment["B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD"], "${B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD:-true}")
        self.assertEqual(environment["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "${B1_VOICEBOX_HF_HUB_OFFLINE:-1}")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "${B1_VOICEBOX_TRANSFORMERS_OFFLINE:-1}")
        self.assertEqual(environment["DO_NOT_TRACK"], "1")
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-fsS", "http://127.0.0.1:17493/health"])
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/voicebox:/srv/b1-ai-hub/voicebox", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/voicebox:/srv/b1-ai-hub/models:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/cache/voicebox:/srv/b1-ai-hub/cache", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", volumes)
        self.assertEqual(device["driver"], "${B1_VOICEBOX_GPU_DRIVER:-nvidia}")
        self.assertEqual(device["count"], "${B1_VOICEBOX_GPU_COUNT:-1}")
        self.assertEqual(device["capabilities"], ["gpu"])

    def test_production_voicebox_override_points_control_plane_to_native_port(self) -> None:
        control_plane = self.production_voicebox_compose["services"]["control-plane"]

        self.assertEqual(control_plane["environment"]["VOICEBOX_URL"], "http://voicebox:17493")

    def test_production_voicebox_override_resets_development_mock_fields_and_volumes(self) -> None:
        self.assertIn("B1_RUNTIME_KIND: !reset null", self.production_voicebox_text)
        self.assertIn("B1_RUNTIME_NAME: !reset null", self.production_voicebox_text)
        self.assertIn("volumes: !override", self.production_voicebox_text)

    def test_production_voicebox_build_uses_resolved_constraints(self) -> None:
        dockerfile = (ROOT / "deploy" / "voicebox" / "Dockerfile").read_text(encoding="utf-8")
        constraints = (ROOT / "deploy" / "voicebox" / "constraints.txt").read_text(encoding="utf-8")
        readme = (ROOT / "deploy" / "voicebox" / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "voicebox.md").read_text(encoding="utf-8")

        self.assertIn("COPY constraints.txt /tmp/voicebox-constraints.txt", dockerfile)
        self.assertIn("-c /tmp/voicebox-constraints.txt", dockerfile)
        self.assertIn("qwen-tts @ git+https://github.com/QwenLM/Qwen3-TTS.git@", dockerfile)
        self.assertIn("#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85", dockerfile)
        self.assertIn("build-essential", dockerfile)
        self.assertIn('direct_cangjie_file = Path(model_dir) / "Cangjie5_TC.json"', dockerfile)
        self.assertIn("torch==2.13.0", constraints)
        self.assertIn("chatterbox-tts==0.1.7", constraints)
        for text in (readme, docs):
            self.assertIn("spacy_ontonotes", text)
            self.assertIn("$B1_DATA_ROOT/models/runtime-views/voicebox/models--ResembleAI--chatterbox/", text)
            self.assertIn("Cangjie", text)

    def test_production_voicebox_build_includes_b1_runtime_proxy(self) -> None:
        dockerfile = (ROOT / "deploy" / "voicebox" / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "deploy" / "voicebox" / "b1-voicebox-entrypoint.sh").read_text(encoding="utf-8")
        proxy = (ROOT / "deploy" / "voicebox" / "b1_voicebox_proxy.py").read_text(encoding="utf-8")

        self.assertIn("COPY b1_voicebox_proxy.py /usr/local/bin/b1_voicebox_proxy.py", dockerfile)
        self.assertIn("python /usr/local/bin/b1_voicebox_proxy.py", entrypoint)
        self.assertIn('"python", "-m", "backend.main"', proxy)
        self.assertIn('"--data-dir", data_dir', proxy)
        for route in ("load", "warm", "smoke", "unload"):
            self.assertIn(f'if action == "{route}"', proxy)
        self.assertIn("websocket_proxy", proxy)
        self.assertIn("upstream_process_restart", proxy)

    def test_control_plane_mounts_workflow_seeds_read_only(self) -> None:
        service = self.compose["services"]["control-plane"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertTrue(any("./workflows:/opt/b1/workflows:ro" == volume for volume in volumes))
        self.assertEqual(environment["B1_WORKFLOW_SEED_DIR"], "/opt/b1/workflows/approved")
        self.assertEqual(environment["B1_COMFYUI_NODE_PIN_REGISTRY"], "/opt/b1/workflows/approved-node-pins.json")

    def test_models_virtual_host_routes_through_control_plane(self) -> None:
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        models_block_start = caddyfile.index("{$B1_HOST_MODELS")
        models_block_end = caddyfile.index("{$B1_HOST_COMFY", models_block_start)
        models_block = caddyfile[models_block_start:models_block_end]
        self.assertIn("reverse_proxy control-plane:8000", models_block)
        self.assertNotIn("reverse_proxy artifact-server:8000", models_block)

    def test_gateway_strips_spoofed_compatibility_headers_except_managed_hosts(self) -> None:
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        for host_var in ("B1_HOST_API", "B1_HOST_MODELS"):
            block_start = caddyfile.index("{$" + host_var)
            block_end = caddyfile.find("\n}\n", block_start)
            block = caddyfile[block_start:block_end]
            self.assertIn("header_up -X-B1-Compatibility", block, host_var)
        comfy_block_start = caddyfile.index("{$B1_HOST_COMFY")
        comfy_block_end = caddyfile.index("{$B1_HOST_VOICE", comfy_block_start)
        comfy_block = caddyfile[comfy_block_start:comfy_block_end]
        voice_block_start = caddyfile.index("{$B1_HOST_VOICE")
        voice_block_end = caddyfile.find("\n}\n", voice_block_start)
        voice_block = caddyfile[voice_block_start:voice_block_end]
        self.assertIn("header_up X-B1-Compatibility comfyui-native", comfy_block)
        self.assertIn("header_up X-B1-Compatibility voicebox-native", voice_block)

    def test_legacy_comfy_listener_is_not_in_base_caddyfile(self) -> None:
        caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertNotIn(":8188", caddyfile)
        self.assertNotIn("comfyui-legacy-8188", caddyfile)

    def test_legacy_comfy_override_enables_listener_explicitly(self) -> None:
        gateway = self.legacy_compose["services"]["gateway"]
        legacy_caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile.legacy-comfy").read_text(encoding="utf-8")
        self.assertEqual(gateway["profiles"], ["legacy-comfy"])
        self.assertIn("/etc/caddy/Caddyfile.legacy-comfy", gateway["command"])
        self.assertTrue(any("Caddyfile.legacy-comfy" in volume for volume in gateway["volumes"]))
        self.assertEqual(gateway["environment"]["B1_LEGACY_COMFY_ALLOW_CIDRS"], "${B1_LEGACY_COMFY_ALLOW_CIDRS:-192.168.2.0/24 100.64.0.0/10}")
        self.assertEqual(gateway["ports"], ["${B1_LEGACY_COMFY_PUBLISH:-8188:8188}"])
        self.assertNotIn("192.168.2.100", gateway["ports"][0])
        self.assertNotIn("B1_LEGACY_COMFY_BIND", gateway["ports"][0])
        self.assertIn(":8188", legacy_caddyfile)
        self.assertIn("remote_ip {$B1_LEGACY_COMFY_ALLOW_CIDRS", legacy_caddyfile)
        self.assertIn("header_up -Authorization", legacy_caddyfile)
        self.assertIn("header_up -Cookie", legacy_caddyfile)
        self.assertIn("header_up -X-B1-CSRF", legacy_caddyfile)
        self.assertIn("header_up X-B1-Compatibility comfyui-legacy-8188", legacy_caddyfile)

    def test_only_runtime_agent_mounts_docker_socket(self) -> None:
        for name, service in self.compose["services"].items():
            volumes = service.get("volumes", [])
            has_socket = any("/var/run/docker.sock" in volume for volume in volumes)
            self.assertEqual(has_socket, name == "runtime-agent", name)

    def test_runtime_agent_mounts_generated_token_read_only(self) -> None:
        service = self.compose["services"]["runtime-agent"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertTrue(any("/run/secrets:ro" in volume for volume in volumes))
        self.assertEqual(environment["B1_RUNTIME_AGENT_TOKEN_FILE"], "/run/secrets/runtime_agent_token")
        self.assertEqual(environment["B1_RUNTIME_AGENT_MTLS_ENABLED"], "${B1_RUNTIME_AGENT_MTLS_ENABLED:-true}")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_CERT_FILE"], "/run/secrets/runtime_agent_server.crt")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_KEY_FILE"], "/run/secrets/runtime_agent_server.key")
        self.assertEqual(environment["B1_RUNTIME_AGENT_TLS_CA_FILE"], "/run/secrets/runtime_agent_mtls_ca.crt")
        self.assertEqual(environment["B1_RUNTIME_AGENT_CLIENT_CERT_FILE"], "/run/secrets/runtime_agent_client.crt")
        self.assertEqual(environment["B1_RUNTIME_AGENT_CLIENT_KEY_FILE"], "/run/secrets/runtime_agent_client.key")
        self.assertEqual(environment["B1_RUNTIME_AGENT_CLIENT_CERT_REQUIRED"], "${B1_RUNTIME_AGENT_CLIENT_CERT_REQUIRED:-true}")
        self.assertEqual(service["gpus"], "all")
        self.assertEqual(environment["NVIDIA_VISIBLE_DEVICES"], "${B1_RUNTIME_AGENT_NVIDIA_VISIBLE_DEVICES:-all}")
        self.assertEqual(environment["NVIDIA_DRIVER_CAPABILITIES"], "${B1_RUNTIME_AGENT_NVIDIA_DRIVER_CAPABILITIES:-utility}")
        self.assertEqual(environment["B1_METRIC_PATHS"], "/srv/b1-ai-hub,/tmp")
        self.assertEqual(environment["B1_ENABLE_MUTATIONS"], "${B1_ENABLE_MUTATIONS:-false}")
        self.assertEqual(environment["B1_RUNTIME_AGENT_MUTATION_RATE_LIMIT_PER_MINUTE"], "${B1_RUNTIME_AGENT_MUTATION_RATE_LIMIT_PER_MINUTE:-12}")
        self.assertEqual(environment["B1_RUNTIME_ACTION_SERVICES"], "${B1_RUNTIME_ACTION_SERVICES:-localai,comfyui,voicebox,audio-cpu}")
        self.assertEqual(
            environment["B1_ROLLBACK_SERVICES"],
            "${B1_ROLLBACK_SERVICES:-localai,comfyui,voicebox,audio-cpu,artifact-server,open-webui,gateway}",
        )
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}:/srv/b1-ai-hub:ro", volumes)
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "python", "-m", "app.healthcheck"])


if __name__ == "__main__":
    unittest.main()
