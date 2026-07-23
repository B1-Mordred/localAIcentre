from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


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


ComposePolicyLoader.add_constructor("!reset", _compose_reset)
ComposePolicyLoader.add_constructor("!override", _compose_override)


class ComposePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.load((ROOT / "compose.yaml").read_text(encoding="utf-8"), Loader=ComposePolicyLoader)
        cls.legacy_compose = yaml.load((ROOT / "compose.legacy-comfy.yaml").read_text(encoding="utf-8"), Loader=ComposePolicyLoader)
        cls.production_localai_text = (ROOT / "compose.production-localai.yaml").read_text(encoding="utf-8")
        cls.production_comfyui_text = (ROOT / "compose.production-comfyui.yaml").read_text(encoding="utf-8")
        cls.production_voicebox_text = (ROOT / "compose.production-voicebox.yaml").read_text(encoding="utf-8")
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
        required = {
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
        self.assertTrue(required.issubset(services), required - services)

    def test_open_webui_uses_generated_internal_api_key(self) -> None:
        open_webui = self.compose["services"]["open-webui"]
        control_plane = self.compose["services"]["control-plane"]
        self.assertNotIn("OPENAI_API_KEY", open_webui.get("environment", {}))
        self.assertEqual(open_webui["build"]["context"], "./deploy/open-webui")
        self.assertEqual(open_webui["environment"]["B1_OPEN_WEBUI_API_KEY_FILE"], "/run/secrets/open_webui_api_key")
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/secrets:/run/secrets:ro", open_webui.get("volumes", []))
        self.assertIn("bootstrap", open_webui.get("depends_on", {}))
        self.assertEqual(control_plane["environment"]["B1_OPEN_WEBUI_API_KEY_FILE"], "/run/secrets/open_webui_api_key")
        dockerfile = (ROOT / "deploy" / "open-webui" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/open-webui/open-webui:v0.10.2@sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4", dockerfile)

    def test_no_latest_image_tags(self) -> None:
        for name, service in self.compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertNotEqual(image.split(":")[-1], "latest", name)
        for name, service in self.production_localai_compose["services"].items():
            image = service.get("image")
            if not image:
                continue
            self.assertIn("@sha256:", image, name)
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

    def test_only_gateway_publishes_ports(self) -> None:
        for name, service in self.compose["services"].items():
            if name == "gateway":
                continue
            self.assertNotIn("ports", service, name)

    def test_internal_networks_exist(self) -> None:
        networks = self.compose["networks"]
        for name in ("app", "data", "runtime"):
            self.assertTrue(networks[name].get("internal"), name)

    def test_bootstrap_precedes_stateful_services(self) -> None:
        for name in ("postgres", "redis", "control-plane", "runtime-agent"):
            depends_on = self.compose["services"][name].get("depends_on", {})
            self.assertIn("bootstrap", depends_on, name)

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
        self.assertEqual(environment["B1_DEV_AUTH_BYPASS"], "${B1_DEV_AUTH_BYPASS:-false}")
        self.assertEqual(environment["B1_OPENAI_COMPATIBLE_BASE_URL"], "${B1_OPENAI_COMPATIBLE_BASE_URL:-}")
        self.assertEqual(environment["B1_OPENAI_COMPATIBLE_API_KEY_FILE"], "${B1_OPENAI_COMPATIBLE_API_KEY_FILE:-}")
        self.assertEqual(environment["B1_GENERIC_HTTP_BASE_URL"], "${B1_GENERIC_HTTP_BASE_URL:-}")
        self.assertEqual(environment["B1_SESSION_COOKIE_SECURE"], "${B1_SESSION_COOKIE_SECURE:-true}")
        self.assertEqual(environment["B1_SESSION_TTL_SECONDS"], "${B1_SESSION_TTL_SECONDS:-28800}")
        self.assertIn("B1_CORS_ALLOW_ORIGINS", environment)

    def test_frontends_receive_configured_api_host_at_build_time(self) -> None:
        expected = "https://${B1_HOST_API:-api.ai.b1.germering}"
        self.assertEqual(self.compose["services"]["control-center"]["build"]["args"]["VITE_B1_API_BASE"], expected)
        self.assertEqual(self.compose["services"]["media-studio"]["build"]["args"]["VITE_B1_API_BASE"], expected)

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

    def test_control_plane_backup_mounts_are_b1_root_scoped(self) -> None:
        service = self.compose["services"]["control-plane"]
        volumes = service.get("volumes", [])
        environment = service.get("environment", {})
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}:/srv/b1-ai-hub:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/control-plane:/srv/b1-ai-hub/data/control-plane", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/backups:/srv/b1-ai-hub/backups", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/restore-tests:/srv/b1-ai-hub/restore-tests", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views:/srv/b1-ai-hub/models/runtime-views", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/quarantine:/srv/b1-ai-hub/models/quarantine", volumes)
        self.assertEqual(environment["B1_DATA_ROOT"], "/srv/b1-ai-hub")
        self.assertEqual(environment["B1_BACKUP_ROOT"], "/srv/b1-ai-hub/backups")
        self.assertEqual(environment["B1_RESTORE_TEST_ROOT"], "/srv/b1-ai-hub/restore-tests")
        self.assertEqual(environment["B1_RUNTIME_DEPLOYMENT_MODE"], "${B1_RUNTIME_DEPLOYMENT_MODE:-development}")
        self.assertEqual(environment["B1_RUNTIME_PRODUCTION_REQUIRED"], "${B1_RUNTIME_PRODUCTION_REQUIRED:-localai comfyui audio-cpu}")

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
        self.assertEqual(environment["B1_CPU_AUDIO_MODEL_ROOT"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["B1_PIPER_BINARY"], "${B1_PIPER_BINARY:-/opt/piper/piper}")
        self.assertEqual(environment["B1_PIPER_MODEL_PATH"], "${B1_PIPER_MODEL_PATH:-}")

    def test_production_localai_override_uses_pinned_official_image(self) -> None:
        service = self.production_localai_compose["services"]["localai"]
        environment = service["environment"]
        volumes = service["volumes"]
        device = service["deploy"]["resources"]["reservations"]["devices"][0]

        self.assertIsNone(service["build"])
        self.assertEqual(
            service["image"],
            "${B1_LOCALAI_IMAGE:-localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973}",
        )
        self.assertNotIn("ports", service)
        self.assertFalse(service["read_only"])
        self.assertIsNone(environment["B1_RUNTIME_KIND"])
        self.assertIsNone(environment["B1_RUNTIME_NAME"])
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
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-f", "http://127.0.0.1:8080/readyz"])
        self.assertEqual(device["driver"], "${B1_LOCALAI_GPU_DRIVER:-nvidia.com/gpu}")
        self.assertEqual(device["count"], "${B1_LOCALAI_GPU_COUNT:-1}")
        self.assertEqual(device["capabilities"], ["gpu"])

    def test_production_localai_override_points_control_plane_to_official_port(self) -> None:
        control_plane = self.production_localai_compose["services"]["control-plane"]

        self.assertEqual(control_plane["environment"]["LOCALAI_URL"], "http://localai:8080")

    def test_production_localai_override_resets_development_mock_fields(self) -> None:
        self.assertIn("build: !reset null", self.production_localai_text)
        self.assertIn("B1_RUNTIME_KIND: !reset null", self.production_localai_text)
        self.assertIn("B1_RUNTIME_NAME: !reset null", self.production_localai_text)

    def test_production_comfyui_override_builds_pinned_b1_image(self) -> None:
        service = self.production_comfyui_compose["services"]["comfyui"]
        build_args = service["build"]["args"]
        environment = service["environment"]
        volumes = service["volumes"]
        device = service["deploy"]["resources"]["reservations"]["devices"][0]

        self.assertEqual(service["image"], "${B1_COMFYUI_IMAGE:-b1-ai-hub/comfyui:v0.3.77-b1}")
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
        self.assertEqual(environment["B1_COMFYUI_RESERVE_VRAM_GIB"], "${B1_COMFYUI_RESERVE_VRAM_GIB:-1.5}")
        self.assertEqual(environment["B1_COMFYUI_DISABLE_API_NODES"], "${B1_COMFYUI_DISABLE_API_NODES:-true}")
        self.assertEqual(environment["B1_COMFYUI_CACHE_NONE"], "${B1_COMFYUI_CACHE_NONE:-true}")
        self.assertEqual(environment["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(environment["DO_NOT_TRACK"], "1")
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-fsS", "http://127.0.0.1:8188/system_stats"])
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/comfyui:/srv/b1-ai-hub/models:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/comfyui/input:/srv/b1-ai-hub/comfyui/input", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/comfyui/user:/srv/b1-ai-hub/comfyui/user", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/artifacts/temporary/comfyui-output:/srv/b1-ai-hub/comfyui/output", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/artifacts/temporary/comfyui-temp:/srv/b1-ai-hub/comfyui/temp", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/cache/comfyui:/srv/b1-ai-hub/cache", volumes)
        self.assertEqual(device["driver"], "${B1_COMFYUI_GPU_DRIVER:-nvidia.com/gpu}")
        self.assertEqual(device["count"], "${B1_COMFYUI_GPU_COUNT:-1}")
        self.assertEqual(device["capabilities"], ["gpu"])

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
        self.assertEqual(environment["B1_VOICEBOX_DATA_DIR"], "/srv/b1-ai-hub/voicebox")
        self.assertEqual(environment["B1_VOICEBOX_MODELS_DIR"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["VOICEBOX_MODELS_DIR"], "/srv/b1-ai-hub/models")
        self.assertEqual(environment["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "${B1_VOICEBOX_HF_HUB_OFFLINE:-1}")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "${B1_VOICEBOX_TRANSFORMERS_OFFLINE:-1}")
        self.assertEqual(environment["DO_NOT_TRACK"], "1")
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "curl", "-fsS", "http://127.0.0.1:17493/health"])
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/data/voicebox:/srv/b1-ai-hub/voicebox", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/models/runtime-views/voicebox:/srv/b1-ai-hub/models:ro", volumes)
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}/cache/voicebox:/srv/b1-ai-hub/cache", volumes)
        self.assertEqual(device["driver"], "${B1_VOICEBOX_GPU_DRIVER:-nvidia.com/gpu}")
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

        self.assertIn("COPY constraints.txt /tmp/voicebox-constraints.txt", dockerfile)
        self.assertIn("-c /tmp/voicebox-constraints.txt", dockerfile)
        self.assertIn("qwen-tts @ git+https://github.com/QwenLM/Qwen3-TTS.git@", dockerfile)
        self.assertIn("#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85", dockerfile)
        self.assertIn("torch==2.13.0", constraints)
        self.assertIn("chatterbox-tts==0.1.7", constraints)

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
        self.assertEqual(gateway["ports"], ["${B1_LEGACY_COMFY_BIND:-192.168.2.100}:${B1_LEGACY_COMFY_PORT:-8188}:8188"])
        self.assertIn(":8188", legacy_caddyfile)
        self.assertIn("remote_ip {$B1_LEGACY_COMFY_ALLOW_CIDRS", legacy_caddyfile)
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
        self.assertEqual(environment["B1_METRIC_PATHS"], "/srv/b1-ai-hub,/tmp")
        self.assertEqual(environment["B1_ENABLE_MUTATIONS"], "${B1_ENABLE_MUTATIONS:-false}")
        self.assertEqual(environment["B1_RUNTIME_ACTION_SERVICES"], "${B1_RUNTIME_ACTION_SERVICES:-localai,comfyui,voicebox,audio-cpu}")
        self.assertEqual(
            environment["B1_ROLLBACK_SERVICES"],
            "${B1_ROLLBACK_SERVICES:-localai,comfyui,voicebox,audio-cpu,artifact-server,open-webui,gateway}",
        )
        self.assertIn("${B1_DATA_ROOT:-/srv/b1-ai-hub}:/srv/b1-ai-hub:ro", volumes)
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "python", "-m", "app.healthcheck"])


if __name__ == "__main__":
    unittest.main()
