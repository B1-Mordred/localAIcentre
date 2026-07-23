from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class VlmOpenAiApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        self.patch_attr("authenticate", authenticate)

    def resolution(self) -> Any:
        return main.RuntimeResolution(
            public_alias="vision-default",
            model_id="localai-vision-model",
            model_version="1.0.0",
            resolved_model_version="localai-vision-model@1.0.0",
            runtime="localai",
            preferred_runtime="localai",
            requires_gpu=True,
            resource_label="expected",
            runtime_policy="any",
        )

    def test_chat_completions_accepts_vlm_aliases(self) -> None:
        self.patch_auth()
        calls: list[dict[str, Any]] = []
        resolution = self.resolution()

        def resolve_catalog_alias_for_modalities(model: str, modalities: set[str], runtime_policy: str = "any", operation: str | None = None) -> Any:
            calls.append({"resolver": model, "modalities": modalities, "runtime_policy": runtime_policy, "operation": operation})
            return resolution

        async def call_openai_runtime_json(path: str, payload: dict[str, Any], selected: Any, operation: str, owner_id: str | None = None) -> Any:
            calls.append({"path": path, "payload": payload, "runtime": selected.runtime, "operation": operation, "owner_id": owner_id})
            return main.JSONResponse({"proxied": True, "runtime": selected.runtime})

        self.patch_attr("resolve_catalog_alias_for_modalities", resolve_catalog_alias_for_modalities)
        self.patch_attr("call_openai_runtime_json", call_openai_runtime_json)

        response = asyncio.run(
            main.chat_completions(
                main.ChatCompletionRequest(
                    model="vision-default",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe"},
                                {"type": "image_url", "image_url": {"url": "/artifacts/images/job/0.png"}},
                            ],
                        }
                    ],
                ),
                authorization="Bearer key",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0], {"resolver": "vision-default", "modalities": {"llm", "vlm"}, "runtime_policy": "any", "operation": "chat"})
        self.assertEqual(calls[1]["path"], "/v1/chat/completions")
        self.assertEqual(calls[1]["runtime"], "localai")
        self.assertEqual(calls[1]["owner_id"], "client_1")

    def test_chat_completions_rejects_non_openai_runtime_without_scaffold_fallback(self) -> None:
        self.patch_auth()
        resolution = main.RuntimeResolution(
            public_alias="chat-default",
            model_id="comfy-chat",
            model_version="1.0.0",
            resolved_model_version="comfy-chat@1.0.0",
            runtime="comfyui",
            preferred_runtime="comfyui",
            requires_gpu=True,
            resource_label="expected",
            runtime_policy="any",
        )

        def resolve_catalog_alias_for_modalities(*_: Any, **__: Any) -> Any:
            return resolution

        async def call_openai_runtime_json(*_: Any, **__: Any) -> Any:
            raise AssertionError("non-OpenAI runtime must be rejected before proxying")

        self.patch_attr("resolve_catalog_alias_for_modalities", resolve_catalog_alias_for_modalities)
        self.patch_attr("call_openai_runtime_json", call_openai_runtime_json)

        with self.assertRaises(main.HTTPException) as raised:
            asyncio.run(main.chat_completions(main.ChatCompletionRequest(model="chat-default", messages=[]), authorization="Bearer key"))

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("does not support OpenAI-compatible chat forwarding", str(raised.exception.detail))

    def test_responses_accepts_vlm_aliases(self) -> None:
        self.patch_auth()
        calls: list[dict[str, Any]] = []
        resolution = self.resolution()

        def resolve_catalog_alias_for_modalities(model: str, modalities: set[str], runtime_policy: str = "any", operation: str | None = None) -> Any:
            calls.append({"resolver": model, "modalities": modalities, "runtime_policy": runtime_policy, "operation": operation})
            return resolution

        async def call_openai_runtime_json(path: str, payload: dict[str, Any], selected: Any, operation: str, owner_id: str | None = None) -> Any:
            calls.append({"path": path, "payload": payload, "runtime": selected.runtime, "operation": operation, "owner_id": owner_id})
            return main.JSONResponse({"id": "resp_1", "runtime": selected.runtime})

        self.patch_attr("resolve_catalog_alias_for_modalities", resolve_catalog_alias_for_modalities)
        self.patch_attr("call_openai_runtime_json", call_openai_runtime_json)

        response = asyncio.run(
            main.responses(
                {
                    "model": "vision-default",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "describe"},
                                {"type": "input_image", "image_url": "/artifacts/images/job/0.png"},
                            ],
                        }
                    ],
                },
                authorization="Bearer key",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0], {"resolver": "vision-default", "modalities": {"llm", "vlm"}, "runtime_policy": "any", "operation": "responses"})
        self.assertEqual(calls[1]["path"], "/v1/responses")
        self.assertEqual(calls[1]["payload"]["model"], "vision-default")
        self.assertEqual(calls[1]["runtime"], "localai")

    def test_embeddings_proxy_to_audio_cpu_runtime(self) -> None:
        self.patch_auth()
        calls: list[dict[str, Any]] = []
        resolution = main.RuntimeResolution(
            public_alias="embedding-default",
            model_id="b1-cpu-placeholder-embedding",
            model_version="0.1.0",
            resolved_model_version="b1-cpu-placeholder-embedding@0.1.0",
            runtime="audio-cpu",
            preferred_runtime="audio-cpu",
            requires_gpu=False,
            resource_label="recommended",
            runtime_policy="any",
        )

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            calls.append({"resolver": model, "modality": modality, "runtime_policy": runtime_policy, "operation": operation})
            return resolution

        async def call_openai_runtime_json(path: str, payload: dict[str, Any], selected: Any, operation: str, owner_id: str | None = None) -> Any:
            calls.append({"path": path, "payload": payload, "runtime": selected.runtime, "operation": operation, "owner_id": owner_id})
            return main.JSONResponse({"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}]})

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("call_openai_runtime_json", call_openai_runtime_json)

        response = asyncio.run(main.embeddings(main.EmbeddingRequest(model="embedding-default", input="hello"), authorization="Bearer key"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0], {"resolver": "embedding-default", "modality": "embedding", "runtime_policy": "any", "operation": "embeddings"})
        self.assertEqual(calls[1]["path"], "/v1/embeddings")
        self.assertEqual(calls[1]["runtime"], "audio-cpu")
        self.assertEqual(calls[1]["owner_id"], "client_1")


if __name__ == "__main__":
    unittest.main()
