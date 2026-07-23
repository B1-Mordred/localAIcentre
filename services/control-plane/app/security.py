from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


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
    if not parsed.hostname:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if approved_hosts and hostname not in approved_hosts:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not any(ip in network for network in PRIVATE_NETS)


def canonical_artifact_name(name: str) -> str:
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    if cleaned in {"", ".", ".."}:
        raise ValueError("empty artifact name")
    if any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise ValueError("invalid artifact path")
    return cleaned
