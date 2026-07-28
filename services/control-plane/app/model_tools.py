from __future__ import annotations

import html
import ipaddress
import inspect
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse, urlunparse

import httpx


PRIVATE_TOOL_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
WHITESPACE_RE = re.compile(r"\s+")
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
HostnameResolver = Callable[[str, int | None], list[str]]
ToolHeaderProvider = Callable[["ModelToolDefinition"], Awaitable[dict[str, str]] | dict[str, str]]


class ModelToolError(ValueError):
    pass


def resolve_hostname_addresses(hostname: str, port: int | None) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for result in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM):
        sockaddr = result[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses


def tool_ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private_network: bool) -> bool:
    if allow_private_network:
        return not ip.is_loopback and not ip.is_link_local and not ip.is_multicast and not ip.is_unspecified
    if not ip.is_global:
        return False
    return not any(ip in network for network in PRIVATE_TOOL_NETS)


def validate_tool_url(
    value: str,
    *,
    allow_private_network: bool = False,
    allowed_hosts: set[str] | None = None,
    resolver: HostnameResolver | None = None,
) -> str:
    raw = value.strip()
    if len(raw) > 2048:
        raise ModelToolError("URL is too long")
    parsed = urlparse(raw)
    if parsed.scheme not in {"https", "http"}:
        raise ModelToolError("URL must use http or https")
    if parsed.username or parsed.password:
        raise ModelToolError("URL must not contain credentials")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ModelToolError("URL has an invalid port") from exc
    if not parsed.hostname:
        raise ModelToolError("URL must include a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    normalized_allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts or set() if host.strip()}
    if normalized_allowed_hosts and hostname not in normalized_allowed_hosts:
        raise ModelToolError("URL host is not in the model-tool allowlist")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ModelToolError("URL must not target localhost")
    if BAD_PERCENT_ESCAPE_RE.search(parsed.path):
        raise ModelToolError("URL path contains an invalid percent escape")
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise ModelToolError("URL path is not valid UTF-8") from exc
    if any(part in {".", ".."} for part in decoded_path.split("/") if part):
        raise ModelToolError("URL path must not contain relative path segments")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        resolver = resolver or resolve_hostname_addresses
        try:
            resolved_addresses = resolver(hostname, port)
        except OSError as exc:
            raise ModelToolError("URL hostname could not be resolved safely") from exc
        if not resolved_addresses:
            raise ModelToolError("URL hostname could not be resolved safely")
        for address in resolved_addresses:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ModelToolError("URL hostname resolved to an invalid address") from exc
            if not tool_ip_is_public(resolved_ip, allow_private_network=allow_private_network):
                raise ModelToolError("URL hostname resolves to a blocked network range")
    else:
        if not tool_ip_is_public(ip, allow_private_network=allow_private_network):
            raise ModelToolError("URL targets a blocked network range")
    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def html_to_text(value: str, *, max_chars: int) -> str:
    stripped = SCRIPT_STYLE_RE.sub(" ", value)
    stripped = TAG_RE.sub(" ", stripped)
    text = html.unescape(stripped)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars]


class SearchResultParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.results: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = ""
        for key, value in attrs:
            if key.lower() == "href" and value:
                href = value
                break
        if href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            return
        self._current_href = urljoin(self.base_url, href)
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        title = WHITESPACE_RE.sub(" ", html.unescape(" ".join(self._current_text))).strip()
        if title and self._current_href.startswith(("http://", "https://")):
            self.results.append({"title": title[:240], "url": self._current_href})
        self._current_href = ""
        self._current_text = []


class HtmlMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "title" or not self._in_title:
            return
        self._in_title = False
        self.title = WHITESPACE_RE.sub(" ", html.unescape(" ".join(self._title_parts))).strip()[:300]


def html_title(value: str) -> str:
    parser = HtmlMetadataParser()
    parser.feed(value[:250000])
    return parser.title


def unwrap_search_result_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname.endswith("duckduckgo.com") and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return value


