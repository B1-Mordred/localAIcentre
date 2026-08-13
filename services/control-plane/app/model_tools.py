from __future__ import annotations

import html
import ipaddress
import inspect
import json
import re
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

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
BLOCK_TAG_RE = re.compile(r"(?is)</?(?:article|aside|blockquote|br|dd|div|dl|dt|figcaption|figure|footer|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|td|th|thead|tr|ul)\b[^>]*>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
WORD_RE = re.compile(r"[\w-]{2,}", re.UNICODE)
HostnameResolver = Callable[[str, int | None], list[str]]
ToolHeaderProvider = Callable[["ModelToolDefinition"], Awaitable[dict[str, str]] | dict[str, str]]
INTERNAL_SEARCH_HOSTS = {"tool-search"}
DEFAULT_WEB_FETCH_CHARS = 5000
WEB_FETCH_CACHE_TTL_SECONDS = 900.0
WEB_FETCH_CACHE_MAX_ENTRIES = 32
WEB_FETCH_CLEAN_TEXT_MAX_CHARS = 200000
WEB_FETCH_STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "what", "when", "where", "which", "who", "with"}
WEB_FETCH_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


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


def html_to_structured_text(value: str, *, max_chars: int = WEB_FETCH_CLEAN_TEXT_MAX_CHARS) -> str:
    stripped = SCRIPT_STYLE_RE.sub(" ", value)
    stripped = BLOCK_TAG_RE.sub("\n", stripped)
    stripped = TAG_RE.sub(" ", stripped)
    decoded = html.unescape(stripped).replace("\r", "\n")
    lines = [WHITESPACE_RE.sub(" ", line).strip() for line in decoded.split("\n")]
    return "\n".join(line for line in lines if line)[:max_chars]


def text_passages(value: str, *, target_chars: int = 700) -> list[str]:
    passages: list[str] = []
    for block in (line.strip() for line in value.splitlines()):
        if not block:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", block) if len(block) > target_chars * 2 else [block]
        current: list[str] = []
        current_chars = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and current_chars + len(sentence) + 1 > target_chars:
                passages.append(" ".join(current))
                current = []
                current_chars = 0
            current.append(sentence)
            current_chars += len(sentence) + 1
        if current:
            passages.append(" ".join(current))
    return passages


def query_terms(value: str) -> set[str]:
    return {term.casefold() for term in WORD_RE.findall(value) if term.casefold() not in WEB_FETCH_STOP_WORDS}


def select_relevant_passages(value: str, query: str, *, max_chars: int) -> tuple[str, int, int]:
    passages = text_passages(value)
    if not passages:
        return value[:max_chars], 0, 0
    terms = query_terms(query)
    if terms:
        ranked: list[tuple[float, int]] = []
        for index, passage in enumerate(passages):
            words = [word.casefold() for word in WORD_RE.findall(passage)]
            overlap = terms & set(words)
            score = len(overlap) * 3.0 + sum(words.count(term) for term in overlap) * 20.0 / max(1, len(words))
            ranked.append((score, index))
        selected_indexes = [index for score, index in sorted(ranked, key=lambda item: (-item[0], item[1])) if score > 0]
    else:
        selected_indexes = []
    if not selected_indexes:
        selected_indexes = list(range(len(passages)))
    selected: list[str] = []
    used = 0
    for index in selected_indexes:
        passage = passages[index]
        remaining = max_chars - used - (2 if selected else 0)
        if remaining <= 0:
            break
        selected.append(passage[:remaining])
        used += min(len(passage), remaining) + (2 if len(selected) > 1 else 0)
    return "\n\n".join(selected), len(selected), len(passages)


def cached_web_page(url: str) -> dict[str, Any] | None:
    entry = WEB_FETCH_CACHE.get(url)
    if entry is None:
        return None
    stored_at, page = entry
    if time.monotonic() - stored_at > WEB_FETCH_CACHE_TTL_SECONDS:
        WEB_FETCH_CACHE.pop(url, None)
        return None
    WEB_FETCH_CACHE.move_to_end(url)
    return dict(page)


def store_cached_web_page(url: str, page: dict[str, Any]) -> None:
    WEB_FETCH_CACHE[url] = (time.monotonic(), dict(page))
    WEB_FETCH_CACHE.move_to_end(url)
    while len(WEB_FETCH_CACHE) > WEB_FETCH_CACHE_MAX_ENTRIES:
        WEB_FETCH_CACHE.popitem(last=False)


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


def search_result_is_provider_navigation(result_url: str, search_url: str) -> bool:
    result_host = (urlparse(result_url).hostname or "").lower().rstrip(".")
    search_host = (urlparse(search_url).hostname or "").lower().rstrip(".")
    return bool(result_host and search_host and result_host == search_host)


