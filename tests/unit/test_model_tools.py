from __future__ import annotations

import json
import sys
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    import httpx  # noqa: E402
    from fastapi.responses import JSONResponse  # noqa: E402
    from app import main, model_tools, secret_store  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    JSONResponse = None  # type: ignore[assignment]
    main = None
    model_tools = None  # type: ignore[assignment]
    secret_store = None  # type: ignore[assignment]
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

    def test_html_title_extracts_document_title(self) -> None:
        title = model_tools.html_title("<html><head><title> Example Domain </title></head><body><h1>Ignored</h1></body></html>")
        self.assertEqual(title, "Example Domain")

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

    def test_unwrap_search_result_url_removes_duckduckgo_redirect(self) -> None:
        self.assertEqual(
            model_tools.unwrap_search_result_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=abc"),
            "https://example.com/docs",
        )

    def test_registry_definitions_only_include_enabled_requested_tools(self) -> None:
        registry = model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",)))
        definitions = registry.definitions(["web_search", "web_fetch"])
        self.assertEqual([item["function"]["name"] for item in definitions], ["web_fetch"])

    def test_registry_supports_custom_http_json_tool_definition(self) -> None:
        definition = model_tools.ModelToolDefinition(
            name="lookup-ticket",
            enabled=True,
            kind="http-json",
            display_name="Lookup ticket",
            description="Lookup a ticket in an approved service.",
            parameters_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
            config={"url": "https://example.com/ticket", "method": "POST"},
        )
        registry = model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("lookup-ticket",)), definitions=[definition])
        definitions = registry.definitions(["lookup-ticket"])
        self.assertEqual(definitions[0]["function"]["name"], "lookup-ticket")
        self.assertEqual(definitions[0]["function"]["parameters"]["required"], ["id"])

    def test_custom_http_json_tool_posts_arguments(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok", "echo": json.loads(request.content.decode("utf-8"))})

        definition = model_tools.ModelToolDefinition(
            name="lookup-ticket",
            enabled=True,
            kind="http-json",
            display_name="Lookup ticket",
            description="Lookup a ticket in an approved service.",
            parameters_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
            config={"url": "https://example.com/ticket", "method": "POST"},
        )
        registry = model_tools.ModelToolRegistry(
            model_tools.ModelToolSettings(allowed_tools=("lookup-ticket",)),
            definitions=[definition],
            transport=httpx.MockTransport(handler),
            resolver=lambda _host, _port: ["93.184.216.34"],
        )
        result = asyncio.run(registry.execute("lookup-ticket", {"id": "T-1"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["json"]["echo"], {"id": "T-1"})
        self.assertEqual(requests[0].url, "https://example.com/ticket")

    def test_custom_http_json_tool_sends_headers_from_provider(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok"})

        async def header_provider(definition: model_tools.ModelToolDefinition) -> dict[str, str]:
            self.assertEqual(definition.name, "lookup-ticket")
            return {"Authorization": "Bearer test-token", "X-Tool-Key": "key-1"}

        definition = model_tools.ModelToolDefinition(
            name="lookup-ticket",
            enabled=True,
            kind="http-json",
            display_name="Lookup ticket",
            description="Lookup a ticket in an approved service.",
            parameters_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
            config={"url": "https://example.com/ticket", "method": "GET"},
        )
        registry = model_tools.ModelToolRegistry(
            model_tools.ModelToolSettings(allowed_tools=("lookup-ticket",)),
            definitions=[definition],
            transport=httpx.MockTransport(handler),
            resolver=lambda _host, _port: ["93.184.216.34"],
            header_provider=header_provider,
        )
        result = asyncio.run(registry.execute("lookup-ticket", {"id": "T-1"}))
        self.assertTrue(result["ok"])
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-token")
        self.assertEqual(requests[0].headers["x-tool-key"], "key-1")

    def test_model_tool_policy_validation_rejects_missing_search_placeholder(self) -> None:
        with self.assertRaisesRegex(Exception, "search_endpoint_template"):
            main.validate_model_tool_policy_payload(
                main.ModelToolPolicyRequest(search_endpoint_template="https://duckduckgo.com/html/")
            )

    def test_model_tool_policy_validation_rejects_private_search_endpoint(self) -> None:
        with self.assertRaisesRegex(Exception, "blocked network"):
            main.validate_model_tool_policy_payload(
                main.ModelToolPolicyRequest(search_endpoint_template="http://127.0.0.1/search?q={query}")
            )

    def test_model_tool_definition_validation_rejects_builtin_and_arbitrary_headers(self) -> None:
        with self.assertRaisesRegex(Exception, "built-in"):
            main.validate_model_tool_definition_payload(
                "web_fetch",
                main.ModelToolDefinitionRequest(
                    kind="http-json",
                    display_name="Override",
                    description="Override built-in tool.",
                    config={"url": "https://example.com/tool", "method": "POST"},
                ),
            )
        with self.assertRaisesRegex(Exception, "headers"):
            main.validate_model_tool_definition_payload(
                "ticket-lookup",
                main.ModelToolDefinitionRequest(
                    kind="http-json",
                    display_name="Ticket lookup",
                    description="Lookup a ticket.",
                    config={"url": "https://example.com/tool", "method": "POST", "headers": {"Authorization": "secret"}},
                ),
            )

    def test_model_tool_definition_validation_normalizes_http_json(self) -> None:
        payload = main.ModelToolDefinitionRequest(
            kind="http-json",
            display_name="Ticket lookup",
            description="Lookup a ticket.",
            parameters_schema={"properties": {"id": {"type": "string"}}},
            config={"url": "https://example.com/tool", "method": "post", "max_result_chars": 2048},
            visibility_roles=["admin", "creator"],
        )
        normalized = main.validate_model_tool_definition_payload("ticket-lookup", payload)
        self.assertEqual(normalized["name"], "ticket-lookup")
        self.assertEqual(normalized["config"]["method"], "POST")
        self.assertEqual(normalized["config"]["url"], "https://example.com/tool")
        self.assertEqual(normalized["parameters_schema"]["type"], "object")
        self.assertEqual(normalized["visibility_roles"], ["admin", "creator"])

    def test_model_tool_definition_validation_normalizes_bearer_auth(self) -> None:
        payload = main.ModelToolDefinitionRequest(
            kind="http-json",
            display_name="Ticket lookup",
            description="Lookup a ticket.",
            config={
                "url": "https://example.com/tool",
                "method": "post",
                "auth": {"type": "bearer", "secret_name": "integration:ticket-api"},
            },
        )
        normalized = main.validate_model_tool_definition_payload("ticket-lookup", payload)
        self.assertEqual(normalized["config"]["auth"], {"type": "bearer", "secret_name": "integration:ticket-api"})

    def test_model_tool_definition_validation_normalizes_header_auth(self) -> None:
        payload = main.ModelToolDefinitionRequest(
            kind="http-json",
            display_name="Ticket lookup",
            description="Lookup a ticket.",
            config={
                "url": "https://example.com/tool",
                "method": "post",
                "auth": {"type": "header", "secret_name": "integration:ticket-api", "header_name": "X-API-Key"},
            },
        )
        normalized = main.validate_model_tool_definition_payload("ticket-lookup", payload)
        self.assertEqual(normalized["config"]["auth"], {"type": "header", "secret_name": "integration:ticket-api", "header_name": "X-API-Key"})

    def test_model_tool_definition_validation_rejects_unsafe_auth_headers(self) -> None:
        for header_name in ["Cookie", "Content-Type", "Sec-Fetch-Site", "Bad Header"]:
            with self.subTest(header_name=header_name):
                with self.assertRaises(Exception):
                    main.validate_model_tool_definition_payload(
                        "ticket-lookup",
                        main.ModelToolDefinitionRequest(
                            kind="http-json",
                            display_name="Ticket lookup",
                            description="Lookup a ticket.",
                            config={
                                "url": "https://example.com/tool",
                                "method": "post",
                                "auth": {"type": "header", "secret_name": "integration:ticket-api", "header_name": header_name},
                            },
                        ),
                    )

    def test_model_tool_secret_reference_requires_integration_secret(self) -> None:
        original_get = main.database.get_encrypted_secret

        async def fake_get_encrypted_secret(name: str, *, include_deleted: bool = False) -> dict[str, object] | None:
            self.assertEqual(name, "integration:ticket-api")
            return {"name": name, "category": "runtime"}

        main.database.get_encrypted_secret = fake_get_encrypted_secret
        self.addCleanup(lambda: setattr(main.database, "get_encrypted_secret", original_get))
        with self.assertRaisesRegex(Exception, "category integration"):
            asyncio.run(main.validate_model_tool_secret_references({"auth": {"type": "bearer", "secret_name": "integration:ticket-api"}}))

    @unittest.skipIf(secret_store is None or getattr(secret_store, "AESGCM", None) is None, "cryptography is not installed")
    def test_model_tool_secret_headers_decrypt_integration_secret(self) -> None:
        master_key = "m" * 40
        secret_name = "integration:ticket-api"
        envelope = secret_store.encrypt_value(master_key, secret_name, "secret-token")
        original_get = main.database.get_encrypted_secret
        original_require_key = main.require_master_encryption_key

        async def fake_get_encrypted_secret(name: str, *, include_deleted: bool = False) -> dict[str, object] | None:
            self.assertEqual(name, secret_name)
            return {"name": name, "category": "integration", "secret_envelope": envelope}

        main.database.get_encrypted_secret = fake_get_encrypted_secret
        main.require_master_encryption_key = lambda: master_key
        self.addCleanup(lambda: setattr(main.database, "get_encrypted_secret", original_get))
        self.addCleanup(lambda: setattr(main, "require_master_encryption_key", original_require_key))
        definition = model_tools.ModelToolDefinition(
            name="lookup-ticket",
            enabled=True,
            kind="http-json",
            display_name="Lookup ticket",
            description="Lookup a ticket in an approved service.",
            parameters_schema={"type": "object"},
            config={"url": "https://example.com/ticket", "method": "GET", "auth": {"type": "bearer", "secret_name": secret_name}},
        )
        headers = asyncio.run(main.model_tool_secret_headers(definition))
        self.assertEqual(headers, {"Authorization": "Bearer secret-token"})

    def test_parse_b1_text_tool_call_accepts_strict_json_object(self) -> None:
        tool_call = main.parse_b1_text_tool_call(
            '{"b1_tool_call":{"name":"web_fetch","arguments":{"url":"https://example.com"}}}',
            {"web_fetch"},
        )
        self.assertIsNotNone(tool_call)
        assert tool_call is not None
        self.assertEqual(tool_call["function"]["name"], "web_fetch")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"url": "https://example.com"})

    def test_parse_b1_text_tool_call_accepts_fenced_json_object(self) -> None:
        tool_call = main.parse_b1_text_tool_call(
            '```json\n{"b1_tool_call":{"name":"web_fetch","arguments":{"url":"https://example.com"}}}\n```',
            {"web_fetch"},
        )
        self.assertIsNotNone(tool_call)

    def test_parse_b1_text_tool_call_rejects_prose_and_unknown_tools(self) -> None:
        self.assertIsNone(
            main.parse_b1_text_tool_call(
                'Please call {"b1_tool_call":{"name":"web_fetch","arguments":{"url":"https://example.com"}}}',
                {"web_fetch"},
            )
        )
        self.assertIsNone(
            main.parse_b1_text_tool_call(
                '{"b1_tool_call":{"name":"unsafe_tool","arguments":{}}}',
                {"web_fetch"},
            )
        )


@unittest.skipIf(main is None or model_tools is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ChatToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_model_tool_execute_runs_enabled_tool_and_audits(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><h1>Example Domain</h1></html>")

        registry = model_tools.ModelToolRegistry(
            model_tools.ModelToolSettings(allowed_tools=("web_fetch",)),
            transport=httpx.MockTransport(handler),
            resolver=lambda _host, _port: ["93.184.216.34"],
        )
        auth = main.AuthContext(subject_id="operator_1", role=main.Role.OPERATOR, scopes=frozenset({"models:write"}))
        audits: list[dict[str, object]] = []

        async def fake_authenticate(_authorization):
            return auth

        async def fake_registry(_auth):
            return registry

        async def fake_audit_event(*_args, **kwargs):
            audits.append(kwargs)

        original_authenticate = main.authenticate
        original_registry = main.model_tool_registry_for_auth
        original_audit = main.record_audit_event
        main.authenticate = fake_authenticate
        main.model_tool_registry_for_auth = fake_registry
        main.record_audit_event = fake_audit_event
        self.addAsyncCleanup(lambda: setattr(main, "authenticate", original_authenticate))
        self.addAsyncCleanup(lambda: setattr(main, "model_tool_registry_for_auth", original_registry))
        self.addAsyncCleanup(lambda: setattr(main, "record_audit_event", original_audit))

        response = await main.admin_model_tool_execute(
            "web_fetch",
            main.ModelToolExecuteRequest(arguments={"url": "https://example.com"}),
            authorization="Bearer test",
        )
        self.assertEqual(response["tool"], "web_fetch")
        self.assertTrue(response["result"]["ok"])
        self.assertIn("Example Domain", response["result"]["text"])
        self.assertEqual(str(requests[0].url), "https://example.com/")
        self.assertEqual(audits[0]["target_id"], "web_fetch")
        self.assertEqual(audits[0]["metadata"]["argument_keys"], ["url"])

    async def test_admin_model_tool_execute_rejects_invisible_tool(self) -> None:
        auth = main.AuthContext(subject_id="operator_1", role=main.Role.OPERATOR, scopes=frozenset({"models:write"}))

        async def fake_authenticate(_authorization):
            return auth

        async def fake_registry(_auth):
            return model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",)))

        original_authenticate = main.authenticate
        original_registry = main.model_tool_registry_for_auth
        main.authenticate = fake_authenticate
        main.model_tool_registry_for_auth = fake_registry
        self.addAsyncCleanup(lambda: setattr(main, "authenticate", original_authenticate))
        self.addAsyncCleanup(lambda: setattr(main, "model_tool_registry_for_auth", original_registry))

        with self.assertRaisesRegex(Exception, "not enabled"):
            await main.admin_model_tool_execute(
                "ticket-lookup",
                main.ModelToolExecuteRequest(arguments={"id": "T-1"}),
                authorization="Bearer test",
            )

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
            ["web_fetch"],
            model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",))),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Example Domain")
        self.assertEqual(response.headers["x-b1-tool-iterations"], "1")
        self.assertEqual(response.headers["x-b1-tools"], "web_fetch")
        self.assertNotIn("b1_tools", calls[0])
        self.assertEqual(calls[0]["tools"][0]["function"]["name"], "web_fetch")
        self.assertEqual(calls[1]["messages"][-2]["role"], "tool")
        self.assertEqual(calls[1]["messages"][-2]["tool_call_id"], "call_1")
        self.assertEqual(calls[1]["messages"][-1]["role"], "system")

    async def test_chat_tool_loop_executes_text_protocol_tool_call(self) -> None:
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
                                    "content": '{"b1_tool_call":{"name":"web_fetch","arguments":{"url":"https://example.com"}}}',
                                }
                            }
                        ]
                    }
                )
            return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "Example Domain"}}]})

        executed: list[dict[str, object]] = []

        async def fake_execute(_registry, tool_call):
            executed.append(tool_call)
            return {"ok": True, "url": "https://example.com", "text": "Example Domain"}

        original_runtime_json = main.call_openai_runtime_json
        original_execute = main.execute_b1_tool_call
        main.call_openai_runtime_json = fake_runtime_json
        main.execute_b1_tool_call = fake_execute
        self.addCleanup(lambda: setattr(main, "call_openai_runtime_json", original_runtime_json))
        self.addCleanup(lambda: setattr(main, "execute_b1_tool_call", original_execute))

        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "Fetch example.com and answer with the title."}],
            b1_tools=["web_fetch"],
        )
        response = await main.call_chat_with_b1_tools(
            payload,
            main.strip_b1_chat_fields(payload.model_dump(exclude_none=True)),
            SimpleNamespace(runtime="localai", resolved_model_version="model@v1", public_alias="chat-default"),
            ["web_fetch"],
            model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",))),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Example Domain")
        self.assertEqual(response.headers["x-b1-tool-iterations"], "1")
        self.assertEqual(executed[0]["function"]["name"], "web_fetch")
        self.assertEqual(calls[0]["messages"][0]["role"], "system")
        self.assertIn("B1 AI Hub tools are available", calls[0]["messages"][0]["content"])
        self.assertEqual(calls[1]["messages"][-2]["role"], "tool")
        self.assertEqual(calls[1]["messages"][-1]["role"], "system")

    async def test_chat_tool_loop_stops_repeated_identical_tool_call(self) -> None:
        calls: list[dict[str, object]] = []
        repeated_tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_fetch", "arguments": json.dumps({"url": "https://example.com"})},
        }

        async def fake_runtime_json(path, payload, resolution, operation, owner_id=None):
            calls.append(payload)
            if len(calls) <= 2:
                return JSONResponse({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [repeated_tool_call]}}]})
            return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "Example Domain"}}]})

        executed: list[dict[str, object]] = []

        async def fake_execute(_registry, tool_call):
            executed.append(tool_call)
            return {"ok": True, "tool": "web_fetch", "url": "https://example.com", "title": "Example Domain", "text": "Example Domain Example Domain This domain is for use in examples."}

        original_runtime_json = main.call_openai_runtime_json
        original_execute = main.execute_b1_tool_call
        main.call_openai_runtime_json = fake_runtime_json
        main.execute_b1_tool_call = fake_execute
        self.addCleanup(lambda: setattr(main, "call_openai_runtime_json", original_runtime_json))
        self.addCleanup(lambda: setattr(main, "execute_b1_tool_call", original_execute))

        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "Fetch example.com and answer with the title."}],
            b1_tools=["web_fetch"],
            b1_tool_max_iterations=4,
        )
        response = await main.call_chat_with_b1_tools(
            payload,
            main.strip_b1_chat_fields(payload.model_dump(exclude_none=True)),
            SimpleNamespace(runtime="localai", resolved_model_version="model@v1", public_alias="chat-default"),
            ["web_fetch"],
            model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_fetch",))),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Example Domain")
        self.assertEqual(len(executed), 1)
        self.assertEqual(response.headers["x-b1-tool-stop-reason"], "duplicate_tool_call")
        self.assertEqual(response.headers["x-b1-tool-answer"], "synthesized")
        self.assertTrue(body["b1_tool_answer_synthesized"])
        self.assertEqual(len(calls), 2)

    async def test_chat_tool_loop_synthesizes_search_results_on_max_iterations(self) -> None:
        calls: list[dict[str, object]] = []

        async def fake_runtime_json(path, payload, resolution, operation, owner_id=None):
            calls.append(payload)
            return JSONResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{len(calls)}",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": json.dumps({"query": "current example"})},
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        async def fake_execute(_registry, tool_call):
            return {
                "ok": True,
                "tool": "web_search",
                "query": "current example",
                "results": [
                    {"title": "First result", "url": "https://example.com/one"},
                    {"title": "Second result", "url": "https://example.com/two"},
                ],
                "result_count": 2,
            }

        original_runtime_json = main.call_openai_runtime_json
        original_execute = main.execute_b1_tool_call
        main.call_openai_runtime_json = fake_runtime_json
        main.execute_b1_tool_call = fake_execute
        self.addCleanup(lambda: setattr(main, "call_openai_runtime_json", original_runtime_json))
        self.addCleanup(lambda: setattr(main, "execute_b1_tool_call", original_execute))

        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "Search current example."}],
            b1_tools=["web_search"],
            b1_tool_max_iterations=1,
        )
        response = await main.call_chat_with_b1_tools(
            payload,
            main.strip_b1_chat_fields(payload.model_dump(exclude_none=True)),
            SimpleNamespace(runtime="localai", resolved_model_version="model@v1", public_alias="chat-default"),
            ["web_search"],
            model_tools.ModelToolRegistry(model_tools.ModelToolSettings(allowed_tools=("web_search",))),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertIn("1. First result - https://example.com/one", body["choices"][0]["message"]["content"])
        self.assertEqual(response.headers["x-b1-tool-stop-reason"], "max_iterations")
        self.assertEqual(response.headers["x-b1-tool-answer"], "synthesized")

    async def test_chat_tool_loop_synthesizes_custom_json_message(self) -> None:
        calls: list[dict[str, object]] = []
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "b1-live-echo", "arguments": json.dumps({"message": "b1-custom-tool-proof"})},
        }

        async def fake_runtime_json(path, payload, resolution, operation, owner_id=None):
            calls.append(payload)
            if len(calls) <= 2:
                return JSONResponse({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_call]}}]})
            return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "unused"}}]})

        async def fake_execute(_registry, _tool_call):
            return {
                "ok": True,
                "tool": "b1-live-echo",
                "json": {"message": "b1-custom-tool-proof"},
                "url": "https://postman-echo.com/post",
            }

        original_runtime_json = main.call_openai_runtime_json
        original_execute = main.execute_b1_tool_call
        main.call_openai_runtime_json = fake_runtime_json
        main.execute_b1_tool_call = fake_execute
        self.addCleanup(lambda: setattr(main, "call_openai_runtime_json", original_runtime_json))
        self.addCleanup(lambda: setattr(main, "execute_b1_tool_call", original_execute))

        payload = main.ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "Call the echo tool and return the echoed message only."}],
            b1_tools=["b1-live-echo"],
            b1_tool_max_iterations=4,
        )
        response = await main.call_chat_with_b1_tools(
            payload,
            main.strip_b1_chat_fields(payload.model_dump(exclude_none=True)),
            SimpleNamespace(runtime="localai", resolved_model_version="model@v1", public_alias="chat-default"),
            ["b1-live-echo"],
            model_tools.ModelToolRegistry(
                model_tools.ModelToolSettings(allowed_tools=("b1-live-echo",)),
                definitions=[
                    model_tools.ModelToolDefinition(
                        name="b1-live-echo",
                        enabled=True,
                        kind="http-json",
                        display_name="B1 live echo",
                        description="Echo a message",
                        parameters_schema={"type": "object"},
                        config={"url": "https://postman-echo.com/post", "method": "POST"},
                    )
                ],
            ),
            owner_id="user_1",
        )

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "b1-custom-tool-proof")
        self.assertEqual(response.headers["x-b1-tool-answer"], "synthesized")


if __name__ == "__main__":
    unittest.main()
