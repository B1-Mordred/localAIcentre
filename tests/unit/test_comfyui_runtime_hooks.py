from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status


class FakeRoutes:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, path: str):
        self.posts.append(path)

        def decorator(func):
            return func

        return decorator


class FakeQueue:
    def __init__(self) -> None:
        self.flags: dict[str, object] = {}
        self.running: list[object] = []
        self.queued: list[object] = []
        self.history: dict[str, object] = {}
        self.history_status: str | None = None

    def get_current_queue(self) -> tuple[list[object], list[object]]:
        return self.running, self.queued

    def get_tasks_remaining(self) -> int:
        return len(self.running) + len(self.queued)

    def set_flag(self, name: str, data: object) -> None:
        self.flags[name] = data

    def put(self, item: object) -> None:
        self.queued.append(item)
        if self.history_status and isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], str):
            self.history[item[1]] = {"status": {"status_str": self.history_status}}

    def get_history(self, prompt_id: str | None = None) -> dict[str, object]:
        if prompt_id is not None:
            item = self.history.get(prompt_id)
            return {prompt_id: item} if item is not None else {}
        return dict(self.history)


class FakeRuntimeActionRequest:
    def __init__(self, *, headers: dict[str, str] | None = None, payload: object | None = None, action: str = "smoke") -> None:
        self.headers = headers or {}
        self.match_info = {"action": action}
        self.payload = {} if payload is None else payload

    async def json(self) -> object:
        return self.payload


def load_hooks(folder_names: dict[str, list[str]] | None = None):
    fake_routes = FakeRoutes()
    fake_queue = FakeQueue()
    prompt_server_cls = type("PromptServer", (), {"instance": types.SimpleNamespace(routes=fake_routes, prompt_queue=fake_queue)})

    aiohttp_mod = types.ModuleType("aiohttp")
    web_mod = types.SimpleNamespace(
        Request=object,
        Response=FakeResponse,
        json_response=lambda payload, status=200: FakeResponse(payload, status),
    )
    aiohttp_mod.web = web_mod

    server_mod = types.ModuleType("server")
    server_mod.PromptServer = prompt_server_cls

    comfy_mod = types.ModuleType("comfy")
    model_management_mod = types.ModuleType("comfy.model_management")
    model_management_mod.get_torch_device = lambda: "cuda:0"
    model_management_mod.get_torch_device_name = lambda device: "Fake CUDA"
    model_management_mod.get_total_memory = lambda device, torch_total_too=False: (12 * 1024**3, 1024**3)
    model_management_mod.get_free_memory = lambda device, torch_free_too=False: (10 * 1024**3, 512 * 1024**2)
    model_management_mod.loaded_models = lambda: ["model"]
    model_management_mod.unload_all_models = lambda: None
    model_management_mod.soft_empty_cache = lambda: None
    comfy_mod.model_management = model_management_mod

    execution_mod = types.ModuleType("execution")

    async def validate_prompt(prompt_id: str, prompt: dict[str, object], partial_execution_targets: object) -> tuple[bool, None, list[str], dict[str, object]]:
        return True, None, ["1"], {}

    execution_mod.validate_prompt = validate_prompt

    folder_paths_mod = types.ModuleType("folder_paths")
    names = folder_names or {"checkpoints": ["sdxl/base.safetensors"], "vae": ["sdxl.vae.safetensors"]}
    folder_paths_mod.added_paths = []
    folder_paths_mod.filename_list_cache = {}
    folder_paths_mod.get_filename_list = lambda folder: names.get(folder, [])

    def add_model_folder_path(folder: str, path: str, is_default: bool = False) -> None:
        folder_paths_mod.added_paths.append((folder, path, is_default))

    folder_paths_mod.add_model_folder_path = add_model_folder_path

    fake_modules = {
        "aiohttp": aiohttp_mod,
        "server": server_mod,
        "comfy": comfy_mod,
        "comfy.model_management": model_management_mod,
        "execution": execution_mod,
        "folder_paths": folder_paths_mod,
    }
    previous = {name: sys.modules.get(name) for name in fake_modules}
    sys.modules.update(fake_modules)
    try:
        spec = importlib.util.spec_from_file_location("b1_comfyui_runtime_hooks_test", ROOT / "deploy" / "comfyui" / "b1_runtime_hooks" / "__init__.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load ComfyUI runtime hooks")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module, fake_routes, fake_queue


