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
        self.assertIn("operator_evidence_ready?: boolean;", self.source)
        self.assertIn("cutover_preservation_ready?: boolean;", self.source)
        self.assertIn("gpu_evidence_ready?: boolean;", self.source)
        self.assertIn("native_comfyui_evidence_ready?: boolean;", self.source)
        self.assertIn("remote_nodes_evidence_ready?: boolean;", self.source)
        self.assertIn("modelhub_evidence_ready?: boolean;", self.source)
        self.assertIn("voicebox_evidence_ready?: boolean;", self.source)
        self.assertIn("live_evidence_ready?: boolean;", self.source)
        self.assertIn("ACCEPTANCE_EVIDENCE_ITEMS", self.source)
        self.assertIn("rtx3060_acceptance", self.source)
        self.assertIn("operator_evidence: acceptanceEvidence", self.source)
        self.assertIn("rollback proof missing", self.source)
        self.assertIn("GPU proof missing", self.source)
        self.assertIn("ComfyUI proof missing", self.source)
        self.assertIn("remote-node proof missing", self.source)
        self.assertIn("Model Hub proof missing", self.source)
        self.assertIn("Voicebox proof missing", self.source)
        self.assertIn("/admin/acceptance-reports?limit=10", self.source)
        self.assertIn("/admin/acceptance-reports", self.source)
        self.assertIn("<h3>Acceptance Reports</h3>", self.source)

    def test_models_tab_supports_remote_manifest_url(self) -> None:
        self.assertIn('const [manifestUrl, setManifestUrl] = useState("");', self.source)
        self.assertIn("return url ? { manifest_url: url } : null;", self.source)
        self.assertIn("Manifest URL", self.source)
        self.assertIn("Plan remote manifest install", self.source)
        self.assertIn("Queue remote manifest download", self.source)
        self.assertIn("accept_license: Boolean(downloadPlan?.requires_license_acceptance)", self.source)
        self.assertIn("acceptance required", self.source)


if __name__ == "__main__":
    unittest.main()
