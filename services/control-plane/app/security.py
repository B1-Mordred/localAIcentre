from __future__ import annotations

import ipaddress
from urllib.parse import unquote, urlparse


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


def is_safe_public_import_url(url: str, approved_hosts: set[str] | None = None) -> bool:
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
        return True
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    return not any(ip in network for network in PRIVATE_NETS)


def canonical_artifact_name(name: str) -> str:
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    if cleaned in {"", ".", ".."}:
        raise ValueError("empty artifact name")
    if any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise ValueError("invalid artifact path")
    return cleaned
