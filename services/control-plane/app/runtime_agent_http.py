from __future__ import annotations

import ssl
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

    if verify_tls:
        context = ssl.create_default_context(cafile=ca_file or None)
    else:
        context = ssl._create_unverified_context()
    if client_cert_file and client_key_file:
        context.load_cert_chain(client_cert_file, client_key_file)
    elif client_cert_file:
        context.load_cert_chain(client_cert_file)
    return {"verify": context, "trust_env": False}