@dataclass(frozen=True)
class ModelToolSettings:
    enabled: bool = True
    allowed_tools: tuple[str, ...] = ("web_search", "web_fetch")
    allow_private_network: bool = False
    allowed_hosts: tuple[str, ...] = ()
    max_result_chars: int = 12000
    max_search_results: int = 5
    timeout_seconds: float = 12.0
    search_endpoint_template: str = "https://duckduckgo.com/html/?q={query}"


@dataclass(frozen=True)
class ModelToolDefinition:
    name: str
    enabled: bool
    kind: str
    display_name: str
    description: str
    parameters_schema: dict[str, Any]
    config: dict[str, Any]
    visibility_roles: tuple[str, ...] = ()
    notes: str = ""


BUILTIN_TOOL_NAMES = {"web_search", "web_fetch"}
MODEL_TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")


def builtin_tool_definition(name: str, settings: ModelToolSettings) -> dict[str, Any] | None:
    if name == "web_search":
        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web for current information. Use this before answering time-sensitive factual questions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": settings.max_search_results},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
    if name == "web_fetch":
        return {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch and extract readable text from a public HTTP or HTTPS URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Public URL to retrieve."},
                        "max_chars": {"type": "integer", "minimum": 256, "maximum": settings.max_result_chars},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        }
    return None


