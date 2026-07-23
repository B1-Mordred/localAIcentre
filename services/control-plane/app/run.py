from __future__ import annotations

import os

import uvicorn

from . import migrate


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    if bool_env("B1_DB_MIGRATIONS_ENABLED", True):
        migrate.upgrade("head")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
