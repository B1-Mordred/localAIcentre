from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import artifact_retention  # noqa: E402


class ArtifactRetentionPolicyTests(unittest.TestCase):
    def make_job(
        self,
        *,
        job_id: str,
        state: str = "completed",
        completed_at: datetime,
        artifact_path: str,
        size: int = 4,
        native_prompt_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "id": job_id,
            "owner_id": "owner_1",
            "state": state,
            "completed_at": completed_at,
            "native_prompt_id": native_prompt_id,
            "artifacts": [
                {
                    "id": f"artifact_{job_id}_0",
                    "url": f"/artifacts/{artifact_path}",
                    "path": artifact_path,
                    "bytes": size,
                    "mime_type": "image/png",
                    "storage": "artifact-server",
                }
            ],
        }

    def test_plan_candidates_only_old_terminal_job_scoped_files(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "localai" / "job_old").mkdir(parents=True)
            (root / "localai" / "job_old" / "0.png").write_bytes(b"old!")
            (root / "localai" / "job_new").mkdir(parents=True)
            (root / "localai" / "job_new" / "0.png").write_bytes(b"new!")
            (root / "comfyui" / "prompt_1").mkdir(parents=True)
            (root / "comfyui" / "prompt_1" / "0.png").write_bytes(b"keep")
            (root / "localai" / "shared" / "0.png").parent.mkdir(parents=True)
            (root / "localai" / "shared" / "0.png").write_bytes(b"bad!")

            jobs = [
                self.make_job(job_id="job_old", completed_at=now - timedelta(days=40), artifact_path="localai/job_old/0.png"),
                self.make_job(job_id="job_new", completed_at=now - timedelta(days=5), artifact_path="localai/job_new/0.png"),
                self.make_job(
                    job_id="job_prompt",
                    completed_at=now - timedelta(days=40),
                    artifact_path="comfyui/prompt_1/0.png",
                    native_prompt_id="prompt_1",
                ),
                self.make_job(job_id="job_bad", completed_at=now - timedelta(days=40), artifact_path="localai/shared/0.png"),
            ]

            plan = artifact_retention.build_artifact_retention_plan(root, jobs, delete_older_than_days=30, now=now)

            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual({item["path"] for item in plan["candidates"]}, {"localai/job_old/0.png", "comfyui/prompt_1/0.png"})
            self.assertEqual(plan["total_reclaimable_bytes"], 8)
            self.assertTrue(any(item["reason"] == "job_newer_than_retention_cutoff" for item in plan["kept"]))
            self.assertTrue(any(item["reason"] == "artifact_path_not_scoped_to_job" for item in plan["invalid_preserved"]))

    def test_protected_and_symlink_artifacts_are_preserved(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "voicebox" / "job_voice").mkdir(parents=True)
            (root / "voicebox" / "job_voice" / "0.wav").write_bytes(b"wave")
            (root / "localai" / "job_link").mkdir(parents=True)
            (root / "localai" / "job_link" / "0.png").symlink_to("/tmp/outside.png")
            jobs = [
                self.make_job(job_id="job_voice", completed_at=now - timedelta(days=40), artifact_path="voicebox/job_voice/0.wav"),
                self.make_job(job_id="job_link", completed_at=now - timedelta(days=40), artifact_path="localai/job_link/0.png"),
            ]

            plan = artifact_retention.build_artifact_retention_plan(
                root,
                jobs,
                delete_older_than_days=30,
                protected_urls={"/artifacts/voicebox/job_voice/0.wav"},
                now=now,
            )

            self.assertEqual(plan["candidate_count"], 0)
            self.assertEqual(plan["kept"][0]["reason"], "protected_by_voice_profile")
            self.assertEqual(plan["invalid_preserved"][0]["reason"], "artifact path contains a symlink")

    def test_apply_requires_confirmation_deletes_files_and_marks_job_artifacts(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "audio-cpu" / "job_audio" / "0.wav"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"wave")
            jobs = [self.make_job(job_id="job_audio", completed_at=now - timedelta(days=40), artifact_path="audio-cpu/job_audio/0.wav")]

            with self.assertRaises(artifact_retention.ArtifactRetentionError):
                artifact_retention.apply_artifact_retention_plan(root, jobs, delete_older_than_days=30, now=now)

            report = artifact_retention.apply_artifact_retention_plan(root, jobs, delete_older_than_days=30, confirmed=True, now=now)

            self.assertFalse(target.exists())
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["deleted_count"], 1)
            self.assertEqual(report["job_update_count"], 1)
            updated_artifact = report["job_updates"][0]["artifacts"][0]
            self.assertEqual(updated_artifact["retention_status"], "deleted")
            self.assertEqual(updated_artifact["retention_reason"], "older_than_30_days")

    def test_namespace_filter_limits_candidates(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ("localai/job_image/0.png", "audio-cpu/job_audio/0.wav"):
                target = root / path
                target.parent.mkdir(parents=True)
                target.write_bytes(b"data")
            jobs = [
                self.make_job(job_id="job_image", completed_at=now - timedelta(days=40), artifact_path="localai/job_image/0.png"),
                self.make_job(job_id="job_audio", completed_at=now - timedelta(days=40), artifact_path="audio-cpu/job_audio/0.wav"),
            ]

            plan = artifact_retention.build_artifact_retention_plan(root, jobs, delete_older_than_days=30, namespaces=["audio-cpu"], now=now)

            self.assertEqual([item["path"] for item in plan["candidates"]], ["audio-cpu/job_audio/0.wav"])
            self.assertEqual(plan["kept"][0]["reason"], "outside_selected_namespaces")


if __name__ == "__main__":
    unittest.main()
