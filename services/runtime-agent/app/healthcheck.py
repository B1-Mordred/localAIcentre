from __future__ import annotations

import os
import ssl
import urllib.request


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    if not bool_env("B1_RUNTIME_AGENT_MTLS_ENABLED", True):
        urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5).read()
        return

    context = ssl.create_default_context(cafile=os.getenv("B1_RUNTIME_AGENT_TLS_CA_FILE", "/run/secrets/runtime_agent_mtls_ca.crt"))
    context.load_cert_chain(
        os.getenv("B1_RUNTIME_AGENT_CLIENT_CERT_FILE", "/run/secrets/runtime_agent_client.crt"),
        os.getenv("B1_RUNTIME_AGENT_CLIENT_KEY_FILE", "/run/secrets/runtime_agent_client.key"),
    )
    context.check_hostname = False
    urllib.request.urlopen("https://127.0.0.1:8443/healthz", context=context, timeout=5).read()


if __name__ == "__main__":
    main()