def parse_google_news_rss(value: bytes, *, max_results: int) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError:
        return []
    results: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = WHITESPACE_RE.sub(" ", str(item.findtext("title") or "")).strip()
        link = str(item.findtext("link") or "").strip()
        if title and link.startswith(("http://", "https://")):
            results.append({"title": title[:240], "url": link})
        if len(results) >= max_results:
            break
    return results


def parse_json_search_results(value: Any, *, max_results: int) -> list[dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        return []
    results: list[dict[str, str]] = []
    for item in value["results"]:
        if not isinstance(item, dict):
            continue
        title = WHITESPACE_RE.sub(" ", str(item.get("title") or "")).strip()
        url = str(item.get("url") or "").strip()
        if title and url.startswith(("http://", "https://")):
            results.append({"title": title[:240], "url": url})
        if len(results) >= max_results:
            break
    return results


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
MODEL_TOOL_JSON_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
MODEL_TOOL_URL_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
UNSAFE_JSON_PATH_SEGMENTS = {"__proto__", "prototype", "constructor"}


def builtin_tool_definition(name: str, settings: ModelToolSettings) -> dict[str, Any] | None:
    if name == "web_search":
        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web for current information. Use this first for news or other time-sensitive questions; pass a plain-language query, not a search-engine URL.",
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
                "description": "Fetch relevant readable passages from a public source URL. Include query with the user's information need so B1 can select the best passages. Omit max_chars for the fast default; request more only if the first extract is insufficient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Public URL to retrieve."},
                        "query": {"type": "string", "description": "The user's question or the facts to extract from the page."},
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


def validate_json_result_path(value: str) -> str:
    path = value.strip()
    if not path:
        return ""
    if len(path) > 256:
        raise ModelToolError("json_result_path is too long")
    if not MODEL_TOOL_JSON_PATH_RE.fullmatch(path):
        raise ModelToolError("json_result_path must be a dot-separated path of JSON object keys or array indexes")
    segments = path.split(".")
    if any(segment in UNSAFE_JSON_PATH_SEGMENTS for segment in segments):
        raise ModelToolError("json_result_path contains an unsafe segment")
    return path


def resolve_json_result_path(value: Any, path: str) -> Any:
    current = value
    for segment in validate_json_result_path(path).split("."):
        if isinstance(current, dict):
            if segment not in current:
                raise ModelToolError(f"json_result_path segment not found: {segment}")
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdecimal():
            index = int(segment)
            if index >= len(current):
                raise ModelToolError(f"json_result_path index out of range: {segment}")
            current = current[index]
            continue
        raise ModelToolError(f"json_result_path segment cannot be applied: {segment}")
    return current


def bounded_json_payload(key: str, value: Any, *, max_chars: int) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return {key: value, f"{key}_truncated": False}
    return {f"{key}_text": encoded[:max_chars], f"{key}_truncated": True}


def url_template_variables(value: str) -> tuple[str, ...]:
    names: list[str] = []
    for match in MODEL_TOOL_URL_TEMPLATE_RE.finditer(value):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return tuple(names)


