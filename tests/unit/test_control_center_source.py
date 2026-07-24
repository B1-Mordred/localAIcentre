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

    def test_workflows_tab_manages_comfyui_node_pins(self) -> None:
        self.assertIn("type ComfyUiNodePin", self.source)
        self.assertIn("const [nodePins, setNodePins]", self.source)
        self.assertIn("const [selectedNodePin, setSelectedNodePin]", self.source)
        self.assertIn("/admin/comfyui/node-pins", self.source)
        self.assertIn("loadNodePins", self.source)
        self.assertIn("saveNodePin", self.source)
        self.assertIn("ComfyUI Node Pins", self.source)
        self.assertIn("Approve ComfyUI Node Pin", self.source)
        self.assertIn("Edit ComfyUI Node Pin", self.source)
        self.assertIn("allowed_route_prefixes", self.source)
        self.assertIn("dependency_lock_sha256", self.source)
        self.assertIn("Save Pin", self.source)

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
        self.assertIn("type RollbackRehearsalStatus", self.source)
        self.assertIn("const [rollbackRehearsal, setRollbackRehearsal]", self.source)
        self.assertIn("loadRollbackRehearsal", self.source)
        self.assertIn("createRollbackRehearsal", self.source)
        self.assertIn("/admin/migration/rollback-rehearsal", self.source)
        self.assertIn("type OpenWebUiMigrationPlanStatus", self.source)
        self.assertIn("const [openWebUiPlan, setOpenWebUiPlan]", self.source)
        self.assertIn("loadOpenWebUiPlan", self.source)
        self.assertIn("createOpenWebUiPlan", self.source)
        self.assertIn("/admin/migration/open-webui-plan", self.source)
        self.assertIn("<h3>Open WebUI Migration Plan</h3>", self.source)
        self.assertIn("Generate Open WebUI migration plan", self.source)
        self.assertIn("openWebUiPlan?.ready_to_generate", self.source)
        self.assertIn("<h3>Rollback Rehearsal</h3>", self.source)
        self.assertIn("rollback_commands_tested: rollbackCommandsTested", self.source)
        self.assertIn("old_resources_preserved: rollbackResourcesPreserved", self.source)
        self.assertIn("Generate rollback rehearsal report", self.source)
        self.assertIn("type BackupMigrationRollbackEvidenceStatus", self.source)
        self.assertIn("const [backupRollbackEvidence, setBackupRollbackEvidence]", self.source)
        self.assertIn("loadBackupRollbackEvidence", self.source)
        self.assertIn("createBackupRollbackEvidence", self.source)
        self.assertIn("/admin/migration/backup-migration-rollback-evidence", self.source)
        self.assertIn("<h3>Backup/Migration/Rollback Evidence</h3>", self.source)
        self.assertIn("confirm_reviewed: backupRollbackReviewed", self.source)
        self.assertIn("Generate backup/migration/rollback evidence", self.source)
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
        self.assertIn("type BackupPostgresImportPlan", self.source)
        self.assertIn("const [postgresImportPlan, setPostgresImportPlan]", self.source)
        self.assertIn("runPostgresImport", self.source)
        self.assertIn("/admin/backups/${encodeURIComponent(backup.name)}/postgres-import", self.source)
        self.assertIn("confirm_backup_name: postgresImportConfirm.trim()", self.source)
        self.assertIn("<h3>PostgreSQL Import</h3>", self.source)
        self.assertIn("Raw Import JSON", self.source)
        self.assertIn("postgresImportConfirm !== postgresImportBackup", self.source)

    def test_models_tab_supports_remote_manifest_url(self) -> None:
        self.assertIn('const [manifestUrl, setManifestUrl] = useState("");', self.source)
        self.assertIn("return url ? { manifest_url: url } : null;", self.source)
        self.assertIn("Manifest URL", self.source)
        self.assertIn("Plan remote manifest install", self.source)
        self.assertIn("Queue remote manifest download", self.source)
        self.assertIn("accept_license: Boolean(downloadPlan?.requires_license_acceptance)", self.source)
        self.assertIn("acceptance required", self.source)

    def test_models_tab_runs_installed_model_smoke_tests(self) -> None:
        self.assertIn("type ModelSmokeTestResult", self.source)
        self.assertIn("const [smokeResult, setSmokeResult]", self.source)
        self.assertIn("const smokeTestModel = (record: ModelRecord)", self.source)
        self.assertIn("/admin/models/${modelVersionPath(record)}/smoke-test", self.source)
        self.assertIn("body: JSON.stringify({ persist: true })", self.source)
        self.assertIn("setSmokeResult(payload)", self.source)
        self.assertIn("Run smoke test for ${record.display_name}", self.source)
        self.assertIn("measurements persisted", self.source)


if __name__ == "__main__":
    unittest.main()
