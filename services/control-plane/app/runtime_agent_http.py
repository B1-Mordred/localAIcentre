from __future__ import annotations

from typing import Any


def runtime_agent_httpx_kwargs(
    base_url: str,
    *,
    ca_file: str = "",
    client_cert_file: str = "",
    client_key_file: str = "",
    verify_tls: bool = True,
) -> dict[str, Any]:
    if not base_url.lower().startswith("https://"):
        return {}

    kwargs: dict[str, Any] = {"verify": ca_file if verify_tls and ca_file else verify_tls}
    if client_cert_file and client_key_file:
        kwargs["cert"] = (client_cert_file, client_key_file)
    elif client_cert_file:
        kwargs["cert"] = client_cert_file
    return kwargs