def expand_url_template(value: str, arguments: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    used: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in arguments:
            raise ModelToolError(f"URL template argument is missing: {name}")
        value = arguments[name]
        if isinstance(value, (dict, list)):
            raise ModelToolError(f"URL template argument must be a scalar: {name}")
        if value is None:
            raise ModelToolError(f"URL template argument is null: {name}")
        if name not in used:
            used.append(name)
        return quote(str(value), safe="")

    return MODEL_TOOL_URL_TEMPLATE_RE.sub(replace, value), tuple(used)


def arguments_without_template_values(arguments: dict[str, Any], used_template_arguments: tuple[str, ...]) -> dict[str, Any]:
    if not used_template_arguments:
        return dict(arguments)
    used = set(used_template_arguments)
    return {key: value for key, value in arguments.items() if key not in used}


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
        parsed_url = urlparse(url)
        if parsed_url.hostname in {"duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"}:
            raise ModelToolError("search-result pages must be queried with web_search, not web_fetch")
        query = str(arguments.get("query") or "").strip()[:1000]
        max_chars = int(arguments.get("max_chars") or min(DEFAULT_WEB_FETCH_CHARS, self.settings.max_result_chars))
        max_chars = max(256, min(max_chars, self.settings.max_result_chars))
        page = cached_web_page(url)
        cache_hit = page is not None
        if page is None:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
                response = await client.get(url, headers={"User-Agent": "B1-AI-Hub-ModelTools/0.2"})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            raw_text = response.text
            is_html = "html" in content_type.lower() or "<html" in raw_text[:500].lower()
            page = {
                "url": str(response.url),
                "content_type": content_type,
                "status_code": response.status_code,
                "title": html_title(raw_text) if is_html else "",
                "clean_text": html_to_structured_text(raw_text) if is_html else WHITESPACE_RE.sub(" ", raw_text).strip()[:WEB_FETCH_CLEAN_TEXT_MAX_CHARS],
            }
            store_cached_web_page(url, page)
        text, selected_count, passage_count = select_relevant_passages(str(page["clean_text"]), query, max_chars=max_chars)
        result = {
            "ok": True,
            "tool": "web_fetch",
            "url": str(page["url"]),
            "content_type": str(page["content_type"]),
            "status_code": int(page["status_code"]),
            "text": text,
            "truncated": len(str(page["clean_text"])) > len(text),
            "query": query,
            "extraction": "query_passages" if query else "progressive_start",
            "selected_passages": selected_count,
            "total_passages": passage_count,
            "max_chars": max_chars,
            "cache_hit": cache_hit,
        }
        if page.get("title"):
            result["title"] = str(page["title"])
        return result

    async def web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        max_results = int(arguments.get("max_results") or self.settings.max_search_results)
        max_results = max(1, min(max_results, self.settings.max_search_results))
        endpoint = self.settings.search_endpoint_template.replace("{query}", quote_plus(query))
        endpoint_host = (urlparse(endpoint).hostname or "").lower().rstrip(".")
        url = validate_tool_url(
            endpoint,
            allow_private_network=self.settings.allow_private_network or endpoint_host in INTERNAL_SEARCH_HOSTS,
            allowed_hosts=set(self.settings.allowed_hosts),
            resolver=self.resolver,
        )
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
            response = await client.get(url, headers={"User-Agent": "B1-AI-Hub-ModelTools/0.1"})
        response.raise_for_status()
        try:
            raw_results = parse_json_search_results(response.json(), max_results=max_results)
        except ValueError:
            raw_results = []
        if not raw_results:
            parser = SearchResultParser(str(response.url))
            parser.feed(response.text)
            raw_results = parser.results
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in raw_results:
            result_url = unwrap_search_result_url(result["url"])
            if search_result_is_provider_navigation(result_url, str(response.url)):
                continue
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
        source = str(response.url)
        if not deduped:
            fallback_endpoint = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=de&gl=DE&ceid=DE:de"
            fallback_url = validate_tool_url(
                fallback_endpoint,
                allow_private_network=self.settings.allow_private_network,
                allowed_hosts=set(self.settings.allowed_hosts),
                resolver=self.resolver,
            )
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=True, transport=self.transport) as client:
                fallback_response = await client.get(fallback_url, headers={"User-Agent": "B1-AI-Hub-ModelTools/0.1"})
            if fallback_response.status_code < 400:
                for result in parse_google_news_rss(fallback_response.content, max_results=max_results):
                    try:
                        safe_url = validate_tool_url(
                            result["url"],
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
                if deduped:
                    source = str(fallback_response.url)
        return {
            "ok": True,
            "tool": "web_search",
            "query": query,
            "results": deduped,
            "result_count": len(deduped),
            "source": source,
        }

    async def http_json_tool(self, definition: ModelToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_url = str(definition.config.get("url") or "").strip()
        method = str(definition.config.get("method") or "POST").strip().upper()
        if method not in {"GET", "POST"}:
            return {"ok": False, "tool": definition.name, "error": "http-json tools support only GET or POST"}
        try:
            expanded_url, used_template_arguments = expand_url_template(raw_url, arguments)
        except ModelToolError as exc:
            return {"ok": False, "tool": definition.name, "error": str(exc)}
        if "{" in expanded_url or "}" in expanded_url:
            return {"ok": False, "tool": definition.name, "error": "URL template contains an invalid placeholder"}
        url = validate_tool_url(
            expanded_url,
            allow_private_network=self.settings.allow_private_network,
            allowed_hosts=set(self.settings.allowed_hosts),
            resolver=self.resolver,
        )
        request_arguments = arguments_without_template_values(arguments, used_template_arguments)
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
                response = await client.get(url, params=request_arguments, headers=headers)
            else:
                response = await client.post(url, json=request_arguments, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        payload: dict[str, Any]
        ok = True
        if "json" in content_type.lower():
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if parsed is None:
                payload = {"text": response.text[:max_chars]}
            else:
                payload = {}
                json_result_path = validate_json_result_path(str(definition.config.get("json_result_path") or ""))
                include_raw_json = definition.config.get("include_raw_json", True)
                if json_result_path:
                    try:
                        extracted = resolve_json_result_path(parsed, json_result_path)
                        payload.update(
                            {
                                "extracted_path": json_result_path,
                                **bounded_json_payload("extracted", extracted, max_chars=max_chars),
                            }
                        )
                    except ModelToolError as exc:
                        ok = False
                        payload["error"] = str(exc)
                        payload["extracted_path"] = json_result_path
                if include_raw_json is not False:
                    payload.update(bounded_json_payload("json", parsed, max_chars=max_chars))
        else:
            text = html_to_text(response.text, max_chars=max_chars) if "html" in content_type.lower() else WHITESPACE_RE.sub(" ", response.text).strip()[:max_chars]
            payload = {"text": text}
        return {
            "ok": ok,
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
