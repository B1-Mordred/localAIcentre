#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import backup_migration_rollback as evidence_policy  # noqa: E402


def read_key_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate B1 backup, migration, and rollback acceptance evidence.")
    parser.add_argument("--b1-backup", required=True, help="B1 AI Hub backup directory.")
    parser.add_argument("--restore-report", required=True, help="restore-report.json from a restore-to-alternate-directory rehearsal.")
    parser.add_argument("--inventory", required=True, help="Reviewed old-stack inventory JSON.")
    parser.add_argument("--old-stack-backup", required=True, help="Verified old-stack backup directory.")
    parser.add_argument("--open-webui-plan", required=True, help="Reviewed Open WebUI migration plan JSON.")
    parser.add_argument("--cutover-plan", required=True, help="Reviewed cutover/rollback plan JSON with no unresolved warnings.")
    parser.add_argument("--rollback-report", required=True, help="Rollback rehearsal report JSON.")
    parser.add_argument("--output", required=True, help="Output evidence JSON under $B1_BACKUP_ROOT/acceptance.")
    parser.add_argument("--backup-encryption-key-file", default=None, help="Key file for encrypted-only B1 backup archives.")
    args = parser.parse_args()
    try:
        payload = evidence_policy.build_evidence(
            b1_backup=Path(args.b1_backup),
            restore_report=Path(args.restore_report),
            inventory=Path(args.inventory),
            old_stack_backup_path=Path(args.old_stack_backup),
            open_webui_plan=Path(args.open_webui_plan),
            cutover_plan=Path(args.cutover_plan),
            rollback_report=Path(args.rollback_report),
            backup_encryption_key=read_key_file(args.backup_encryption_key_file) or None,
        )
        output = evidence_policy.write_evidence(Path(args.output), payload)
    except evidence_policy.EvidenceError as exc:
        raise SystemExit(f"backup/migration/rollback evidence failed: {exc}") from exc
    print(json.dumps({"status": payload["status"], "output": str(output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
