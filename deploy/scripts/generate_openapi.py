#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE = ROOT / "services" / "control-plane"
DEFAULT_OUTPUT = ROOT / "docs" / "openapi.json"

DEFAULT_ENV = {
    "B1_DEV_AUTH_BYPASS": "true",
    "B1_JOB_RUNNER_ENABLED": "false",
    "B1_GPU_JOB_RUNNER_ENABLED": "false",
    "B1_MODEL_DOWNLOAD_RUNNER_ENABLED": "false",
    "B1_DATA_ROOT": "/srv/b1-ai-hub",
    "B1_BACKUP_ROOT": "/srv/b1-ai-hub/backups",
    "B1_RESTORE_TEST_ROOT": "/srv/b1-ai-hub/restore-tests",
    "B1_ARTIFACT_ROOT": "/srv/b1-ai-hub/artifacts",
    "B1_MODEL_CATALOG_DIR": str(ROOT / "model-catalog" / "seed"),
    "B1_WORKFLOW_SEED_DIR": str(ROOT / "workflows" / "approved"),
}


def apply_default_environment() -> None:
    for key, value in DEFAULT_ENV.items():
        os.environ.setdefault(key, value)


def openapi_schema() -> dict[str, Any]:
    apply_default_environment()
    sys.path.insert(0, str(CONTROL_PLANE))
    from app.main import app  # noqa: PLC0415

    schema = app.openapi()
    if not str(schema.get("openapi", "")).startswith("3.1."):
        raise RuntimeError(f"expected OpenAPI 3.1.x, got {schema.get('openapi')!r}")
    return schema


def schema_json() -> str:
    return json.dumps(openapi_schema(), indent=2, sort_keys=True) + "\n"


def check_file(path: Path, expected: str) -> int:
    if not path.exists():
        print(f"{path} does not exist; run: python3 deploy/scripts/generate_openapi.py --output {path}", file=sys.stderr)
        return 1
    current = path.read_text(encoding="utf-8")
    if current == expected:
        print(f"{path} is up to date")
        return 0
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=str(path),
        tofile=f"{path} (generated)",
    )
    sys.stderr.writelines(diff)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or validate the committed B1 AI Hub OpenAPI schema.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Schema output path. Defaults to docs/openapi.json.")
    parser.add_argument("--check", action="store_true", help="Fail when the output path differs from the generated schema.")
    args = parser.parse_args()

    expected = schema_json()
    output = args.output.resolve()
    if args.check:
        return check_file(output, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
