from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUDIO_CPU_APP_ROOT = ROOT / "services" / "audio-cpu" / "app"

try:
    audio_cpu_pkg_spec = importlib.util.spec_from_file_location(
        "audio_cpu_app",
        AUDIO_CPU_APP_ROOT / "__init__.py",
        submodule_search_locations=[str(AUDIO_CPU_APP_ROOT)],
    )
    if audio_cpu_pkg_spec is None or audio_cpu_pkg_spec.loader is None:
        raise ModuleNotFoundError("audio_cpu_app")
    audio_cpu_pkg = importlib.util.module_from_spec(audio_cpu_pkg_spec)
    sys.modules["audio_cpu_app"] = audio_cpu_pkg
    audio_cpu_pkg_spec.loader.exec_module(audio_cpu_pkg)
    audio_cpu_main_spec = importlib.util.spec_from_file_location("audio_cpu_app.main", AUDIO_CPU_APP_ROOT / "main.py")
    if audio_cpu_main_spec is None or audio_cpu_main_spec.loader is None:
        raise ModuleNotFoundError("audio_cpu_app.main")
    audio_cpu_main = importlib.util.module_from_spec(audio_cpu_main_spec)
    sys.modules["audio_cpu_app.main"] = audio_cpu_main
    audio_cpu_main_spec.loader.exec_module(audio_cpu_main)
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "fastapi":
        raise
    audio_cpu_main = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeRequest:
    def __init__(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    async def json(self) -> dict[str, Any]:
        return self.payload


@unittest.skipIf(audio_cpu_main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AudioCpuRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_env(
            {
                "B1_CPU_AUDIO_ENGINE": None,
                "B1_CPU_EMBEDDING_ENGINE": None,
                "B1_CPU_STT_ENGINE": None,
                "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": None,
                "B1_CPU_AUDIO_MODEL_ROOT": None,
                "B1_ONNX_EMBEDDING_MODEL_ROOT": None,
                "B1_VOSK_STT_MODEL_ROOT": None,
                "B1_VOSK_STT_MODEL_PATH": None,
                "B1_PIPER_BINARY": None,
                "B1_PIPER_MODEL_PATH": None,
                "B1_PIPER_CONFIG_PATH": None,
                "B1_RUNTIME_CONTROL_TOKEN": None,
                "B1_RUNTIME_CONTROL_TOKEN_FILE": None,
                "B1_RUNTIME_CONTROL_REQUIRE_AUTH": None,
                "B1_CPU_RESIDENCY_ENABLED": None,
                "B1_CPU_RESIDENCY_MAX_RAM_GIB": None,
                "B1_CPU_RESIDENT_ALIASES": None,
                "B1_HOST_TOTAL_RAM_GIB": None,
                "B1_HOST_RESERVE_RAM_GIB": None,
                "B1_AUDIO_CPU_RUNTIME_VERSION": None,
                "B1_AUDIO_CPU_BASE_IMAGE": None,
                "B1_AUDIO_CPU_PIPER_RELEASE": None,
                "B1_AUDIO_CPU_PIPER_ASSET": None,
                "B1_AUDIO_CPU_PIPER_SHA256": None,
            }
        )
        audio_cpu_main.clear_resident_caches()

    def patch_env(self, values: dict[str, str | None]) -> None:
        original = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        def restore() -> None:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(audio_cpu_main, name)
        setattr(audio_cpu_main, name, value)
        self.addCleanup(lambda: setattr(audio_cpu_main, name, original))

    def fake_piper(self, root: Path) -> Path:
        binary = root / "fake-piper.py"
        binary.write_text(
            """#!/usr/bin/env python3
import sys
import wave
from pathlib import Path

output = Path(sys.argv[sys.argv.index("--output_file") + 1])
sys.stdin.read()
with wave.open(str(output), "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b"\\x00\\x00" * 4000)
""",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        self.patch_attr("resolve_executable", lambda value: str(Path(value).resolve()) if value else None)
        self.patch_attr("subprocess", SimpleNamespace(run=self.fake_piper_run, TimeoutExpired=audio_cpu_main.subprocess.TimeoutExpired))
        return binary

    def fake_piper_run(self, command: list[str], **_: Any) -> Any:
        output = Path(command[command.index("--output_file") + 1])
        output.write_bytes(audio_cpu_main.silence_wav(0.25))
        return SimpleNamespace(returncode=0)

    def test_health_exposes_placeholder_engine_policy(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})
        health = asyncio.run(audio_cpu_main.healthz())

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["engine"], "scaffold")
        self.assertTrue(health["placeholder"])
        self.assertTrue(health["placeholder_enabled"])
        self.assertEqual(health["capabilities"], {"speech": True, "transcription": True, "embeddings": True})
        residency = health["details"]["cpu_residency"]
        self.assertTrue(residency["enabled"])
        self.assertEqual(residency["max_ram_gib"], 2.0)
        self.assertEqual(residency["resident_aliases"], ["embedding-default", "tts-fast", "stt-default"])
        self.assertEqual(residency["headroom"]["reserve_ram_gib"], 6.0)
        self.assertEqual(residency["headroom"]["total_ram_gib"], 32.0)
        self.assertIn(residency["headroom"]["reason"], {"ok", "mem_available_unmeasured", "host_ram_below_reserve"})
        self.assertEqual(
            residency["cache"],
            {
                "vosk": {"maxsize": 2, "currsize": 0},
                "onnx_embedding": {"maxsize": 4, "currsize": 0},
            },
        )

    def test_speech_marks_scaffold_output_as_placeholder(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})
        response = asyncio.run(audio_cpu_main.speech(FakeRequest({"input": "hello"})))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "audio/wav")
        self.assertEqual(response.headers["X-B1-Placeholder"], "true")
        self.assertEqual(response.headers["X-B1-CPU-Audio-Engine"], "scaffold")
        self.assertGreater(len(response.body), 44)

    def test_transcription_marks_scaffold_result_as_placeholder(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})
        response = asyncio.run(audio_cpu_main.transcription(FakeRequest({"audio": "UklGRg=="})))

        self.assertIn("CPU transcription scaffold", response["text"])
        self.assertFalse(response["gpu_lease_required"])
        self.assertEqual(response["b1_engine"], "scaffold")
        self.assertTrue(response["b1_placeholder"])

    def test_embeddings_are_deterministic_bounded_and_nonzero(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})
        first = asyncio.run(audio_cpu_main.embeddings(FakeRequest({"model": "embedding-default", "input": ["hello", "world"], "dimensions": 16})))
        second = asyncio.run(audio_cpu_main.embeddings(FakeRequest({"model": "embedding-default", "input": ["hello"], "dimensions": 16})))

        self.assertEqual(first["object"], "list")
        self.assertEqual(first["model"], "embedding-default")
        self.assertFalse(first["gpu_lease_required"])
        self.assertEqual(len(first["data"]), 2)
        self.assertEqual(len(first["data"][0]["embedding"]), 16)
        self.assertNotEqual(first["data"][0]["embedding"], [0.0] * 16)
        self.assertEqual(first["data"][0]["embedding"], second["data"][0]["embedding"])
        self.assertNotEqual(first["data"][0]["embedding"], first["data"][1]["embedding"])
        self.assertTrue(first["b1_placeholder"])
        self.assertEqual(first["b1_engine"], "scaffold")

    def test_runtime_smoke_exercises_cpu_path_without_gpu_lease(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})

        speech = asyncio.run(
            audio_cpu_main.runtime_smoke(
                FakeRequest(
                    {
                        "model": "b1-cpu-placeholder-tts",
                        "model_alias": "tts-fast",
                        "resolved_model_version": "b1-cpu-placeholder-tts@0.1.0",
                        "modality": "tts",
                    }
                )
            )
        )
        embedding = asyncio.run(audio_cpu_main.runtime_smoke(FakeRequest({"model": "b1-cpu-placeholder-embedding", "modality": "embedding"})))

        self.assertEqual(speech["status"], "ok")
        self.assertFalse(speech["gpu_lease_required"])
        self.assertEqual(speech["measurements"]["peak_vram_mib"], 0)
        self.assertGreater(speech["measurements"]["peak_ram_mib"], 0)
        self.assertGreater(speech["measurements"]["speech_bytes"], 44)
        self.assertEqual(embedding["measurements"]["embedding_dimensions"], 16)
        self.assertTrue(embedding["measurements"]["embedding_nonzero"])

    def test_runtime_smoke_requires_configured_runtime_control_token(self) -> None:
        self.patch_env(
            {
                "B1_CPU_AUDIO_ENGINE": "scaffold",
                "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true",
                "B1_RUNTIME_CONTROL_TOKEN": "hook-token",
                "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true",
            }
        )

        missing = asyncio.run(audio_cpu_main.runtime_smoke(FakeRequest({"model": "b1-cpu-placeholder-tts", "modality": "tts"})))
        accepted = asyncio.run(
            audio_cpu_main.runtime_smoke(
                FakeRequest(
                    {"model": "b1-cpu-placeholder-tts", "modality": "tts"},
                    headers={"Authorization": "Bearer hook-token"},
                )
            )
        )

        self.assertEqual(missing.status_code, 401)
        self.assertIn(b"runtime_control_token_required", missing.body)
        self.assertEqual(accepted["status"], "ok")
        self.assertFalse(accepted["gpu_lease_required"])

    def test_runtime_build_info_reports_pinned_cpu_audio_metadata(self) -> None:
        self.patch_env({"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"})

        missing = asyncio.run(audio_cpu_main.runtime_build_info(FakeRequest({}, headers={})))
        accepted = asyncio.run(audio_cpu_main.runtime_build_info(FakeRequest({}, headers={"Authorization": "Bearer hook-token"})))

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted["status"], "ok")
        self.assertEqual(accepted["runtime"], "audio-cpu")
        self.assertEqual(accepted["action"], "build-info")
        self.assertEqual(accepted["component"], "b1-audio-cpu")
        self.assertTrue(accepted["pinned"])
        self.assertIn("@sha256:", accepted["base_image"])
        self.assertEqual(accepted["piper_release"], "2023.11.14-2")
        self.assertEqual(accepted["piper_asset_sha256"], "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992")
        self.assertEqual(accepted["capabilities"]["actions"], ["status", "build-info", "smoke", "unload"])
        self.assertFalse(accepted["capabilities"]["gpu_lease_required"])

    def test_runtime_status_reports_real_engines_without_raw_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            piper_view = model_root / "b1-piper-en-us-amy-low" / "v1"
            piper_view.mkdir(parents=True)
            (piper_view / "voice.onnx").write_bytes(b"voice")
            (piper_view / "voice.onnx.json").write_text("{}", encoding="utf-8")
            embedding_view = model_root / "b1-minilm-l6-v2-onnx-q4" / "v1"
            embedding_view.mkdir(parents=True)
            (embedding_view / "model.onnx").write_bytes(b"embedding")
            (embedding_view / "tokenizer.json").write_text("{}", encoding="utf-8")
            vosk_view = model_root / "b1-vosk-small-en-us-0.15" / "v1" / "vosk-model-small-en-us-0.15"
            (vosk_view / "am").mkdir(parents=True)
            (vosk_view / "conf").mkdir()
            (vosk_view / "graph").mkdir()
            (vosk_view / "am" / "final.mdl").write_bytes(b"vosk")
            (vosk_view / "conf" / "model.conf").write_text("--sample-frequency=16000\n", encoding="utf-8")
            (vosk_view / "graph" / "HCLr.fst").write_bytes(b"graph")
            binary = self.fake_piper(root)
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_EMBEDDING_ENGINE": "onnx",
                    "B1_CPU_STT_ENGINE": "vosk",
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_ONNX_EMBEDDING_MODEL_ROOT": str(model_root),
                    "B1_VOSK_STT_MODEL_ROOT": str(model_root),
                    "B1_PIPER_BINARY": str(binary),
                    "B1_PIPER_MODEL_PATH": None,
                    "B1_PIPER_CONFIG_PATH": None,
                }
            )
            self.patch_attr("onnx_embedding_dependency_status", lambda: [])
            self.patch_attr("vosk_stt_dependency_status", lambda: [])
            self.patch_attr("meminfo_available_ram_gib", lambda path="/proc/meminfo": 16.0)

            status = asyncio.run(audio_cpu_main.runtime_status(FakeRequest({})))

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["runtime"], "audio-cpu")
        self.assertEqual(status["action"], "status")
        self.assertFalse(status["gpu_lease_required"])
        self.assertEqual(status["capabilities"]["actions"], ["status", "build-info", "smoke", "unload"])
        self.assertEqual(status["capabilities"]["operations"], {"speech": True, "transcription": True, "embeddings": True})
        self.assertFalse(status["placeholder"]["enabled"])
        self.assertEqual(status["placeholder"]["operations"], [])
        self.assertEqual(status["engines"]["speech"]["engine"], "piper")
        self.assertEqual(status["engines"]["embeddings"]["engine"], "onnx")
        self.assertEqual(status["engines"]["transcription"]["engine"], "vosk")
        self.assertTrue(status["engines"]["speech"]["available"])
        self.assertTrue(status["engines"]["embeddings"]["available"])
        self.assertTrue(status["engines"]["transcription"]["available"])
        self.assertTrue(status["cpu_residency"]["headroom"]["ok"])
        self.assertEqual(status["build_info"]["status"], "ok")
        self.assertNotIn(str(model_root), str(status))
        for probe in status["engines"].values():
            self.assertNotIn("model_path", probe)
            self.assertNotIn("model_root", probe)
            self.assertNotIn("binary", probe)
            self.assertIn("model_path_present", probe)

    def test_runtime_status_degrades_for_enabled_scaffold_engines(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "true"})
        status = asyncio.run(audio_cpu_main.runtime_status(FakeRequest({})))

        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["placeholder"]["operations"], ["speech", "embeddings", "transcription"])
        self.assertEqual(status["engines"]["speech"]["engine"], "scaffold")
        self.assertTrue(status["engines"]["speech"]["placeholder"])
        self.assertFalse(status["gpu_lease_required"])

    def test_placeholder_endpoints_fail_when_policy_disables_them(self) -> None:
        self.patch_env({"B1_CPU_AUDIO_ENGINE": "scaffold", "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false"})

        health = asyncio.run(audio_cpu_main.healthz())
        speech = asyncio.run(audio_cpu_main.speech(FakeRequest({"input": "hello"})))
        transcription = asyncio.run(audio_cpu_main.transcription(FakeRequest({"audio": "UklGRg=="})))
        embeddings = asyncio.run(audio_cpu_main.embeddings(FakeRequest({"input": "hello"})))
        smoke = asyncio.run(audio_cpu_main.runtime_smoke(FakeRequest({"model": "b1-cpu-placeholder-tts", "modality": "tts"})))

        self.assertEqual(health["status"], "unconfigured")
        self.assertFalse(health["capabilities"]["speech"])
        for response in (speech, transcription, embeddings):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["X-B1-GPU-Lease-Required"], "false")
            self.assertIn(b"engine_unavailable", response.body)
        self.assertEqual(smoke["status"], "unconfigured")
        self.assertEqual(smoke["reason"], "engine_unavailable")
        self.assertFalse(smoke["gpu_lease_required"])

    def test_cpu_residency_policy_uses_forwarded_decision_over_env_default(self) -> None:
        self.patch_env(
            {
                "B1_CPU_RESIDENCY_ENABLED": "true",
                "B1_CPU_RESIDENT_ALIASES": "embedding-default,tts-fast",
            }
        )
        self.patch_attr("meminfo_available_ram_gib", lambda path="/proc/meminfo": 16.0)

        self.assertTrue(audio_cpu_main.cpu_residency_allowed({"b1_model_alias": "embedding-default"}))
        self.assertFalse(audio_cpu_main.cpu_residency_allowed({"b1_model_alias": "stt-default"}))
        self.assertFalse(
            audio_cpu_main.cpu_residency_allowed(
                {"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": False}
            )
        )
        self.assertTrue(
            audio_cpu_main.cpu_residency_allowed({"b1_model_alias": "unlisted", "b1_cpu_residency_allowed": "true"})
        )

    def test_cpu_residency_denies_cache_when_host_ram_reserve_is_unavailable(self) -> None:
        self.patch_env({"B1_HOST_TOTAL_RAM_GIB": "32", "B1_HOST_RESERVE_RAM_GIB": "6"})
        self.patch_attr("meminfo_available_ram_gib", lambda path="/proc/meminfo": 5.5)

        headroom = audio_cpu_main.cpu_residency_headroom()

        self.assertFalse(headroom["ok"])
        self.assertEqual(headroom["reason"], "host_ram_below_reserve")
        self.assertFalse(
            audio_cpu_main.cpu_residency_allowed({"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": True})
        )

    def test_onnx_embedding_session_cache_is_used_only_for_cpu_resident_aliases(self) -> None:
        created: list[dict[str, Any]] = []

        def fake_create(model_path: str) -> dict[str, Any]:
            session = {"model_path": model_path, "index": len(created)}
            created.append(session)
            return session

        self.patch_attr("create_onnx_embedding_session", fake_create)
        self.patch_attr("meminfo_available_ram_gib", lambda path="/proc/meminfo": 16.0)
        model_path = "/srv/b1-ai-hub/models/embedding/model.onnx"

        resident_1 = audio_cpu_main.onnx_embedding_session_for_payload(
            model_path,
            {"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": True},
        )
        resident_2 = audio_cpu_main.onnx_embedding_session_for_payload(
            model_path,
            {"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": True},
        )
        one_shot_1 = audio_cpu_main.onnx_embedding_session_for_payload(
            model_path,
            {"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": False},
        )
        one_shot_2 = audio_cpu_main.onnx_embedding_session_for_payload(
            model_path,
            {"b1_model_alias": "embedding-default", "b1_cpu_residency_allowed": False},
        )

        self.assertIs(resident_1, resident_2)
        self.assertIsNot(one_shot_1, one_shot_2)
        self.assertEqual([item["index"] for item in created], [0, 1, 2])
        self.assertEqual(audio_cpu_main.resident_cache_state()["onnx_embedding"]["currsize"], 0)

    def test_runtime_unload_clears_cpu_resident_caches(self) -> None:
        created: list[dict[str, Any]] = []

        def fake_create(model_path: str) -> dict[str, Any]:
            session = {"model_path": model_path, "index": len(created)}
            created.append(session)
            return session

        self.patch_env({"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"})
        self.patch_attr("create_onnx_embedding_session", fake_create)
        audio_cpu_main.onnx_embedding_session("/srv/b1-ai-hub/models/embedding/model.onnx")

        result = asyncio.run(
            audio_cpu_main.runtime_unload(
                FakeRequest(
                    {
                        "model": "b1-cpu-embedding",
                        "model_alias": "embedding-default",
                        "resolved_model_version": "b1-cpu-embedding@0.1.0",
                    },
                    headers={"Authorization": "Bearer hook-token"},
                )
            )
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime"], "audio-cpu")
        self.assertEqual(result["action"], "unload")
        self.assertFalse(result["gpu_lease_required"])
        self.assertEqual(result["details"]["cache"]["before"]["onnx_embedding"]["currsize"], 1)
        self.assertEqual(result["details"]["cache"]["after"]["onnx_embedding"]["currsize"], 0)
        self.assertEqual(audio_cpu_main.resident_cache_state()["onnx_embedding"]["currsize"], 0)

    def test_piper_engine_exposes_real_tts_without_placeholder_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            model_root.mkdir()
            model_path = model_root / "voice.onnx"
            model_path.write_bytes(b"piper-model-placeholder-for-test")
            binary = self.fake_piper(root)
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_PIPER_BINARY": str(binary),
                    "B1_PIPER_MODEL_PATH": str(model_path),
                }
            )

            health = asyncio.run(audio_cpu_main.healthz())
            speech = asyncio.run(audio_cpu_main.speech(FakeRequest({"input": "hello from piper"})))
            smoke = asyncio.run(audio_cpu_main.runtime_smoke(FakeRequest({"model": "tts-fast", "modality": "tts"})))
            transcription = asyncio.run(audio_cpu_main.transcription(FakeRequest({"audio": "UklGRg=="})))

        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["placeholder"])
        self.assertEqual(health["engine"], "piper")
        self.assertEqual(health["capabilities"], {"speech": True, "transcription": False, "embeddings": False})
        self.assertEqual(speech.status_code, 200)
        self.assertEqual(speech.headers["X-B1-Placeholder"], "false")
        self.assertEqual(speech.headers["X-B1-CPU-Audio-Engine"], "piper")
        self.assertGreater(len(speech.body), 44)
        self.assertEqual(smoke["status"], "ok")
        self.assertFalse(smoke["placeholder"])
        self.assertGreater(smoke["measurements"]["peak_ram_mib"], 0)
        self.assertGreater(smoke["measurements"]["speech_bytes"], 44)
        self.assertEqual(transcription.status_code, 503)
        self.assertIn(b"engine_unavailable", transcription.body)

    def test_piper_engine_uses_resolved_model_version_runtime_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            view = model_root / "b1-piper-en-us-amy-low" / "v1.0.0"
            view.mkdir(parents=True)
            model_path = view / "en_US-amy-low.onnx"
            config_path = view / "en_US-amy-low.onnx.json"
            model_path.write_bytes(b"piper-model-placeholder-for-test")
            config_path.write_text("{}", encoding="utf-8")
            binary = self.fake_piper(root)
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_PIPER_BINARY": str(binary),
                    "B1_PIPER_MODEL_PATH": None,
                    "B1_PIPER_CONFIG_PATH": None,
                }
            )

            health = asyncio.run(audio_cpu_main.healthz())
            status = audio_cpu_main.piper_status({"b1_resolved_model_version": "b1-piper-en-us-amy-low@v1.0.0"})
            speech = asyncio.run(
                audio_cpu_main.speech(
                    FakeRequest(
                        {
                            "model": "b1-piper-en-us-amy-low",
                            "b1_resolved_model_version": "b1-piper-en-us-amy-low@v1.0.0",
                            "input": "hello from model view",
                        }
                    )
                )
            )
            smoke = asyncio.run(
                audio_cpu_main.runtime_smoke(
                    FakeRequest(
                        {
                            "model": "b1-piper-en-us-amy-low",
                            "resolved_model_version": "b1-piper-en-us-amy-low@v1.0.0",
                            "modality": "tts",
                        }
                    )
                )
            )

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["capabilities"]["speech"])
        self.assertEqual(status["model_path"], str(model_path.resolve()))
        self.assertEqual(status["config_path"], str(config_path.resolve()))
        self.assertEqual(speech.status_code, 200)
        self.assertEqual(speech.headers["X-B1-Placeholder"], "false")
        self.assertEqual(smoke["status"], "ok")

    def test_piper_engine_rejects_unsafe_resolved_model_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            model_root.mkdir()
            binary = self.fake_piper(root)
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_PIPER_BINARY": str(binary),
                    "B1_PIPER_MODEL_PATH": None,
                    "B1_PIPER_CONFIG_PATH": None,
                }
            )

            response = asyncio.run(
                audio_cpu_main.speech(
                    FakeRequest(
                        {
                            "model": "b1-piper-en-us-amy-low",
                            "b1_resolved_model_version": "../escape@v1.0.0",
                            "input": "hello",
                        }
                    )
                )
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"engine_unavailable", response.body)

    def test_piper_engine_rejects_model_path_outside_model_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            model_root.mkdir()
            outside = root / "outside.onnx"
            outside.write_bytes(b"outside")
            binary = self.fake_piper(root)
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_PIPER_BINARY": str(binary),
                    "B1_PIPER_MODEL_PATH": str(outside),
                }
            )

            health = asyncio.run(audio_cpu_main.healthz())
            response = asyncio.run(audio_cpu_main.speech(FakeRequest({"input": "hello"})))

        self.assertEqual(health["status"], "unconfigured")
        self.assertFalse(health["capabilities"]["speech"])
        self.assertEqual(health["details"]["reason"], "model_path_outside_allowed_root")
        self.assertEqual(response.status_code, 503)

    def test_onnx_embedding_engine_uses_resolved_model_version_runtime_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            view = model_root / "b1-minilm-l6-v2-onnx-q4" / "aff7a1dc4e8a1ea593e6ea21e95c22ef0a25966f" / "onnx"
            view.mkdir(parents=True)
            (view / "model_q4.onnx").write_bytes(b"onnx-placeholder-for-test")
            (model_root / "b1-minilm-l6-v2-onnx-q4" / "aff7a1dc4e8a1ea593e6ea21e95c22ef0a25966f" / "tokenizer.json").write_text("{}", encoding="utf-8")
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_EMBEDDING_ENGINE": "onnx",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_ONNX_EMBEDDING_MODEL_ROOT": str(model_root),
                }
            )
            self.patch_attr("onnx_embedding_dependency_status", lambda: [])
            self.patch_attr("onnx_embedding_vectors", lambda texts, payload=None: [[0.25, 0.5, 0.75, 1.0] for _ in texts])

            health = asyncio.run(audio_cpu_main.healthz())
            response = asyncio.run(
                audio_cpu_main.embeddings(
                    FakeRequest(
                        {
                            "model": "b1-minilm-l6-v2-onnx-q4",
                            "b1_resolved_model_version": "b1-minilm-l6-v2-onnx-q4@aff7a1dc4e8a1ea593e6ea21e95c22ef0a25966f",
                            "input": ["hello", "world"],
                        }
                    )
                )
            )
            smoke = asyncio.run(
                audio_cpu_main.runtime_smoke(
                    FakeRequest(
                        {
                            "model": "b1-minilm-l6-v2-onnx-q4",
                            "resolved_model_version": "b1-minilm-l6-v2-onnx-q4@aff7a1dc4e8a1ea593e6ea21e95c22ef0a25966f",
                            "modality": "embedding",
                        }
                    )
                )
            )

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["capabilities"]["embeddings"])
        self.assertFalse(health["capabilities"]["speech"])
        self.assertFalse(response["b1_placeholder"])
        self.assertEqual(response["b1_embedding_engine"], "onnxruntime")
        self.assertEqual(response["b1_embedding_dimensions"], 4)
        self.assertEqual(len(response["data"]), 2)
        self.assertEqual(response["data"][0]["embedding"], [0.25, 0.5, 0.75, 1.0])
        self.assertEqual(smoke["status"], "ok")
        self.assertEqual(smoke["engine"], "onnx")
        self.assertFalse(smoke["placeholder"])
        self.assertGreater(smoke["measurements"]["peak_ram_mib"], 0)
        self.assertEqual(smoke["measurements"]["embedding_dimensions"], 4)

    def test_vosk_stt_engine_uses_resolved_model_version_runtime_view(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_transcription(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload)
            return {
                "text": "hello from vosk",
                "duration_seconds": 0.25,
                "language": "en-us",
                "gpu_lease_required": False,
                "b1_engine": "vosk",
                "b1_stt_engine": "vosk",
                "b1_placeholder": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / "models"
            view = model_root / "b1-vosk-small-en-us-0.15" / "0.15" / "vosk-model-small-en-us-0.15"
            (view / "am").mkdir(parents=True)
            (view / "conf").mkdir()
            (view / "graph").mkdir()
            (view / "am" / "final.mdl").write_bytes(b"vosk-final-model-placeholder")
            (view / "conf" / "model.conf").write_text("--sample-frequency=16000\n", encoding="utf-8")
            (view / "graph" / "HCLr.fst").write_bytes(b"vosk-graph-placeholder")
            self.patch_env(
                {
                    "B1_CPU_AUDIO_ENGINE": "piper",
                    "B1_CPU_STT_ENGINE": "vosk",
                    "B1_CPU_AUDIO_ENABLE_PLACEHOLDER": "false",
                    "B1_CPU_AUDIO_MODEL_ROOT": str(model_root),
                    "B1_VOSK_STT_MODEL_ROOT": str(model_root),
                }
            )
            self.patch_attr("vosk_stt_dependency_status", lambda: [])
            self.patch_attr("vosk_transcription", fake_transcription)

            payload = {
                "model": "b1-vosk-small-en-us-0.15",
                "b1_resolved_model_version": "b1-vosk-small-en-us-0.15@0.15",
                "audio": base64.b64encode(audio_cpu_main.silence_wav(0.25)).decode("ascii"),
            }
            health = asyncio.run(audio_cpu_main.healthz())
            status = audio_cpu_main.vosk_stt_status(payload)
            response = asyncio.run(audio_cpu_main.transcription(FakeRequest(payload)))
            smoke = asyncio.run(
                audio_cpu_main.runtime_smoke(
                    FakeRequest(
                        {
                            "model": "b1-vosk-small-en-us-0.15",
                            "resolved_model_version": "b1-vosk-small-en-us-0.15@0.15",
                            "modality": "stt",
                        }
                    )
                )
            )

        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["capabilities"]["speech"])
        self.assertTrue(health["capabilities"]["transcription"])
        self.assertEqual(status["model_path"], str(view.resolve()))
        self.assertFalse(response["b1_placeholder"])
        self.assertEqual(response["b1_stt_engine"], "vosk")
        self.assertEqual(response["text"], "hello from vosk")
        self.assertEqual(smoke["status"], "ok")
        self.assertEqual(smoke["engine"], "vosk")
        self.assertFalse(smoke["placeholder"])
        self.assertGreater(smoke["measurements"]["peak_ram_mib"], 0)
        self.assertEqual(smoke["measurements"]["transcript_chars"], len("hello from vosk"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
