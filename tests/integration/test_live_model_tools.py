from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from tests.support.evidence import write_private_json  # noqa: E402
from test_live_stack import LiveApiClient, response_header  # noqa: E402


MODEL_TOOLS_EVIDENCE_FORMAT = "b1-ai-hub-model-tools-acceptance/v1"
REQUIRED_CHECKS = (
    "tool_registry_advertises_builtins",
    "web_fetch_chat_completed",
    "web_search_chat_completed",
    "web_fetch_responses_completed",
    "api_client_default_tools_completed",
    "custom_http_json_tool_completed",
)


def chat_message_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def responses_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


def redacted_header_subset(headers: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): value[:200]
        for key, value in headers.items()
        if key.lower().startswith("x-b1-tool") or key.lower() == "x-b1-tools"
    }


def policy_payload(policy: dict[str, Any], *, allowed_tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "enabled": bool(policy.get("enabled", True)),
        "allowed_tools": allowed_tools if allowed_tools is not None else list(policy.get("allowed_tools") or []),
        "allow_private_network": bool(policy.get("allow_private_network", False)),
        "allowed_hosts": list(policy.get("allowed_hosts") or []),
        "max_result_chars": int(policy.get("max_result_chars") or 12000),
        "max_search_results": int(policy.get("max_search_results") or 5),
        "timeout_seconds": float(policy.get("timeout_seconds") or 12.0),
        "search_endpoint_template": str(policy.get("search_endpoint_template") or "https://duckduckgo.com/html/?q={query}"),
        "notes": str(policy.get("notes") or ""),
    }


