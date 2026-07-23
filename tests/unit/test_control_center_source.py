from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_CENTER = ROOT / "web" / "control-center" / "src" / "main.tsx"


class ControlCenterSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CONTROL_CENTER.read_text(encoding="utf-8")

    def test_dashboard_surfaces_recovery_required_job_metric(self) -> None:
        self.assertIn("recovery_required_last_hour: number;", self.source)
        self.assertIn("metrics?.jobs.recovery_required_last_hour", self.source)
        self.assertRegex(self.source, r"<Metric label=\"Jobs last hour\"[^>]+recovery")

    def test_recovery_required_jobs_are_terminal_but_retryable(self) -> None:
        self.assertRegex(self.source, r'TERMINAL_JOB_STATES = new Set\(\[[^\]]*"recovery_required"')
        self.assertRegex(self.source, r'RETRYABLE_JOB_STATES = new Set\(\[[^\]]*"recovery_required"')

    def test_jobs_tab_streams_selected_job_events(self) -> None:
        self.assertIn('/admin/jobs/${encodeURIComponent(jobId)}/events', self.source)
        self.assertIn('Accept: "text/event-stream"', self.source)
        self.assertIn('setJobs((current) => current.map((row) => (row.id === job.id ? job : row)))', self.source)
        self.assertIn('if (!selected || TERMINAL_JOB_STATES.has(selected.state)) return;', self.source)

    def test_external_runtime_config_has_one_configuration_error_field(self) -> None:
        self.assertEqual(self.source.count("configuration_error?: string | null;"), 1)

    def test_system_tab_surfaces_acceptance_reports(self) -> None:
        self.assertIn("type AcceptanceReportSummary", self.source)
        self.assertIn("/admin/acceptance-reports?limit=10", self.source)
        self.assertIn("/admin/acceptance-reports", self.source)
        self.assertIn("<h3>Acceptance Reports</h3>", self.source)


if __name__ == "__main__":
    unittest.main()
