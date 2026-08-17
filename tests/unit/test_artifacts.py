from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.artifacts import (  # noqa: E402
    ArtifactAccessError,
    artifact_url_for_path,
    job_has_artifact_url,
    subject_can_read_job_artifact,
)


class ArtifactPolicyTests(unittest.TestCase):
    def test_artifact_path_normalization_rejects_escape(self) -> None:
        self.assertEqual(artifact_url_for_path("temporary/job.json"), "/artifacts/temporary/job.json")
        with self.assertRaises(ArtifactAccessError):
            artifact_url_for_path("../secrets/admin_bootstrap_key")
        with self.assertRaises(ArtifactAccessError):
            artifact_url_for_path("/absolute/path")
        with self.assertRaises(ArtifactAccessError):
            artifact_url_for_path("temporary/./job.json")

    def test_artifact_path_rejects_encoded_escape_segments(self) -> None:
        for artifact_path in [
            "voicebox/%2e%2e/private.wav",
            "voicebox/safe%2Fprivate.wav",
            "voicebox/safe%5Cprivate.wav",
            "voicebox/safe%3Ftoken.wav",
            "voicebox/safe%23fragment.wav",
            "voicebox/%00sample.wav",
            "voicebox/%/sample.wav",
            "voicebox/%2/sample.wav",
            "voicebox/%zz/sample.wav",
            "voicebox/%ffsample.wav",
        ]:
            with self.subTest(artifact_path=artifact_path):
                with self.assertRaises(ArtifactAccessError):
                    artifact_url_for_path(artifact_path)

    def test_job_artifact_membership_uses_recorded_urls(self) -> None:
        job = {"artifacts": [{"url": "/artifacts/temporary/job.json"}, {"url": "/artifacts/images/result.png"}]}
        self.assertTrue(job_has_artifact_url(job, "/artifacts/images/result.png"))
        self.assertFalse(job_has_artifact_url(job, "/artifacts/images/missing.png"))

    def test_artifact_owner_or_admin_can_read(self) -> None:
        job = {"owner_id": "client_1"}
        self.assertTrue(subject_can_read_job_artifact("client_1", frozenset({"jobs:read"}), job))
        self.assertFalse(subject_can_read_job_artifact("client_2", frozenset({"jobs:read"}), job))
        self.assertTrue(subject_can_read_job_artifact("admin", frozenset({"*"}), job))
        self.assertTrue(subject_can_read_job_artifact("admin_session", frozenset({"jobs:read"}), job, "admin"))
        self.assertTrue(subject_can_read_job_artifact("operator_session", frozenset({"jobs:read"}), job, "operator"))
        self.assertFalse(subject_can_read_job_artifact("creator_session", frozenset({"jobs:read"}), job, "creator"))


if __name__ == "__main__":
    unittest.main()
