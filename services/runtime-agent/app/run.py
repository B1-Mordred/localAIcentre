from __future__ import annotations

import os
import ssl

import uvicorn


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    tls_enabled = bool_env("B1_RUNTIME_AGENT_MTLS_ENABLED", True)
    port = int(os.getenv("B1_RUNTIME_AGENT_PORT", "8443" if tls_enabled else "8000"))
    kwargs: dict[str, object] = {
        "host": "0.0.0.0",
        "port": port,
    }
    if tls_enabled:
        kwargs.update(
            {
                "ssl_certfile": os.getenv("B1_RUNTIME_AGENT_TLS_CERT_FILE", "/run/secrets/runtime_agent_server.crt"),
                "ssl_keyfile": os.getenv("B1_RUNTIME_AGENT_TLS_KEY_FILE", "/run/secrets/runtime_agent_server.key"),
                "ssl_ca_certs": os.getenv("B1_RUNTIME_AGENT_TLS_CA_FILE", "/run/secrets/runtime_agent_mtls_ca.crt"),
                "ssl_cert_reqs": ssl.CERT_REQUIRED
                if bool_env("B1_RUNTIME_AGENT_CLIENT_CERT_REQUIRED", True)
                else ssl.CERT_OPTIONAL,
            }
        )
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    main()
