from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

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


class FakeArtifactDatabase:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.updated_jobs: list[tuple[str, list[dict[str, Any]]]] = []
        now = datetime.now(tz=UTC)
        target = root / "localai" / "job_old" / "0.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"data")
        protected = root / "voicebox" / "job_voice" / "0.wav"
        protected.parent.mkdir(parents=True)
        protected.write_bytes(b"wave")
        self.jobs = [
            {
                "id": "job_old",
                "owner_id": "owner_1",
                "state": "completed",
                "completed_at": now - timedelta(days=90),
                "native_prompt_id": None,
                "artifacts": [
                    {
                        "id": "artifact_job_old_0",
                        "url": "/artifacts/localai/job_old/0.png",
                        "path": "localai/job_old/0.png",
                        "bytes": 4,
                        "mime_type": "image/png",
                    }
                ],
            },
            {
                "id": "job_voice",
                "owner_id": "owner_1",
                "state": "completed",
                "completed_at": now - timedelta(days=90),
                "native_prompt_id": None,
                "artifacts": [
                    {
                        "id": "artifact_job_voice_0",
                        "url": "/artifacts/voicebox/job_voice/0.wav",
                        "path": "voicebox/job_voice/0.wav",
                        "bytes": 4,
                        "mime_type": "audio/wav",
                    }
                ],
            },
        ]

    async def list_artifact_retention_jobs(self, cutoff: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        return self.jobs[:limit]

    async def list_voice_profiles(self, include_deleted: bool = False, owner_id: str | None = None, runtime: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "voice_1", "sample_artifacts": [{"url": "/artifacts/voicebox/job_voice/0.wav"}]}]

    async def get_job_by_artifact_url(self, artifact_url: str) -> dict[str, Any] | None:
        for job in self.jobs:
            if any(isinstance(artifact, dict) and artifact.get("url") == artifact_url for artifact in job.get("artifacts") or []):
                return job
        return None

    async def update_job(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        self.updated_jobs.append((job_id, changes["artifacts"]))
        for job in self.jobs:
            if job["id"] == job_id:
                job["artifacts"] = changes["artifacts"]
                return job
        return None


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ArtifactCleanupApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, fake_database: FakeArtifactDatabase, audit_events: list[dict[str, Any]], *, scopes: frozenset[str] = frozenset({"*"})) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=scopes)

        async def record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("database", fake_database)
        self.patch_attr("artifact_root_path", lambda: fake_database.root)
        self.patch_attr("authenticate", authenticate)
        self.patch_attr("record_audit_event", record_audit_event)

    def test_artifact_retention_plan_preserves_voice_samples(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeArtifactDatabase(Path(tmp))
            self.patch_common(fake_database, audit_events)

            plan = asyncio.run(main.admin_artifact_retention_plan(main.ArtifactRetentionRequest(delete_older_than_days=30)))

            self.assertEqual(plan["candidate_count"], 1)
            self.assertEqual(plan["candidates"][0]["path"], "localai/job_old/0.png")
            self.assertEqual(plan["kept"][0]["reason"], "protected_by_voice_profile")
            self.assertTrue((Path(tmp) / "voicebox" / "job_voice" / "0.wav").exists())

    def test_artifact_cleanup_requires_confirmation_updates_job_and_audits(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeArtifactDatabase(Path(tmp))
            self.patch_common(fake_database, audit_events)

            with self.assertRaises(HTTPException) as missing_confirmation:
                asyncio.run(main.admin_artifact_cleanup(main.ArtifactRetentionRequest(delete_older_than_days=30, confirm=False)))
            self.assertEqual(missing_confirmation.exception.status_code, 400)

            result = asyncio.run(main.admin_artifact_cleanup(main.ArtifactRetentionRequest(delete_older_than_days=30, confirm=True)))

            self.assertEqual(result["deleted_count"], 1)
            self.assertFalse((Path(tmp) / "localai" / "job_old" / "0.png").exists())
            self.assertTrue((Path(tmp) / "voicebox" / "job_voice" / "0.wav").exists())
            self.assertEqual(fake_database.updated_jobs[0][0], "job_old")
            self.assertEqual(fake_database.updated_jobs[0][1][0]["retention_status"], "deleted")
            self.assertEqual(audit_events[0]["event_type"], "artifact.cleanup")
            self.assertEqual(audit_events[0]["metadata"]["deleted_paths"], ["localai/job_old/0.png"])

    def test_deleted_artifact_download_returns_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeArtifactDatabase(Path(tmp))
            fake_database.jobs[0]["artifacts"][0]["retention_status"] = "deleted"
            fake_database.jobs[0]["artifacts"][0]["deleted_at"] = datetime.now(tz=UTC).isoformat()
            self.patch_common(fake_database, [])

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.artifact_download("localai/job_old/0.png", request=object(), authorization="Bearer key"))

            self.assertEqual(caught.exception.status_code, 410)

    def test_artifact_retention_requires_storage_read_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_database = FakeArtifactDatabase(Path(tmp))
            self.patch_common(fake_database, [], scopes=frozenset({"jobs:read"}))

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.admin_artifact_retention_plan(main.ArtifactRetentionRequest(delete_older_than_days=30)))

            self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
