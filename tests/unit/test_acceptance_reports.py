from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import acceptance  # noqa: E402

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def sample_report(**overrides: Any) -> dict[str, Any]:
    report_id = overrides.pop("report_id", "acceptance-20260724t120000z-deadbeef")
    self_test = overrides.pop(
        "self_test",
        {
            "status": "ok",
            "checks": [
                {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                {"name": "runtimes:production-readiness", "status": "ok", "detail": "required runtimes are production-ready"},
            ],
        },
    )
    return acceptance.build_report(
        report_id=report_id,
        created_by="admin_1",
        label=overrides.pop("label", "cutover dry run"),
        notes=overrides.pop("notes", "operator notes"),
        generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        runtime_deployment_mode=overrides.pop("runtime_deployment_mode", "production"),
        resource_policy=overrides.pop("resource_policy", {"gpu_total_vram_gib": 12.0, "host_total_ram_gib": 32.0}),
        maintenance=overrides.pop("maintenance", {"enabled": True, "reason": "cutover validation"}),
        self_test=self_test,
        metrics=overrides.pop(
            "metrics",
            {
                "gpu": {"available": True, "device_count": 1, "memory_total_mib": 12288, "memory_used_mib": 1024},
                "jobs": {"completed_last_hour": 4, "failed_last_hour": 0, "cancelled_last_hour": 0, "recovery_required_last_hour": 0},
            },
        ),
        admission=overrides.pop("admission", {"storage": {"root": "/srv/b1-ai-hub"}}),
        scheduler_lease=overrides.pop("scheduler_lease", {"owner": "idle"}),
        runtime_states=overrides.pop("runtime_states", [{"runtime": "comfyui", "status": "idle", "stage": "idle"}]),
        runtime_reservations=overrides.pop("runtime_reservations", []),
    )


class AcceptanceReportTests(unittest.TestCase):
    def test_report_writer_persists_json_markdown_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = sample_report()
            summary = acceptance.write_report(root, report)

            report_dir = root / report["id"]
            json_path = report_dir / "report.json"
            markdown_path = report_dir / "report.md"
            checksum_path = report_dir / "SHA256SUMS"

            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertTrue(checksum_path.is_file())
            self.assertEqual(summary["id"], report["id"])
            self.assertTrue(summary["operator_handoff_ready"])
            self.assertIn("Operator handoff ready: true", markdown_path.read_text(encoding="utf-8"))

            checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
            checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
            self.assertEqual(checksums["report.json"], hashlib.sha256(json_path.read_bytes()).hexdigest())
            self.assertEqual(checksums["report.md"], hashlib.sha256(markdown_path.read_bytes()).hexdigest())

            loaded = acceptance.load_report(root, report["id"])
            listed = acceptance.list_reports(root)

        self.assertEqual(loaded["format"], acceptance.REPORT_FORMAT)
        self.assertEqual([item["id"] for item in listed], [report["id"]])

    def test_report_blocks_handoff_for_degraded_development_snapshot(self) -> None:
        report = sample_report(
            runtime_deployment_mode="development",
            self_test={
                "status": "degraded",
                "checks": [
                    {"name": "gpu:nvml", "status": "warning", "detail": "dev host"},
                    {"name": "runtimes:production-readiness", "status": "warning", "detail": "placeholder runtimes"},
                ],
            },
            metrics={"gpu": {"available": False}, "jobs": {}},
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("self-test status is degraded", report["acceptance_blockers"])
        self.assertIn("runtime deployment mode is not production", report["acceptance_blockers"])
        self.assertIn("required runtimes are not production-ready", report["acceptance_blockers"])

    def test_report_id_rejects_traversal(self) -> None:
        with self.assertRaises(acceptance.AcceptanceReportError):
            acceptance.report_directory(Path("/tmp/b1"), "../escape")


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AcceptanceReportApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, role: Any = None, scopes: frozenset[str] = frozenset({"*"})) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role or Role.ADMIN, scopes=scopes)

        self.patch_attr("authenticate", authenticate)

    def test_create_list_and_get_acceptance_report_endpoint(self) -> None:
        audit_events: list[dict[str, Any]] = []

        async def build_snapshot(auth: Any, payload: Any) -> dict[str, Any]:
            return sample_report(label=payload.label, notes=payload.notes)

        async def record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_auth()
        self.patch_attr("build_acceptance_report_snapshot", build_snapshot)
        self.patch_attr("record_audit_event", record_audit_event)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.patch_attr("acceptance_report_root_path", lambda: root)

            created = asyncio.run(
                main.admin_acceptance_report_create(
                    main.AcceptanceReportCreate(label="cutover", notes="validated on temporary hostnames"),
                    authorization="Bearer key",
                )
            )
            listed = asyncio.run(main.admin_acceptance_reports(authorization="Bearer key", limit=10))
            fetched = asyncio.run(main.admin_acceptance_report_get(created["report"]["id"], authorization="Bearer key"))

        self.assertEqual(created["summary"]["status"], "ok")
        self.assertEqual(created["report"]["label"], "cutover")
        self.assertEqual(listed["data"][0]["id"], created["report"]["id"])
        self.assertEqual(fetched["report"]["id"], created["report"]["id"])
        self.assertEqual(audit_events[0]["event_type"], "acceptance_report.created")
        self.assertEqual(audit_events[0]["target_type"], "acceptance_report")

    def test_acceptance_report_create_requires_admin_write_scope(self) -> None:
        self.patch_auth(scopes=frozenset({"admin:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.admin_acceptance_report_create(
                    main.AcceptanceReportCreate(),
                    authorization="Bearer key",
                )
            )

        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
