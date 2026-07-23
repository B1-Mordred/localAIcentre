from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_proxy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b1_localai_proxy", ROOT / "deploy" / "localai" / "b1_localai_proxy.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load LocalAI proxy")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy = load_proxy()


class LocalAIProxyTests(unittest.TestCase):
    def test_payload_model_candidates_strips_versions_and_deduplicates(self) -> None:
        payload = {
            "model": "chat-default@old",
            "resolved_model_version": "llama-3.1-8b-q4@sha256:abc",
            "model_alias": "chat-default",
        }

        self.assertEqual(proxy.payload_model_candidates(payload), ["chat-default", "llama-3.1-8b-q4"])

    def test_model_ids_from_openai_style_list(self) -> None:
        payload = {"data": [{"id": "chat-default"}, {"id": "embedding-default"}]}

        self.assertEqual(proxy.model_ids_from_list(payload), {"chat-default", "embedding-default"})
        self.assertTrue(proxy.model_available(["chat-default@sha256:abc"], {"chat-default"}))

    def test_smoke_request_for_chat_uses_static_redaction_safe_prompt(self) -> None:
        status, reason, request = proxy.smoke_request(
            {
                "model": "chat-default",
                "modality": "llm",
                "operation": "chat",
            }
        )

        self.assertEqual(status, "ok")
        self.assertEqual(reason, "chat")
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"]["model"], "chat-default")
        self.assertEqual(request["body"]["messages"][0]["content"], "b1 local runtime smoke")
        self.assertFalse(request["body"]["stream"])

    def test_smoke_request_for_embeddings_and_images(self) -> None:
        embedding = proxy.smoke_request({"model": "embedding-default", "modality": "embeddings", "operation": "embeddings"})
        image = proxy.smoke_request({"model": "image-default", "modality": "image", "operation": "text-to-image"})

        self.assertEqual(embedding[2]["path"], "/v1/embeddings")
        self.assertEqual(image[2]["path"], "/v1/images/generations")
        self.assertEqual(image[2]["body"]["size"], os.getenv("B1_LOCALAI_SMOKE_IMAGE_SIZE", "64x64"))

    def test_video_smoke_is_disabled_unless_explicitly_enabled(self) -> None:
        with patch.dict(os.environ, {"B1_LOCALAI_VIDEO_SMOKE_ENABLED": "false"}, clear=False):
            status, reason, request = proxy.smoke_request({"model": "video-text", "modality": "video", "operation": "text-to-video"})

        self.assertEqual(status, "unsupported")
        self.assertEqual(reason, "video_generation_smoke_not_available")
        self.assertIsNone(request)

    def test_operation_kind_distinguishes_image_and_video_generation(self) -> None:
        self.assertEqual(proxy.operation_kind({"modality": "image", "operation": "generation"}), "image_generation")
        self.assertEqual(proxy.operation_kind({"modality": "video", "operation": "generation"}), "video_generation")
        self.assertEqual(proxy.operation_kind({"operation": "text-to-image"}), "image_generation")
        self.assertEqual(proxy.operation_kind({"operation": "text-to-video"}), "video_generation")
        self.assertEqual(proxy.operation_kind({"operation": "image-to-video"}), "video_image")


if __name__ == "__main__":
    unittest.main()
