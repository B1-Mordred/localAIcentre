from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlparse


PRIVATE_NETS = [
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
HostnameResolver = Callable[[str, int | None], list[str]]

CREDENTIAL_QUERY_KEY_EXACT = {
    "key",
    "sig",
}
CREDENTIAL_QUERY_KEY_FRAGMENTS = {
    "accesskeyid",
    "accesstoken",
    "apikey",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "downloadtoken",
    "password",
    "passwd",
    "secret",
    "securitytoken",
    "sharedaccesssignature",
    "signature",
    "token",
}


def query_key_may_carry_credentials(key: str) -> bool:
    lowered = key.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    if lowered in CREDENTIAL_QUERY_KEY_EXACT or compact in CREDENTIAL_QUERY_KEY_EXACT:
        return True
    return any(fragment in compact for fragment in CREDENTIAL_QUERY_KEY_FRAGMENTS)


def has_credential_query_parameter(query: str) -> bool:
    return any(query_key_may_carry_credentials(key) for key, _value in parse_qsl(query, keep_blank_values=True))


def public_import_ip_is_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global:
        return False
    return not any(ip in network for network in PRIVATE_NETS)


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


def is_safe_public_import_url(url: str, approved_hosts: set[str] | None = None, *, resolver: HostnameResolver | None = None) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"https"}:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.fragment:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    normalized_approved_hosts = {host.lower().rstrip(".") for host in approved_hosts or set()}
    if normalized_approved_hosts and hostname not in normalized_approved_hosts:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    decoded_path = unquote(parsed.path)
    if any(part in {".", ".."} for part in decoded_path.split("/") if part):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        resolver = resolver or resolve_hostname_addresses
        try:
            resolved_addresses = resolver(hostname, parsed.port)
        except OSError:
            return False
        if not resolved_addresses:
            return False
        for address in resolved_addresses:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                return False
            if not public_import_ip_is_safe(resolved_ip):
                return False
        return True
    return public_import_ip_is_safe(ip)


def canonical_artifact_name(name: str) -> str:
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    if cleaned in {"", ".", ".."}:
        raise ValueError("empty artifact name")
    if any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise ValueError("invalid artifact path")
    return cleaned
