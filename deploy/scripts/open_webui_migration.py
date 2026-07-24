#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.open_webui_migration import (  # noqa: E402,F401
    OpenWebUiMigrationError,
    annotate_database_coverage,
    backup_database_artifacts,
    backed_up_open_webui_container_metadata,
    build_and_write_plan,
    build_plan,
    choose_strategy,
    data_path_candidates,
    database_candidates,
    image_reference_metadata,
    load_inventory,
    load_json_file,
    load_verified_backup_manifest,
    open_webui_container_version_evidence,
    select_inputs,
    status,
    summarize_plan,
    unreadable_data_roots,
    utc_stamp,
    version_compatibility_status,
    write_plan,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a non-destructive Open WebUI preservation and migration plan.")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--output", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + f"/backups/open-webui-migration-plan-{utc_stamp()}.json")
    parser.add_argument("--restore-target", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + "/restore-tests/open-webui-migration")
    args = parser.parse_args()
    try:
        plan = build_plan(
            inventory_path=Path(args.inventory),
            backup_dir=Path(args.backup),
            restore_target=args.restore_target,
        )
        output = write_plan(plan, Path(args.output))
    except OpenWebUiMigrationError as exc:
        raise SystemExit(f"Open WebUI migration plan failed: {exc}") from exc
    print(f"wrote Open WebUI migration plan: {output}")
    print(json.dumps({"format": plan["format"], "strategy": plan["open_webui"]["recommended_strategy"], "warnings": plan["warnings"]}, indent=2))


if __name__ == "__main__":
    main()
