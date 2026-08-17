from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import admission  # noqa: E402


class AdmissionPolicyTests(unittest.TestCase):
    def test_queue_policy_rejects_owner_queue_limit(self) -> None:
        policy = admission.AdmissionPolicy(
            max_queued_jobs_per_owner=2,
            max_active_jobs_per_owner=0,
            max_jobs_per_hour_per_owner=0,
            max_queued_jobs_global=0,
            artifact_storage_max_bytes=0,
            artifact_storage_reserve_bytes=0,
        )
        snapshot = admission.QueueAdmissionSnapshot(
            owner_id="client_1",
            owner_queued_jobs=2,
            owner_active_jobs=0,
            owner_jobs_last_hour=0,
            global_queued_jobs=0,
        )

        with self.assertRaisesRegex(admission.AdmissionDeniedError, "owner queued job limit"):
            admission.enforce_queue_admission(policy, snapshot)

    def test_queue_policy_allows_disabled_limits(self) -> None:
        policy = admission.AdmissionPolicy(
            max_queued_jobs_per_owner=0,
            max_active_jobs_per_owner=0,
            max_jobs_per_hour_per_owner=0,
            max_queued_jobs_global=0,
            artifact_storage_max_bytes=0,
            artifact_storage_reserve_bytes=0,
        )
        snapshot = admission.QueueAdmissionSnapshot(
            owner_id="client_1",
            owner_queued_jobs=1000,
            owner_active_jobs=1000,
            owner_jobs_last_hour=1000,
            global_queued_jobs=1000,
        )

        admission.enforce_queue_admission(policy, snapshot)

    def test_storage_policy_rejects_configured_artifact_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "job_1").mkdir()
            (root / "job_1" / "output.bin").write_bytes(b"12345")
            policy = admission.AdmissionPolicy(
                max_queued_jobs_per_owner=0,
                max_active_jobs_per_owner=0,
                max_jobs_per_hour_per_owner=0,
                max_queued_jobs_global=0,
                artifact_storage_max_bytes=8,
                artifact_storage_reserve_bytes=0,
            )

            snapshot = admission.artifact_storage_snapshot(root, policy)

            with self.assertRaises(admission.AdmissionDeniedError) as caught:
                admission.enforce_artifact_storage_admission(policy, snapshot, incoming_bytes=4)

            self.assertEqual(caught.exception.code, "artifact_storage_limit")
            self.assertEqual(caught.exception.status_code, 507)

    def test_storage_report_uses_disk_reserve_without_tree_scan_when_cap_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output.bin").write_bytes(b"12345")
            policy = admission.AdmissionPolicy(
                max_queued_jobs_per_owner=0,
                max_active_jobs_per_owner=0,
                max_jobs_per_hour_per_owner=0,
                max_queued_jobs_global=0,
                artifact_storage_max_bytes=0,
                artifact_storage_reserve_bytes=0,
            )

            snapshot = admission.artifact_storage_snapshot(root, policy)

            self.assertIsNone(snapshot.artifact_bytes)
            self.assertGreater(snapshot.disk_free_bytes or 0, 0)


if __name__ == "__main__":
    unittest.main()
