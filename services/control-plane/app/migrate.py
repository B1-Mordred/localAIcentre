from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from .settings import load_settings


APP_ROOT = Path(__file__).resolve().parent


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(APP_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(APP_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url or load_settings().database_url)
    return config


def upgrade(revision: str = "head", database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), revision)


def current(database_url: str | None = None, verbose: bool = False) -> None:
    command.current(alembic_config(database_url), verbose=verbose)


def history(database_url: str | None = None, verbose: bool = False) -> None:
    command.history(alembic_config(database_url), verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run B1 AI Hub control-plane database migrations.")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL for this migration command.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade the database schema.")
    upgrade_parser.add_argument("revision", nargs="?", default="head")

    current_parser = subparsers.add_parser("current", help="Show the current applied migration revision.")
    current_parser.add_argument("--verbose", action="store_true")

    history_parser = subparsers.add_parser("history", help="Show migration history.")
    history_parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "upgrade":
        upgrade(args.revision, args.database_url)
    elif args.command == "current":
        current(args.database_url, verbose=args.verbose)
    elif args.command == "history":
        history(args.database_url, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