@unittest.skipUnless(os.getenv("B1_MODEL_TOOLS_LIVE_TEST") == "1", "set B1_MODEL_TOOLS_LIVE_TEST=1 to run live model-tool acceptance")
class LiveModelToolsAcceptanceTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    created_tool_names: set[str] = set()
    created_api_client_ids: set[str] = set()
    original_policy: dict[str, Any] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.created_tool_names = set()
        cls.created_api_client_ids = set()
        cls.original_policy = None
        api_key = (
            os.getenv("B1_MODEL_TOOLS_API_KEY")
            or os.getenv("B1_SMOKE_ADMIN_API_KEY")
            or os.getenv("B1_AI_HUB_API_KEY")
            or ""
        )
        if not api_key:
            raise unittest.SkipTest("set B1_MODEL_TOOLS_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
        tls_verify = os.getenv("B1_MODEL_TOOLS_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        cls.client = LiveApiClient(
            os.getenv("B1_MODEL_TOOLS_API_BASE")
            or os.getenv("B1_SMOKE_API_BASE")
            or os.getenv("B1_AI_HUB_API_BASE")
            or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_MODEL_TOOLS_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_MODEL_TOOLS_HTTP_TIMEOUT_SECONDS", "180")),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_MODEL_TOOLS_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.model = os.getenv("B1_MODEL_TOOLS_CHAT_MODEL", "chat-default")
        cls.max_iterations = int(os.getenv("B1_MODEL_TOOLS_MAX_ITERATIONS", "4"))
        cls.custom_url = os.getenv("B1_MODEL_TOOLS_CUSTOM_URL", "https://postman-echo.com/post").strip()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.original_policy is not None:
            try:
                cls.client.json_request("PUT", "/admin/model-tools/policy", body=policy_payload(cls.original_policy), require_auth=True)
            except Exception:
                pass
        for name in sorted(cls.created_tool_names):
            try:
                cls.client.request("DELETE", f"/admin/model-tools/{quote(name, safe='')}", require_auth=True)
            except Exception:
                pass
        for client_id in sorted(cls.created_api_client_ids):
            try:
                cls.client.request("DELETE", f"/admin/api-clients/{quote(client_id, safe='')}", require_auth=True)
            except Exception:
                pass
        evidence_path = os.getenv("B1_MODEL_TOOLS_EVIDENCE", "").strip()
        if not evidence_path:
            return
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in REQUIRED_CHECKS) else "incomplete"
        write_private_json(
            Path(evidence_path),
            {
                "format": MODEL_TOOLS_EVIDENCE_FORMAT,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "base_url": cls.client.base_url,
                "status": status,
                "required_checks": list(REQUIRED_CHECKS),
                "checks": cls.checks,
                "samples": cls.samples,
                "model": cls.model,
            },
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.__class__.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def json_request(self, method: str, path: str, *, body: dict[str, Any] | None = None, expected: int = 200) -> tuple[dict[str, str], Any]:
        status, headers, payload = self.client.json_request(method, path, body=body, require_auth=True)
        if status == 403:
            self.skipTest(f"provided acceptance key lacks scope for {method} {path}")
        self.assertEqual(status, expected, payload)
        return headers, payload

    def chat_with_tools(self, tools: list[str], prompt: str, *, max_tokens: int = 256) -> tuple[dict[str, str], dict[str, Any], str]:
        headers, payload = self.json_request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": False,
                "runtime_policy": "non_comfy_only",
                "b1_tools": tools,
                "b1_tool_max_iterations": self.max_iterations,
            },
        )
        self.assertIsInstance(payload, dict)
        content = chat_message_content(payload)
        self.assertTrue(content.strip(), payload)
        self.assertNotIn("b1_tool_call", content)
        self.assertNotIn('"tool_calls"', content)
        return headers, payload, content

    def responses_with_tools(self, tools: list[str], prompt: str, *, max_tokens: int = 256) -> tuple[dict[str, str], dict[str, Any], str]:
        headers, payload = self.json_request(
            "POST",
            "/v1/responses",
            body={
                "model": self.model,
                "input": prompt,
                "temperature": 0,
                "max_output_tokens": max_tokens,
                "stream": False,
                "runtime_policy": "non_comfy_only",
                "b1_tools": tools,
                "b1_tool_max_iterations": self.max_iterations,
            },
        )
        self.assertIsInstance(payload, dict)
        content = responses_output_text(payload)
        self.assertTrue(content.strip(), payload)
        self.assertNotIn("b1_tool_call", content)
        self.assertNotIn('"tool_calls"', content)
        return headers, payload, content

    def test_tool_registry_advertises_builtins(self) -> None:
        _, payload = self.json_request("GET", "/v1/tools")
        self.assertIsInstance(payload, dict)
        tools = payload.get("allowed_tools")
        definitions = payload.get("definitions")
        self.assertIsInstance(tools, list, payload)
        self.assertIsInstance(definitions, list, payload)
        for name in ("web_fetch", "web_search"):
            self.assertIn(name, tools)
        self.assertTrue(payload.get("streaming_supported"), payload)
        self.assertEqual(payload.get("request_field"), "b1_tools")
        definition_names = sorted(
            item.get("function", {}).get("name")
            for item in definitions
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        )
        self.__class__.samples.append(
            {
                "label": "model-tools-registry",
                "allowed_tools": sorted(str(item) for item in tools),
                "definition_names": [str(item) for item in definition_names if item],
                "streaming_supported": bool(payload.get("streaming_supported")),
                "private_network_blocked_by_default": bool((payload.get("security") or {}).get("private_network_blocked_by_default")),
            }
        )
        self.record_check(
            "tool_registry_advertises_builtins",
            allowed_tools=sorted(str(item) for item in tools),
            definition_count=len(definitions),
            request_field=payload.get("request_field"),
            streaming_supported=bool(payload.get("streaming_supported")),
        )

    def test_web_fetch_chat_completed(self) -> None:
        prompt = (
            "Call the web_fetch tool with exactly this JSON arguments object: {\"url\":\"https://example.com/\"}. "
            "Then answer with the page title plus one short fact from the tool result. Do not answer from memory."
        )
        headers, _, content = self.chat_with_tools(["web_fetch"], prompt)
        lowered = content.lower()
        self.assertNotIn("tool call failed", lowered)
        self.assertIn("example", lowered)
        self.assertIn("domain", lowered)
        x_tools = response_header(headers, "x-b1-tools")
        self.assertIn("web_fetch", x_tools)
        self.__class__.samples.append(
            {
                "label": "web-fetch-chat",
                "model": self.model,
                "content_excerpt": content[:500],
                "headers": redacted_header_subset(headers),
            }
        )
        self.record_check(
            "web_fetch_chat_completed",
            model=self.model,
            response_excerpt=content[:500],
            tool_headers=redacted_header_subset(headers),
            tool_header=x_tools,
            synthesized=response_header(headers, "x-b1-tool-answer") == "synthesized",
        )

    def test_web_search_chat_completed(self) -> None:
        prompt = (
            "Use the web_search tool for the query 'example domain'. "
            "Answer with two result titles and URLs from the tool result."
        )
        headers, _, content = self.chat_with_tools(["web_search"], prompt)
        lowered = content.lower()
        self.assertIn("example", lowered)
        self.assertIn("http", lowered)
        x_tools = response_header(headers, "x-b1-tools")
        self.assertIn("web_search", x_tools)
        self.__class__.samples.append(
            {
                "label": "web-search-chat",
                "model": self.model,
                "content_excerpt": content[:500],
                "headers": redacted_header_subset(headers),
            }
        )
        self.record_check(
            "web_search_chat_completed",
            model=self.model,
            response_excerpt=content[:500],
            tool_headers=redacted_header_subset(headers),
            tool_header=x_tools,
            synthesized=response_header(headers, "x-b1-tool-answer") == "synthesized",
        )

    def test_web_fetch_responses_completed(self) -> None:
        prompt = (
            "Call the web_fetch tool with exactly this JSON arguments object: {\"url\":\"https://example.com/\"}. "
            "Then answer with the page title plus one short fact from the tool result. Do not answer from memory."
        )
        headers, payload, content = self.responses_with_tools(["web_fetch"], prompt)
        lowered = content.lower()
        self.assertEqual(payload.get("object"), "response", payload)
        self.assertNotIn("tool call failed", lowered)
        self.assertIn("example", lowered)
        self.assertIn("domain", lowered)
        x_tools = response_header(headers, "x-b1-tools")
        self.assertIn("web_fetch", x_tools)
        self.__class__.samples.append(
            {
                "label": "web-fetch-responses",
                "model": self.model,
                "content_excerpt": content[:500],
                "headers": redacted_header_subset(headers),
            }
        )
        self.record_check(
            "web_fetch_responses_completed",
            model=self.model,
            response_excerpt=content[:500],
            tool_headers=redacted_header_subset(headers),
            tool_header=x_tools,
            response_object=payload.get("object"),
            synthesized=response_header(headers, "x-b1-tool-answer") == "synthesized",
        )

    def test_api_client_default_tools_completed(self) -> None:
        _, created = self.json_request(
            "POST",
            "/admin/api-clients",
            body={
                "display_name": "B1 model-tool live default client",
                "role": "service",
                "scopes": ["models:read", "inference:write"],
                "cidr_allowlist": [],
                "default_b1_tools": ["web_fetch"],
            },
            expected=200,
        )
        self.assertIsInstance(created, dict)
        api_key = str(created.get("api_key") or "")
        client_id = str(created.get("id") or "")
        self.assertTrue(api_key.startswith("b1k_"), created)
        self.assertTrue(client_id.startswith("client_"), created)
        self.__class__.created_api_client_ids.add(client_id)
        default_client = LiveApiClient(
            self.client.base_url,
            api_key=api_key,
            host_header=self.client.host_header,
            timeout_seconds=self.client.timeout_seconds,
            tls_verify=False,
            allow_insecure_http=self.client.allow_insecure_http,
            resolve_hosts=self.client.resolve_hosts,
        )
        default_client.context = self.client.context
        status, headers, payload = default_client.json_request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Use your available tool to fetch https://example.com/. "
                            "Then answer with the page title and do not mention implementation details."
                        ),
                    }
                ],
                "temperature": 0,
                "max_tokens": 256,
                "stream": False,
                "runtime_policy": "non_comfy_only",
            },
            require_auth=True,
        )
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        content = chat_message_content(payload)
        self.assertTrue(content.strip(), payload)
        lowered = content.lower()
        self.assertIn("example", lowered)
        self.assertIn("domain", lowered)
        x_tools = response_header(headers, "x-b1-tools")
        self.assertIn("web_fetch", x_tools)
        self.__class__.samples.append(
            {
                "label": "api-client-default-tools-chat",
                "model": self.model,
                "client_id": client_id,
                "content_excerpt": content[:500],
                "headers": redacted_header_subset(headers),
            }
        )
        self.record_check(
            "api_client_default_tools_completed",
            model=self.model,
            client_id=client_id,
            default_b1_tools=created.get("default_b1_tools") or [],
            response_excerpt=content[:500],
            tool_headers=redacted_header_subset(headers),
            tool_header=x_tools,
            synthesized=response_header(headers, "x-b1-tool-answer") == "synthesized",
        )

    def test_custom_http_json_tool_completed(self) -> None:
        if not self.custom_url:
            self.skipTest("set B1_MODEL_TOOLS_CUSTOM_URL to run the custom HTTP JSON proof")
        tool_name = "b1-live-echo-" + uuid.uuid4().hex[:10]
        _, registry = self.json_request("GET", "/admin/model-tools")
        self.assertIsInstance(registry, dict)
        original_policy = registry.get("policy")
        self.assertIsInstance(original_policy, dict, registry)
        self.__class__.original_policy = dict(original_policy)
        original_allowed = [str(item) for item in original_policy.get("allowed_tools") or []]
        allowed = sorted(set(original_allowed + ["web_search", "web_fetch", tool_name]))
        _, tool_payload = self.json_request(
            "PUT",
            f"/admin/model-tools/{quote(tool_name, safe='')}",
            body={
                "enabled": True,
                "kind": "http-json",
                "display_name": "B1 live echo",
                "description": "Acceptance-only HTTP JSON echo tool.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                "config": {
                    "url": self.custom_url,
                    "method": "POST",
                    "max_result_chars": 4096,
                    "result_hint": "Use json.data.message when present.",
                },
                "visibility_roles": ["admin", "operator", "creator", "user"],
                "notes": "temporary live acceptance tool; deleted by the harness",
            },
        )
        self.__class__.created_tool_names.add(tool_name)
        self.assertIsInstance(tool_payload, dict)
        _, updated = self.json_request("PUT", "/admin/model-tools/policy", body=policy_payload(original_policy, allowed_tools=allowed))
        self.assertIn(tool_name, updated.get("allowed_tools") or [], updated)

        proof_text = "b1-custom-tool-proof"
        _, manual = self.json_request(
            "POST",
            f"/admin/model-tools/{quote(tool_name, safe='')}/execute",
            body={"arguments": {"message": proof_text}},
        )
        self.assertIsInstance(manual, dict)
        result = manual.get("result")
        self.assertIsInstance(result, dict, manual)
        self.assertTrue(result.get("ok"), result)
        self.assertIn(proof_text, json.dumps(result, sort_keys=True))

        headers, _, content = self.chat_with_tools(
            [tool_name],
            (
                f"Use the {tool_name} tool with JSON arguments {{\"message\":\"{proof_text}\"}}. "
                "Then answer with exactly the echoed message value and no extra explanation."
            ),
            max_tokens=128,
        )
        self.assertIn(proof_text, content)
        x_tools = response_header(headers, "x-b1-tools")
        self.assertIn(tool_name, x_tools)
        self.__class__.samples.append(
            {
                "label": "custom-http-json-chat",
                "model": self.model,
                "tool_name": tool_name,
                "manual_status_code": result.get("status_code"),
                "content_excerpt": content[:500],
                "headers": redacted_header_subset(headers),
            }
        )
        self.record_check(
            "custom_http_json_tool_completed",
            model=self.model,
            tool_name=tool_name,
            manual_status_code=result.get("status_code"),
            response_excerpt=content[:500],
            tool_headers=redacted_header_subset(headers),
            tool_header=x_tools,
            synthesized=response_header(headers, "x-b1-tool-answer") == "synthesized",
        )


if __name__ == "__main__":
    unittest.main()
