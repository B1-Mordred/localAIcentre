from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import voicebox_samples  # noqa: E402


class VoiceboxSampleRetentionTests(unittest.TestCase):
    def touch_sample(self, root: Path, relative: str, content: bytes, modified_at: datetime) -> Path:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        timestamp = modified_at.timestamp()
        os.utime(target, (timestamp, timestamp))
        return target

    def test_plan_candidates_only_old_unreferenced_samples(self) -> None:
        now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_unreferenced = self.touch_sample(
                root,
                "voicebox/references/operator_1/sample_old/narrator.wav",
                b"old",
                now - timedelta(days=45),
            )
            protected = self.touch_sample(
                root,
                "voicebox/references/operator_1/sample_protected/narrator.wav",
                b"keep",
                now - timedelta(days=45),
            )
            self.touch_sample(
                root,
                "voicebox/references/operator_1/sample_new/narrator.wav",
                b"new",
                now - timedelta(days=5),
            )
            (root / "voicebox" / "references" / "operator_1" / "sample_link.wav").symlink_to(old_unreferenced)

            plan = voicebox_samples.build_voicebox_sample_retention_plan(
                root,
                delete_older_than_days=30,
                protected_urls={f"/artifacts/{protected.relative_to(root).as_posix()}"},
                now=now,
            )

            self.assertEqual(plan["candidate_count"], 1)
            self.assertEqual(plan["candidates"][0]["path"], "voicebox/references/operator_1/sample_old/narrator.wav")
            self.assertEqual(plan["total_reclaimable_bytes"], 3)
            self.assertTrue(any(item["reason"] == "protected_by_voice_profile" for item in plan["kept"]))
            self.assertTrue(any(item["reason"] == "sample_newer_than_retention_cutoff" for item in plan["kept"]))
            self.assertTrue(any(item["reason"] == "sample file is a symlink" for item in plan["invalid_preserved"]))

    def test_apply_requires_confirmation_and_deletes_only_candidates(self) -> None:
        now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = self.touch_sample(root, "voicebox/references/operator_1/sample_old/narrator.wav", b"old", now - timedelta(days=45))
            protected = self.touch_sample(root, "voicebox/references/operator_1/sample_keep/narrator.wav", b"keep", now - timedelta(days=45))
            protected_url = f"/artifacts/{protected.relative_to(root).as_posix()}"

            with self.assertRaises(voicebox_samples.VoiceboxSampleRetentionError):
                voicebox_samples.apply_voicebox_sample_retention_plan(
                    root,
                    delete_older_than_days=30,
                    protected_urls={protected_url},
                    now=now,
                    confirmed=False,
                )

            report = voicebox_samples.apply_voicebox_sample_retention_plan(
                root,
                delete_older_than_days=30,
                protected_urls={protected_url},
                now=now,
                confirmed=True,
            )

            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["deleted_count"], 1)
            self.assertFalse(candidate.exists())
            self.assertTrue(protected.exists())


if __name__ == "__main__":
    unittest.main()
