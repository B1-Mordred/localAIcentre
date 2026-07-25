from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_STUDIO = ROOT / "web" / "media-studio" / "src" / "main.tsx"


class MediaStudioSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MEDIA_STUDIO.read_text(encoding="utf-8")

    def test_recovery_required_jobs_are_terminal_for_progress_and_cancel_controls(self) -> None:
        self.assertRegex(self.source, r'TERMINAL_STATES = new Set\(\[[^\]]*"recovery_required"')
        self.assertIn('disabled={!job || TERMINAL_STATES.has(job.state)}', self.source)
        self.assertIn('if (!currentJob || TERMINAL_STATES.has(currentJob.state)) return;', self.source)

    def test_history_rows_open_artifact_and_reproducibility_panel(self) -> None:
        self.assertIn("function HistoryDetail", self.source)
        self.assertIn("selectedHistoryJob", self.source)
        self.assertIn("setHistoryArtifacts(payload.artifacts ?? [])", self.source)
        self.assertIn("downloadHistoryArtifact", self.source)
        self.assertIn("reproducibility metadata", self.source)
        self.assertIn("redacted_request?: Record<string, JsonValue>;", self.source)
        self.assertIn("function ReproducibilityMetadata", self.source)
        self.assertIn("reproducibilityEntries(job)", self.source)
        self.assertIn("<ReproducibilityMetadata job={job} />", self.source)
        self.assertIn("redacted_request", self.source)
        self.assertNotIn("request_params", self.source)
        self.assertIn("resolved_model_version", self.source)
        self.assertIn("jobRoute(job, \"artifacts\", \"/artifacts\")", self.source)
        self.assertIn("<History jobs={jobs} onRefresh={loadJobs} onSelect={loadHistoryArtifacts} />", self.source)

    def test_running_jobs_surface_bounded_sse_timeline(self) -> None:
        self.assertIn("type JobTimelineEntry", self.source)
        self.assertIn("function jobTimelineEntry(job: MediaJob, label: string): JobTimelineEntry", self.source)
        self.assertIn("function appendTimelineEntry", self.source)
        self.assertIn("return [...entries, entry].slice(-12);", self.source)
        self.assertIn("function JobEventTimeline", self.source)
        self.assertIn('aria-label="Job event timeline"', self.source)
        self.assertIn("eventTimeline: JobTimelineEntry[];", self.source)
        self.assertIn("const [eventTimeline, setEventTimeline] = useState<JobTimelineEntry[]>([]);", self.source)
        self.assertIn('setEventTimeline([jobTimelineEntry(job, "submitted")]);', self.source)
        self.assertIn('appendTimelineEntry(entries, jobTimelineEntry(job, "event"))', self.source)
        self.assertIn('appendTimelineEntry(entries, jobTimelineEntry(job, "refresh"))', self.source)
        self.assertIn('appendTimelineEntry(entries, jobTimelineEntry(job, "cancel"))', self.source)
        self.assertIn("<JobEventTimeline entries={eventTimeline} />", self.source)

    def test_media_job_links_are_used_for_streams_artifacts_and_cancellation(self) -> None:
        self.assertIn("type MediaJobLinks", self.source)
        self.assertIn("links?: MediaJobLinks;", self.source)
        self.assertIn("function trustedMediaJobLink", self.source)
        self.assertIn('value.startsWith("/v1/media/jobs/")', self.source)
        self.assertIn('jobRoute(job, "events", "/events")', self.source)
        self.assertIn('jobRoute(currentJob, "self")', self.source)
        self.assertIn('jobRoute(currentJob, "cancel")', self.source)
        self.assertIn('jobRoute(job, "artifacts", "/artifacts")', self.source)

    def test_artifact_outputs_are_previewed_with_authenticated_fetches(self) -> None:
        self.assertIn("function artifactPreviewSource", self.source)
        self.assertIn("new B1ApiClient", self.source)
        self.assertIn("getBearerToken: () => API_TOKEN", self.source)
        self.assertIn("getCsrfToken: () => window.sessionStorage.getItem(CSRF_STORAGE_KEY)", self.source)
        self.assertIn("apiClient.fetch(artifact.url as B1ApiRequestPath, { method: \"GET\" })", self.source)
        self.assertIn("URL.createObjectURL(blob)", self.source)
        self.assertIn("URL.revokeObjectURL(objectUrl)", self.source)
        self.assertIn("function firstPreviewArtifact", self.source)
        self.assertIn("mime.startsWith(\"image/\") || mime.startsWith(\"audio/\") || mime.startsWith(\"video/\")", self.source)
        self.assertIn("<ArtifactPreview artifact={outputPreview} fallbackSource={preview} />", self.source)
        self.assertIn("<ArtifactPreview artifact={outputPreview} />", self.source)

    def test_jobs_show_actual_local_external_and_comfy_backing(self) -> None:
        self.assertIn("type WorkflowExecutionSummary", self.source)
        self.assertIn("execution_summary?: WorkflowExecutionSummary;", self.source)
        self.assertIn("function workflowExecutionLine", self.source)
        self.assertIn("function jobBacking(job: MediaJob | null", self.source)
        self.assertIn("local / ComfyUI-backed", self.source)
        self.assertIn("external / non-Comfy-backed", self.source)
        self.assertIn("local / non-Comfy-backed", self.source)
        self.assertIn('aria-label="Job backing"', self.source)
        self.assertIn("<div><dt>Backing</dt><dd>{backing.label}</dd></div>", self.source)
        self.assertIn("<div><dt>Plan</dt><dd>{workflow ? workflowExecutionLine(workflow) : \"none\"}</dd></div>", self.source)
        self.assertIn("workflowExecutionLine(workflow)", self.source)
        self.assertIn("<td>{job.runtime}<small>{jobBacking(job).label}</small></td>", self.source)

    def test_workflow_presets_are_rendered_and_merge_safe_values(self) -> None:
        self.assertIn("type WorkflowPreset", self.source)
        self.assertIn("presets?: WorkflowPreset[];", self.source)
        self.assertIn("function safePresetEntries", self.source)
        self.assertIn('properties[name].contentEncoding !== "base64"', self.source)
        self.assertIn("function applyWorkflowPreset", self.source)
        self.assertIn("next[name] = value;", self.source)
        self.assertIn("function WorkflowPresets", self.source)
        self.assertIn('aria-label="Workflow presets"', self.source)
        self.assertIn("setValues((current) => applyWorkflowPreset(selectedWorkflow, preset, current));", self.source)
        self.assertIn("<WorkflowPresets workflow={selectedWorkflow} onApply={applyPreset} />", self.source)


if __name__ == "__main__":
    unittest.main()
