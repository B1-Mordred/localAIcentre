from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "deploy" / "scripts" / "repository_quality_evidence.py"
spec = importlib.util.spec_from_file_location("repository_quality_evidence", SCRIPT_PATH)
repository_quality_evidence = importlib.util.module_from_spec(spec)
sys.modules["repository_quality_evidence"] = repository_quality_evidence
assert spec.loader is not None
spec.loader.exec_module(repository_quality_evidence)


class RepositoryQualityEvidenceTests(unittest.TestCase):
    def test_build_evidence_records_clean_source_and_required_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.object(
                repository_quality_evidence,
                "source_state",
                return_value={
                    "source_commit": "a" * 40,
                    "source_branch": "agent/test",
                    "source_dirty": False,
                    "dirty_path_count": 0,
                },
            ):
                payload = repository_quality_evidence.build_evidence(
                    repo_root=repo,
                    quality_command="make quality-container",
                    secret_scan_command="make secret-scan",
                    generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                    quality_passed=True,
                    secret_scan_passed=True,
                )

        self.assertEqual(payload["format"], repository_quality_evidence.EVIDENCE_FORMAT)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertFalse(payload["source_dirty"])
        self.assertEqual(payload["dirty_path_count"], 0)
        self.assertEqual(set(payload["checks"]), {"quality_container", "secret_scan"})
        self.assertIn("backend_unit_tests", payload["checks"]["quality_container"]["coverage"])
        self.assertIn("source_secret_scan", payload["checks"]["secret_scan"]["coverage"])
        quality_summary = payload["checks"]["quality_container"]["result_summary"]
        self.assertEqual(quality_summary["outcome"], "passed")
        self.assertEqual(quality_summary["exit_code"], 0)
        self.assertEqual(quality_summary["required_by_target"], "repository-quality-evidence")
        quality_result_labels = {item["label"] for item in quality_summary["test_results"]}
        self.assertIn("backend_unit_tests", quality_result_labels)
        self.assertIn("frontend_media_studio_audit", quality_result_labels)
        self.assertEqual(payload["samples"][0]["verified_check_count"], 2)
        self.assertEqual(payload["samples"][0]["label"], "repository-quality")

    def test_build_evidence_marks_dirty_source_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.object(
                repository_quality_evidence,
                "source_state",
                return_value={
                    "source_commit": "b" * 40,
                    "source_branch": "agent/test",
                    "source_dirty": True,
                    "dirty_path_count": 1,
                },
            ):
                incomplete = repository_quality_evidence.build_evidence(
                    repo_root=repo,
                    quality_command="make quality-container",
                    secret_scan_command="make secret-scan",
                    generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                    quality_passed=True,
                    secret_scan_passed=True,
                )

        self.assertEqual(incomplete["status"], "incomplete")
        self.assertTrue(incomplete["source_dirty"])
        self.assertEqual(incomplete["dirty_path_count"], 1)

    def test_build_evidence_marks_unverified_gates_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.object(
                repository_quality_evidence,
                "source_state",
                return_value={
                    "source_commit": "c" * 40,
                    "source_branch": "agent/test",
                    "source_dirty": False,
                    "dirty_path_count": 0,
                },
            ):
                payload = repository_quality_evidence.build_evidence(
                    repo_root=repo,
                    quality_command="make quality-container",
                    secret_scan_command="make secret-scan",
                    generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                )

        self.assertEqual(payload["status"], "incomplete")
        self.assertEqual(payload["checks"]["quality_container"]["status"], "unverified")
        self.assertEqual(payload["checks"]["quality_container"]["result_summary"]["outcome"], "unverified")
        self.assertIsNone(payload["checks"]["quality_container"]["result_summary"]["exit_code"])
        self.assertEqual(payload["samples"][0]["verified_check_count"], 0)

    def test_write_private_json_refuses_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            self.symlink_or_skip(target, link)

            with self.assertRaises(repository_quality_evidence.RepositoryQualityEvidenceError):
                repository_quality_evidence.write_private_json(
                    link,
                    {"format": repository_quality_evidence.EVIDENCE_FORMAT},
                    force=True,
                )

    def test_write_private_json_writes_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "repository-quality.json"
            repository_quality_evidence.write_private_json(
                output,
                {"format": repository_quality_evidence.EVIDENCE_FORMAT, "status": "ok"},
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["format"], repository_quality_evidence.EVIDENCE_FORMAT)

    def symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlink creation is unavailable")
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
