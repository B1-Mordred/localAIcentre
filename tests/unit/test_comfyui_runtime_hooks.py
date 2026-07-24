from __future__ import annotations

import asyncio
import importlib.util
import sys
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
    folder_paths_mod.get_filename_list = lambda folder: names.get(folder, [])

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

    def test_runtime_action_requires_configured_bearer_token(self) -> None:
        hooks, _routes, _queue = load_hooks()

        with patch.dict("os.environ", {"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"}, clear=False):
            missing = asyncio.run(hooks.runtime_action(FakeRuntimeActionRequest()))
            accepted = asyncio.run(hooks.runtime_action(FakeRuntimeActionRequest(headers={"Authorization": "Bearer hook-token"})))

        self.assertEqual(missing.status, 401)
        self.assertEqual(missing.payload["reason"], "runtime_control_token_required")
        self.assertNotEqual(accepted.status, 401)

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
