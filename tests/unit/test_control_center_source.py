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

    def test_jobs_tab_summarizes_redacted_reproducibility_metadata(self) -> None:
        self.assertIn("function JobReproducibilitySummary", self.source)
        self.assertIn("function jobReproducibilityEntries", self.source)
        self.assertIn("<JobReproducibilitySummary job={selected} />", self.source)
        self.assertIn("<h3>Redacted Request</h3>", self.source)
        self.assertIn("selected.redacted_request ?? {}", self.source)
        self.assertIn("input?.workflow_id", self.source)
        self.assertIn("input?.workflow_version", self.source)
        self.assertIn("Object.entries(parameters ?? {}).slice(0, 10)", self.source)
        self.assertNotIn("request_params", self.source)

    def test_jobs_tab_downloads_artifacts_with_authenticated_fetches(self) -> None:
        self.assertIn("type JobArtifact", self.source)
        self.assertIn("async function downloadJobArtifact", self.source)
        self.assertIn('apiFetch(artifact.url, { method: "GET" })', self.source)
        self.assertIn("URL.createObjectURL(blob)", self.source)
        self.assertIn("URL.revokeObjectURL(url)", self.source)
        self.assertIn("function artifactFilename", self.source)
        self.assertIn("downloadSelectedArtifact(artifact, index)", self.source)
        self.assertIn('title={`Download artifact ${index + 1}`}', self.source)

    def test_external_access_surfaces_client_snippets(self) -> None:
        self.assertIn("function accessSnippets", self.source)
        self.assertIn("Client Snippets", self.source)
        self.assertIn("snippet-grid", self.source)
        self.assertIn("copySnippet(snippet)", self.source)
        self.assertIn("navigator.clipboard.writeText(snippet.code)", self.source)
        for label in ("curl", "Open WebUI", "External ComfyUI", "Model Hub Sync", "Voicebox", "Python", "JavaScript"):
            self.assertIn(f'label: "{label}"', self.source)
        self.assertIn("python -m pip install ./integrations/b1-model-client", self.source)
        self.assertIn("B1_MODELHUB_URL=https://models.ai.b1.germering", self.source)
        self.assertIn("b1-model-client plan --cache ~/.cache/b1-ai-hub/models chat-default", self.source)
        self.assertIn("b1-model-client sync --cache ~/.cache/b1-ai-hub/models chat-default", self.source)
        self.assertIn("https://voice.ai.b1.germering", self.source)
        self.assertIn("python -m pip install ./integrations/comfyui-b1-remote-nodes", self.source)
        self.assertIn("<B1_API_KEY>", self.source)
        self.assertIn("<B1_MODELHUB_KEY>", self.source)

    def test_external_access_handles_admin_only_credential_routes(self) -> None:
        self.assertIn("apiClientsAdminOnly", self.source)
        self.assertIn("modelHubClientsAdminOnly", self.source)
        self.assertIn("adminOnlyClientPayload", self.source)
        self.assertIn("if (response.status === 403) return adminOnlyClientPayload;", self.source)
        self.assertIn("Credential management requires an administrator role.", self.source)
        self.assertIn("Administrator role required to view API clients", self.source)
        self.assertIn("Administrator role required to view Model Hub clients", self.source)
        self.assertIn("Administrator role required to view encrypted values", self.source)
        self.assertIn('disabled={busy || apiClientsAdminOnly || !apiDisplayName.trim()}', self.source)
        self.assertIn('disabled={busy || modelHubClientsAdminOnly || !hubDisplayName.trim()}', self.source)
        self.assertIn('disabled={busy || secretsAdminOnly || !secretName.trim() || !secretDisplayName.trim() || !secretValue}', self.source)

    def test_external_runtime_config_has_one_configuration_error_field(self) -> None:
        self.assertEqual(self.source.count("configuration_error?: string | null;"), 1)

    def test_runtimes_tab_surfaces_structured_capabilities(self) -> None:
        self.assertIn("const capabilityListLabel", self.source)
        self.assertIn("const runtimeCapabilityFlags", self.source)
        self.assertIn("reportedCapabilities ? (", self.source)
        self.assertIn("<tr><td>Modalities</td><td>{capabilityListLabel(reportedCapabilities.modalities)}</td></tr>", self.source)
        self.assertIn("<tr><td>Operations</td><td>{capabilityListLabel(reportedCapabilities.operations)}</td></tr>", self.source)
        self.assertIn("<tr><td>Capability flags</td><td>{runtimeCapabilityFlags(reportedCapabilities)}</td></tr>", self.source)
        self.assertIn('capabilities.openai_compatible ? "OpenAI API" : ""', self.source)
        self.assertIn('capabilities.native_api ? "native API" : ""', self.source)
        self.assertIn('capabilities.external ? "external data" : "local LAN"', self.source)

    def test_system_tab_surfaces_acceptance_reports(self) -> None:
        self.assertIn("type AcceptanceReportSummary", self.source)
        self.assertIn("operator_evidence_ready?: boolean;", self.source)
        self.assertIn("cutover_preservation_ready?: boolean;", self.source)
        self.assertIn("smoke_evidence_ready?: boolean;", self.source)
        self.assertIn("gpu_evidence_ready?: boolean;", self.source)
        self.assertIn("localai_evidence_ready?: boolean;", self.source)
        self.assertIn("installed_workflows_evidence_ready?: boolean;", self.source)
        self.assertIn("native_comfyui_evidence_ready?: boolean;", self.source)
        self.assertIn("remote_nodes_evidence_ready?: boolean;", self.source)
        self.assertIn("modelhub_evidence_ready?: boolean;", self.source)
        self.assertIn("voicebox_evidence_ready?: boolean;", self.source)
        self.assertIn("security_evidence_ready?: boolean;", self.source)
        self.assertIn("restart_reconciliation_evidence_ready?: boolean;", self.source)
        self.assertIn("backup_migration_rollback_evidence_ready?: boolean;", self.source)
        self.assertIn("live_evidence_ready?: boolean;", self.source)
        self.assertIn("ACCEPTANCE_EVIDENCE_ITEMS", self.source)
        self.assertIn("rtx3060_acceptance", self.source)
        self.assertIn("restart_reconciliation", self.source)
        self.assertIn("smoke proof missing", self.source)
        self.assertIn("LocalAI proof missing", self.source)
        self.assertIn("operator_evidence: acceptanceEvidence", self.source)
        self.assertIn("rollback proof missing", self.source)
        self.assertIn("GPU proof missing", self.source)
        self.assertIn("workflow proof missing", self.source)
        self.assertIn("ComfyUI proof missing", self.source)
        self.assertIn("remote-node proof missing", self.source)
        self.assertIn("Model Hub proof missing", self.source)
        self.assertIn("Voicebox proof missing", self.source)
        self.assertIn("security proof missing", self.source)
        self.assertIn("restart proof missing", self.source)
        self.assertIn("backup/rollback proof missing", self.source)
        self.assertIn("rollback preservation missing", self.source)
        self.assertIn("/admin/acceptance-reports?limit=10", self.source)
        self.assertIn("/admin/acceptance-reports", self.source)
        self.assertIn("type AcceptanceReportDetail", self.source)
        self.assertIn("type AcceptanceReportFileName", self.source)
        self.assertIn("inspectAcceptanceReport", self.source)
        self.assertIn("downloadAcceptanceReportFile", self.source)
        self.assertIn("downloadAcceptanceReport", self.source)
        self.assertIn("/admin/acceptance-reports/${encodeURIComponent(reportId)}", self.source)
        self.assertIn("/admin/acceptance-reports/${encodeURIComponent(reportId)}/files/${encodeURIComponent(filename)}", self.source)
        self.assertIn("Download Markdown", self.source)
        self.assertIn("Download checksums", self.source)
        self.assertIn("<h3>Acceptance Report Detail</h3>", self.source)
        self.assertIn("acceptanceOperatorEvidenceRows", self.source)
        self.assertIn("acceptanceLiveEvidenceRows", self.source)
        self.assertIn("acceptancePreservedResourceRows", self.source)
        self.assertIn("Preserved Rollback Resources", self.source)
        self.assertIn("Raw JSON", self.source)
        self.assertIn("<h3>Acceptance Reports</h3>", self.source)

    def test_storage_tab_inspects_backup_manifests(self) -> None:
        self.assertIn("type BackupManifest", self.source)
        self.assertIn("type BackupManifestFile", self.source)
        self.assertIn("const [selectedBackupManifest, setSelectedBackupManifest]", self.source)
        self.assertIn("inspectBackupManifest", self.source)
        self.assertIn("/admin/backups/${encodeURIComponent(backup.name)}/manifest", self.source)
        self.assertIn("Inspect manifest", self.source)
        self.assertIn("<h3>Backup Manifest Detail</h3>", self.source)
        self.assertIn("sensitiveManifestFiles", self.source)
        self.assertIn("largestManifestFiles", self.source)
        self.assertIn("manifestPostgresDumps", self.source)
        self.assertIn("Raw Manifest JSON", self.source)

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
