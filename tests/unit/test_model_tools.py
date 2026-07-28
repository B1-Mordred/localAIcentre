from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi.responses import JSONResponse  # noqa: E402
    from app import main, model_tools  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    JSONResponse = None  # type: ignore[assignment]
    main = None
    model_tools = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(model_tools is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ModelToolPolicyTests(unittest.TestCase):
    def test_validate_tool_url_rejects_loopback_resolution(self) -> None:
        with self.assertRaisesRegex(model_tools.ModelToolError, "blocked network"):
            model_tools.validate_tool_url("https://example.test/path", resolver=lambda _host, _port: ["127.0.0.1"])

    def test_validate_tool_url_accepts_public_resolution_and_removes_fragment(self) -> None:
        url = model_tools.validate_tool_url("HTTPS://Example.TEST/path?q=1#section", resolver=lambda _host, _port: ["93.184.216.34"])
        self.assertEqual(url, "https://example.test/path?q=1")

    def test_html_to_text_removes_scripts_and_tags(self) -> None:
        text = model_tools.html_to_text("<html><script>secret()</script><h1>Title</h1><p>A&nbsp;B</p></html>", max_chars=100)
        self.assertEqual(text, "Title A B")

    def test_search_result_parser_extracts_links(self) -> None:
        parser = model_tools.SearchResultParser("https://search.example/")
        parser.feed('<a href="https://example.com/a">First result</a><a href="/local">Local result</a>')
        self.assertEqual(
            parser.results,
            [
                {"title": "First result", "url": "https://example.com/a"},
                {"title": "Local result", "url": "https://search.example/local"},
            ],
        )

    def test_registry_definitions_only_include_enabled_requested_tools(self) -> None:
        registry = model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",)))
        definitions = registry.definitions(["web_search", "web_fetch"])
        self.assertEqual([item["function"]["name"] for item in definitions], ["web_fetch"])


@unittest.skipIf(main is None or model_tools is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ChatToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_tool_loop_adds_definitions_executes_tool_and_finishes(self) -> None:
        calls: list[dict[str, object]] = []

        async def fake_runtime_json(path, payload, resolution, operation, owner_id=None):
            calls.append(payload)
            if len(calls) == 1:
                return JSONResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {"name": "web_fetch", "arguments": json.dumps({"url": "https://example.com"})},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                )
            return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "Example Domain"}}]})

        async def fake_execute(_registry, tool_call):
            return {"ok": True, "url": "https://example.com", "text": "Example Domain"}

        original_runtime_json = main.call_openai_runtime_json
        original_execute = main.execute_b1_tool_call
        main.call_openai_runtime_json = fake_runtime_json
        main.execute_b1_tool_call = fake_execute
        self.addCleanup(lambda: setattr(main, "call_openai_runtime_json", original_runtime_json))
        self.addCleanup(lambda: setattr(main, "execute_b1_tool_call", original_execute))

        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "What is on example.com?"}],
            b1_tools=["web_fetch"],
        )
        response = await main.call_chat_with_b1_tools(
            payload,
            main.strip_b1_chat_fields(payload.model_dump(exclude_none=True)),
            SimpleNamespace(runtime="localai", resolved_model_version="model@v1", public_alias="chat-default"),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Example Domain")
        self.assertEqual(response.headers["x-b1-tool-iterations"], "1")
        self.assertEqual(response.headers["x-b1-tools"], "web_fetch")
        self.assertNotIn("b1_tools", calls[0])
        self.assertEqual(calls[0]["tools"][0]["function"]["name"], "web_fetch")
        self.assertEqual(calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual(calls[1]["messages"][-1]["tool_call_id"], "call_1")


if __name__ == "__main__":
    unittest.main()
