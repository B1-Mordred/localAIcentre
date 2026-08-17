from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import benchmarking  # noqa: E402


class BenchmarkingTests(unittest.TestCase):
    def test_content_hash_is_stable_across_key_order(self) -> None:
        self.assertEqual(benchmarking.content_sha256({"a": 1, "b": [2]}), benchmarking.content_sha256({"b": [2], "a": 1}))

    def test_application_profile_ranking_is_provisional(self) -> None:
        profile = {"id": "quality", "version": "1", "weights": {"quality": 3, "latency": 1}}
        ranking = benchmarking.build_ranking({
            "model-a": [{"metrics": {"quality": 90, "latency": 60}}, {"metrics": {"quality": 80, "latency": 80}}],
            "model-b": [{"metrics": {"quality": 85, "latency": 74}}],
        }, profile)
        self.assertEqual(ranking[0]["rank_status"], "provisional")
        self.assertEqual(ranking[0]["coverage"], 1)
        self.assertIsNotNone(next(row for row in ranking if row["candidate"] == "model-a")["confidence_interval"])

    def test_openrouter_selection_intersects_account_and_skips_codex(self) -> None:
        account = [{"id": name} for name in ["top/a", "openai/codex", "top/b", "top/c", "not-owned"]]
        public = [
            {"id": "top/a", "supported_parameters": ["structured_outputs"]},
            {"id": "openai/codex", "supported_parameters": ["response_format"]},
            {"id": "top/b", "supported_parameters": ["response_format"]},
            {"id": "not-structured", "supported_parameters": []},
            {"id": "top/c", "supported_parameters": ["response_format"]},
        ]
        self.assertEqual(benchmarking.select_openrouter_judges(account, public), ["top/a", "top/b", "top/c"])

    def test_report_bundle_contains_all_formats_and_valid_checksums(self) -> None:
        report = {"title": "Test", "ranking": [{"rank": 1, "candidate": "m", "score": 88, "coverage": 1, "rank_status": "final"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); files = benchmarking.write_report_bundle(root, "report-1", report)
            self.assertEqual(set(files), {"report.json", "report.md", "ranking.csv", "report.html", "report.pdf", "SHA256SUMS"})
            self.assertTrue((root / "report-1" / "report.pdf").read_bytes().startswith(b"%PDF-1.4"))
            for line in (root / "report-1" / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256((root / "report-1" / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(json.loads((root / "report-1" / "report.json").read_text())["title"], "Test")

    def test_raw_output_retention_is_fourteen_days(self) -> None:
        created = datetime(2026, 8, 10, tzinfo=UTC)
        self.assertEqual(benchmarking.raw_expires_at(created), created + timedelta(days=14))
        campaign = {"raw_expires_at": created, "summary": {"raw_path": "/tmp/campaign-output"}}
        self.assertEqual(benchmarking.expired_raw_paths([campaign], created + timedelta(seconds=1))[0].name, "campaign-output")


if __name__ == "__main__":
    unittest.main()
