#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import rollback_rehearsal  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a non-destructive rollback rehearsal report for B1 AI Hub acceptance.")
    parser.add_argument("--cutover-plan", required=True, help="Reviewed cutover-plan JSON with no unresolved warnings.")
    parser.add_argument("--output", required=True, help="Output rollback rehearsal report JSON.")
    parser.add_argument("--rehearsed-by", required=True, help="Operator name or identifier for the rehearsal.")
    parser.add_argument("--rollback-commands-tested", action="store_true", help="Confirm rollback commands/actions were rehearsed.")
    parser.add_argument("--old-resources-preserved", action="store_true", help="Confirm old-stack resources are still preserved.")
    parser.add_argument("--notes", default=None, help="Optional operator notes.")
    args = parser.parse_args()
    try:
        report = rollback_rehearsal.build_report(
            cutover_plan_path=Path(args.cutover_plan),
            rehearsed_by=args.rehearsed_by,
            rollback_commands_tested=args.rollback_commands_tested,
            old_resources_preserved=args.old_resources_preserved,
            notes=args.notes,
        )
        output = rollback_rehearsal.write_report(Path(args.output), report)
    except rollback_rehearsal.RollbackRehearsalError as exc:
        raise SystemExit(f"rollback rehearsal report failed: {exc}") from exc
    print(json.dumps({"status": report["status"], "output": str(output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