def safe_parameters_schema(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    schema = dict(value)
    if schema.get("type") != "object":
        schema["type"] = "object"
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        schema["properties"] = {}
    additional = schema.get("additionalProperties")
    if additional not in {True, False} and not isinstance(additional, dict):
        schema["additionalProperties"] = True
    return schema


def tool_definition_from_row(row: dict[str, Any]) -> ModelToolDefinition:
    return ModelToolDefinition(
        name=str(row.get("name") or ""),
        enabled=bool(row.get("enabled", True)),
        kind=str(row.get("kind") or ""),
        display_name=str(row.get("display_name") or row.get("name") or ""),
        description=str(row.get("description") or ""),
        parameters_schema=safe_parameters_schema(row.get("parameters_schema") if isinstance(row.get("parameters_schema"), dict) else {}),
        config=row.get("config") if isinstance(row.get("config"), dict) else {},
        visibility_roles=tuple(str(item) for item in row.get("visibility_roles") or [] if str(item).strip()),
        notes=str(row.get("notes") or ""),
    )


class ModelToolRegistry:
    def __init__(
        self,
        settings: ModelToolSettings,
        *,
        definitions: list[ModelToolDefinition] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostnameResolver | None = None,
        header_provider: ToolHeaderProvider | None = None,
    ) -> None:
        self.settings = settings
        self.custom_definitions = {definition.name: definition for definition in definitions or [] if definition.enabled}
        self.transport = transport
        self.resolver = resolver
        self.header_provider = header_provider

    def tool_names(self) -> set[str]:
        if not self.settings.enabled:
            return set()
        allowed = set(self.settings.allowed_tools)
        return {name for name in allowed if name in BUILTIN_TOOL_NAMES or name in self.custom_definitions}

    def definitions(self, requested: list[str]) -> list[dict[str, Any]]:
        enabled = self.tool_names()
        definitions: list[dict[str, Any]] = []
        for name in requested:
            if name not in enabled:
                continue
            builtin = builtin_tool_definition(name, self.settings)
            if builtin is not None:
                definitions.append(builtin)
                continue
            custom = self.custom_definitions.get(name)
            if custom is not None:
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": custom.name,
                            "description": custom.description or custom.display_name,
                            "parameters": custom.parameters_schema,
                        },
                    }
                )
        return definitions

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.tool_names():
            return {"ok": False, "error": f"tool is not enabled: {name}"}
        if name == "web_search":
            return await self.web_search(arguments)
        if name == "web_fetch":
            return await self.web_fetch(arguments)
        definition = self.custom_definitions.get(name)
        if definition is not None and definition.kind == "http-json":
            return await self.http_json_tool(definition, arguments)
        return {"ok": False, "error": f"unknown tool: {name}"}

    async def web_fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = validate_tool_url(
            str(arguments.get("url") or ""),
            allow_private_network=self.settings.allow_private_network,
            allowed_hosts=set(self.settings.allowed_hosts),
            resolver=self.resolver,
        )
        max_chars = int(arguments.get("max_chars") or self.settings.max_result_chars)
        max_chars = max(256, min(max_chars, self.settings.max_result_chars))
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
            response = await client.get(url, headers={"User-Agent": "B1-AI-Hub-ModelTools/0.1"})
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        text = response.text
        title = ""
        if "html" in content_type.lower() or "<html" in text[:500].lower():
            title = html_title(text)
            text = html_to_text(text, max_chars=max_chars)
        else:
            text = WHITESPACE_RE.sub(" ", text).strip()[:max_chars]
        result = {
            "ok": True,
            "tool": "web_fetch",
            "url": str(response.url),
            "content_type": content_type,
            "status_code": response.status_code,
            "text": text,
            "truncated": len(text) >= max_chars,
        }
        if title:
            result["title"] = title
        return result

    async def web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        max_results = int(arguments.get("max_results") or self.settings.max_search_results)
        max_results = max(1, min(max_results, self.settings.max_search_results))
        endpoint = self.settings.search_endpoint_template.replace("{query}", quote_plus(query))
        url = validate_tool_url(
            endpoint,
            allow_private_network=self.settings.allow_private_network,
            allowed_hosts=set(self.settings.allowed_hosts),
            resolver=self.resolver,
        )
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
            response = await client.get(url, headers={"User-Agent": "B1-AI-Hub-ModelTools/0.1"})
        response.raise_for_status()
        parser = SearchResultParser(str(response.url))
        parser.feed(response.text)
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in parser.results:
            result_url = unwrap_search_result_url(result["url"])
            try:
                safe_url = validate_tool_url(
                    result_url,
                    allow_private_network=self.settings.allow_private_network,
                    allowed_hosts=set(self.settings.allowed_hosts),
                    resolver=self.resolver,
                )
            except ModelToolError:
                continue
            if safe_url in seen:
                continue
            seen.add(safe_url)
            deduped.append({"title": result["title"], "url": safe_url})
            if len(deduped) >= max_results:
                break
        return {
            "ok": True,
            "tool": "web_search",
            "query": query,
            "results": deduped,
            "result_count": len(deduped),
            "source": str(response.url),
        }

    async def http_json_tool(self, definition: ModelToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_url = str(definition.config.get("url") or "").strip()
        method = str(definition.config.get("method") or "POST").strip().upper()
        if method not in {"GET", "POST"}:
            return {"ok": False, "tool": definition.name, "error": "http-json tools support only GET or POST"}
        url = validate_tool_url(
            raw_url,
            allow_private_network=self.settings.allow_private_network,
            allowed_hosts=set(self.settings.allowed_hosts),
            resolver=self.resolver,
        )
        max_chars = int(definition.config.get("max_result_chars") or self.settings.max_result_chars)
        max_chars = max(256, min(max_chars, self.settings.max_result_chars))
        headers = {"User-Agent": "B1-AI-Hub-ModelTools/0.1"}
        if self.header_provider is not None:
            provided = self.header_provider(definition)
            if inspect.isawaitable(provided):
                provided = await provided
            headers.update({str(key): str(value) for key, value in provided.items() if str(key).strip() and str(value).strip()})
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
            if method == "GET":
                response = await client.get(url, params=arguments, headers=headers)
            else:
                response = await client.post(url, json=arguments, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        payload: dict[str, Any]
        if "json" in content_type.lower():
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            payload = {"json": parsed} if parsed is not None else {"text": response.text[:max_chars]}
        else:
            text = html_to_text(response.text, max_chars=max_chars) if "html" in content_type.lower() else WHITESPACE_RE.sub(" ", response.text).strip()[:max_chars]
            payload = {"text": text}
        return {
            "ok": True,
            "tool": definition.name,
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            **payload,
        }


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