class ComfyUiRuntimeHooksTests(unittest.TestCase):
    def test_registers_b1_runtime_route_and_smoke_node(self) -> None:
        hooks, routes, _queue = load_hooks()

        self.assertIn("/b1/runtime/{action}", routes.posts)
        self.assertIn("B1RuntimeSmoke", hooks.NODE_CLASS_MAPPINGS)
        self.assertEqual(hooks.NODE_DISPLAY_NAME_MAPPINGS["B1RuntimeSmoke"], "B1 Runtime Smoke")
        self.assertIn("B1RuntimeTinyImage", hooks.NODE_CLASS_MAPPINGS)
        self.assertEqual(hooks.NODE_DISPLAY_NAME_MAPPINGS["B1RuntimeTinyImage"], "B1 Runtime Tiny Image")
        self.assertEqual(hooks.B1RuntimeTinyImage.RETURN_TYPES, ("IMAGE",))
        required_inputs = hooks.B1RuntimeTinyImage.INPUT_TYPES()["required"]
        self.assertEqual(set(required_inputs), {"width", "height", "red", "green", "blue"})
        self.assertIn("B1RuntimeSaveAnimatedGif", hooks.NODE_CLASS_MAPPINGS)
        self.assertEqual(hooks.NODE_DISPLAY_NAME_MAPPINGS["B1RuntimeSaveAnimatedGif"], "B1 Runtime Save Animated GIF")
        self.assertEqual(hooks.B1RuntimeSaveAnimatedGif.RETURN_TYPES, ())
        self.assertTrue(hooks.B1RuntimeSaveAnimatedGif.OUTPUT_NODE)
        gif_inputs = hooks.B1RuntimeSaveAnimatedGif.INPUT_TYPES()["required"]
        self.assertEqual(set(gif_inputs), {"images", "filename_prefix", "fps", "max_frames", "loop"})

    def test_animated_gif_output_metadata_uses_native_comfyui_gifs_key(self) -> None:
        hooks, _routes, _queue = load_hooks()

        result = hooks.animated_gif_ui_result("clip_00001_.gif", "b1-video", "output", 4, 4)

        self.assertEqual(
            result,
            {
                "ui": {
                    "gifs": [
                        {
                            "filename": "clip_00001_.gif",
                            "subfolder": "b1-video",
                            "type": "output",
                            "format": "image/gif",
                            "frame_count": 4,
                            "fps": 4,
                        }
                    ]
                }
            },
        )

    def test_animated_gif_filename_prefix_is_constrained_to_output_segments(self) -> None:
        hooks, _routes, _queue = load_hooks()

        self.assertEqual(hooks.safe_filename_prefix("../unsafe//video clip"), "unsafe/video_clip")
        self.assertEqual(hooks.safe_filename_prefix(""), "b1-video/animated")
        self.assertEqual(hooks.safe_filename_prefix("a/b/c/d/e/f"), "a/b/c/d")

    def test_runtime_action_requires_configured_bearer_token(self) -> None:
        hooks, _routes, _queue = load_hooks()

        with patch.dict("os.environ", {"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"}, clear=False):
            missing = asyncio.run(hooks.runtime_action(FakeRuntimeActionRequest()))
            accepted = asyncio.run(hooks.runtime_action(FakeRuntimeActionRequest(headers={"Authorization": "Bearer hook-token"})))

        self.assertEqual(missing.status, 401)
        self.assertEqual(missing.payload["reason"], "runtime_control_token_required")
        self.assertNotEqual(accepted.status, 401)

    def test_build_info_reports_pinned_upstream_and_hook_identity(self) -> None:
        hooks, _routes, _queue = load_hooks()

        with patch.dict(
            "os.environ",
            {
                "B1_COMFYUI_HOOK_VERSION": "b1-comfyui-hooks/v0.3.77-b1",
                "B1_COMFYUI_UPSTREAM_VERSION": "v0.3.77",
                "B1_COMFYUI_UPSTREAM_COMMIT": "59afc3984868289f808d02fa5cd180edfb2de240",
                "B1_COMFYUI_SOURCE_ARCHIVE_SHA256": "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1",
            },
            clear=False,
        ):
            info = hooks.comfyui_build_info()

        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["runtime"], "comfyui")
        self.assertEqual(info["action"], "build-info")
        self.assertEqual(info["hook"], "b1-comfyui-runtime-hooks")
        self.assertEqual(info["hook_version"], "b1-comfyui-hooks/v0.3.77-b1")
        self.assertEqual(info["upstream_repository"], "Comfy-Org/ComfyUI")
        self.assertEqual(info["upstream_version"], "v0.3.77")
        self.assertEqual(info["upstream_commit"], "59afc3984868289f808d02fa5cd180edfb2de240")
        self.assertEqual(info["source_archive_sha256"], "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1")
        self.assertTrue(info["pinned"])

    def test_build_info_fails_closed_when_pins_are_invalid(self) -> None:
        hooks, _routes, _queue = load_hooks()

        with patch.dict(
            "os.environ",
            {
                "B1_COMFYUI_UPSTREAM_COMMIT": "not-a-commit",
                "B1_COMFYUI_SOURCE_ARCHIVE_SHA256": "not-a-sha256",
            },
            clear=False,
        ):
            info = hooks.comfyui_build_info()

        self.assertEqual(info["status"], "unconfigured")
        self.assertFalse(info["pinned"])
        self.assertEqual(info["upstream_commit"], "")
        self.assertEqual(info["source_archive_sha256"], "")

    def test_status_reports_queue_memory_model_folder_summary_and_capabilities(self) -> None:
        hooks, _routes, queue = load_hooks({"checkpoints": ["sdxl/base.safetensors", "sdxl/refiner.safetensors"], "vae": ["sdxl.vae.safetensors"]})
        queue.running.append(object())
        queue.queued.append(object())

        result = hooks.handle_status({})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime"], "comfyui")
        self.assertEqual(result["action"], "status")
        self.assertEqual(result["queue"]["running"], 1)
        self.assertEqual(result["queue"]["queued"], 1)
        self.assertEqual(result["queue"]["tasks_remaining"], 2)
        self.assertTrue(result["memory"]["available"])
        self.assertEqual(result["memory"]["loaded_model_count"], 1)
        self.assertEqual(result["model_folders"]["folder_count"], len(hooks.B1_MODEL_FOLDERS))
        self.assertEqual(result["model_folders"]["file_count"], 3)
        self.assertEqual(result["model_folders"]["folders"][0]["folder"], "checkpoints")
        self.assertEqual(result["model_folders"]["folders"][0]["file_count"], 2)
        self.assertNotIn("base.safetensors", json.dumps(result["model_folders"]))
        self.assertTrue({"status", "build-info", "load", "warm", "smoke", "unload"}.issubset(set(result["capabilities"]["actions"])))
        self.assertTrue(result["build_info"]["pinned"])

    def test_syncs_comfyui_runtime_view_manifest_paths_into_folder_registry(self) -> None:
        hooks, _routes, _queue = load_hooks({"checkpoints": []})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = root / "b1-image-model" / "20260726" / "diffusion" / "checkpoints"
            view.mkdir(parents=True)
            (view / "tiny.safetensors").write_bytes(b"safe")
            marker = root / "b1-image-model" / "20260726" / "manifest.b1.json"
            marker.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-runtime-view/v1",
                        "runtime": "comfyui",
                        "model_ref": "b1-image-model@20260726",
                        "files": [{"path": "diffusion/checkpoints/tiny.safetensors"}],
                    }
                ),
                encoding="utf-8",
            )
            hooks.folder_paths.filename_list_cache["checkpoints"] = object()

            with patch.dict("os.environ", {"B1_COMFYUI_MODEL_VIEW_ROOT": str(root)}, clear=False):
                summary = hooks.sync_b1_model_view_paths()

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["manifest_count"], 1)
        self.assertEqual(summary["added_path_count"], 1)
        self.assertEqual(summary["folder_count"], 1)
        self.assertEqual(
            hooks.folder_paths.added_paths,
            [("checkpoints", str(view), True)],
        )
        self.assertNotIn("checkpoints", hooks.folder_paths.filename_list_cache)

    def test_model_matching_accepts_folder_path_basename_and_stem(self) -> None:
        hooks, _routes, _queue = load_hooks()
        files = [{"folder": "checkpoints", "name": "sdxl/base.safetensors"}]

        self.assertTrue(hooks.model_available(["sdxl/base.safetensors"], files))
        self.assertTrue(hooks.model_available(["base.safetensors"], files))
        self.assertTrue(hooks.model_available(["base"], files))
        self.assertFalse(hooks.model_available(["other"], files))

    def test_strict_model_list_missing_model_fails_closed(self) -> None:
        hooks, _routes, _queue = load_hooks({"checkpoints": ["sdxl/base.safetensors"]})

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_STRICT_MODEL_LIST": "true"}, clear=False):
            result = hooks.check_model_list({"model": "missing-model"}, "load")

        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["reason"], "model_not_listed")

    def test_smoke_defaults_to_unconfirmed_without_queueing_work(self) -> None:
        hooks, _routes, queue = load_hooks()

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_SMOKE_ENABLED": "false"}, clear=False):
            result = asyncio.run(hooks.handle_smoke({"model": "base"}))

        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["reason"], "smoke_disabled")
        self.assertEqual(queue.queued, [])

    def test_enabled_smoke_runs_noop_prompt_through_queue(self) -> None:
        hooks, _routes, queue = load_hooks()
        queue.history_status = "success"

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_SMOKE_ENABLED": "true"}, clear=False):
            result = asyncio.run(hooks.handle_smoke({"model": "base"}))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "b1_noop_queue")
        self.assertEqual(len(queue.queued), 1)
        queued = queue.queued[0]
        self.assertIsInstance(queued, tuple)
        self.assertEqual(queued[2], {"1": {"class_type": "B1RuntimeSmoke", "inputs": {}}})

    def test_enabled_smoke_runs_manifest_native_prompt_through_queue(self) -> None:
        hooks, _routes, queue = load_hooks()
        queue.history_status = "success"
        prompt = {
            "42": {
                "class_type": "B1RuntimeTinyImage",
                "inputs": {"width": 32, "height": 32},
            }
        }

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_SMOKE_ENABLED": "true"}, clear=False):
            result = asyncio.run(
                hooks.handle_smoke(
                    {
                        "model": "base",
                        "runtime_smoke": {
                            "schema": "b1-ai-hub-runtime-smoke/v1",
                            "comfyui": {"prompt": prompt, "timeout_seconds": 5},
                        },
                    }
                )
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "b1_native_prompt")
        self.assertEqual(result["measurements"]["prompt_node_count"], 1)
        self.assertEqual(len(queue.queued), 1)
        queued = queue.queued[0]
        self.assertIsInstance(queued, tuple)
        self.assertEqual(queued[2], prompt)
        self.assertEqual(queued[3]["strategy"], "b1_native_prompt")

    def test_enabled_smoke_rejects_invalid_native_prompt_before_queueing(self) -> None:
        hooks, _routes, queue = load_hooks()

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_SMOKE_ENABLED": "true"}, clear=False):
            result = asyncio.run(
                hooks.handle_smoke(
                    {
                        "model": "base",
                        "runtime_smoke_config": {
                            "prompt": {"42": {"inputs": {"width": 32}}},
                        },
                    }
                )
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "native_prompt_invalid")
        self.assertEqual(queue.queued, [])

    def test_enabled_warm_reports_ready_after_noop_prompt(self) -> None:
        hooks, _routes, queue = load_hooks()
        queue.history_status = "success"

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_WARM_ENABLED": "true"}, clear=False):
            result = asyncio.run(hooks.handle_warm({"model": "base"}))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["strategy"], "b1_noop_queue")
        self.assertEqual(len(queue.queued), 1)

    def test_unload_sets_native_free_flags_and_reports_idle_success(self) -> None:
        hooks, _routes, queue = load_hooks()

        with patch.dict("os.environ", {"B1_COMFYUI_HOOK_UNLOAD_IMMEDIATE_IDLE": "true"}, clear=False):
            result = hooks.handle_unload({})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "native_free_flags_immediate_idle")
        self.assertEqual(queue.flags, {"unload_models": True, "free_memory": True})


if __name__ == "__main__":
    unittest.main()
