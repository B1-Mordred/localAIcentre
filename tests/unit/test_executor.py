from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import executor, secret_store, security  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "sqlalchemy":
        raise
    executor = None
    secret_store = None  # type: ignore[assignment]
    security = None  # type: ignore[assignment]


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8ffff3f0005fe02fea7f3c553"
    "0000000049454e44ae426082"
)
WAV_BYTES = (
    b"RIFF"
    + (38).to_bytes(4, "little")
    + b"WAVE"
    + b"fmt "
    + (16).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little")
    + (16000).to_bytes(4, "little")
    + (32000).to_bytes(4, "little")
    + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little")
    + b"data"
    + (2).to_bytes(4, "little")
    + b"\x00\x00"
)


def pcm_wav_bytes(duration_ms: int = 500, sample_rate: int = 16000) -> bytes:
    sample_count = max(1, int(sample_rate * duration_ms / 1000))
    data = b"\x00\x00" * sample_count
    byte_rate = sample_rate * 2
    block_align = 2
    return (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )


class FakeDatabase:
    def __init__(self, lease_acquired: bool = True, runtime: str = "voicebox", claim_job: bool = True) -> None:
        self.lease_acquired = lease_acquired
        self.claim_job = claim_job
        self.claims: list[dict[str, Any]] = []
        self.model_download_claims = 0
        self.updates: list[dict[str, Any]] = []
        self.releases: list[str] = []
        self.workflow: dict[str, Any] | None = None
        self.runtime_states: dict[str, dict[str, Any]] = {}
        self.runtime_state_updates: list[dict[str, Any]] = []
        self.alias_policies: dict[str, dict[str, Any]] = {}
        self.voice_profiles: dict[str, dict[str, Any]] = {}
        self.waiting_requeues = 1
        self.job = {
            "id": "job_gpu",
            "runtime": runtime,
            "model_alias": "image-default",
            "resolved_model_version": "model@1",
            "modality": "image",
            "operation": "generation",
            "state": "queued",
            "request_params": {},
            "artifacts": [],
        }
        self.model_download: dict[str, Any] | None = None
        self.encrypted_secrets: dict[str, dict[str, Any]] = {}

    async def acquire_scheduler_owner(self, owner: str, ttl_seconds: int) -> dict[str, Any]:
        return {"owner": owner, "ttl_seconds": ttl_seconds, "acquired": self.lease_acquired}

    async def release_scheduler_owner(self, owner: str) -> dict[str, Any]:
        self.releases.append(owner)
        return {"owner": owner, "released": True}

    async def claim_next_job(self, runtime_names: list[str], **kwargs: Any) -> dict[str, Any] | None:
        self.claims.append({"runtime_names": runtime_names, **kwargs})
        if not self.claim_job:
            return None
        self.job.update(
            {
                "state": kwargs.get("claimed_state", "running"),
                "stage": kwargs.get("claimed_stage", "running"),
                "progress": kwargs.get("claimed_progress", 50),
            }
        )
        self.updates.append({"state": self.job["state"], "stage": self.job["stage"], "progress": self.job["progress"], "claim": True})
        return dict(self.job)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return dict(self.job)

    async def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        self.job.update(changes)
        self.updates.append(dict(changes))
        return dict(self.job)

    async def get_workflow(self, workflow_id: str, version: str | None = None) -> dict[str, Any] | None:
        if self.workflow and self.workflow["id"] == workflow_id and self.workflow["version"] == version:
            return dict(self.workflow)
        return None

    async def mark_interrupted_jobs_recovery_required(self, runtime_names: list[str]) -> int:
        return 2

    async def mark_interrupted_jobs_recovery_required_report(self, runtime_names: list[str]) -> dict[str, Any]:
        return {"marked_recovery_required": 2, "recovery_required_job_ids": ["job_active_1", "job_active_2"]}

    async def requeue_interrupted_waiting_jobs(self, runtime_names: list[str]) -> int:
        return self.waiting_requeues

    async def requeue_interrupted_waiting_jobs_report(self, runtime_names: list[str]) -> dict[str, Any]:
        return {
            "requeued": self.waiting_requeues,
            "requeued_job_ids": [f"job_waiting_{index}" for index in range(1, self.waiting_requeues + 1)],
        }

    async def reconcile_interrupted_model_downloads(self) -> dict[str, Any]:
        return {
            "marked_recovery_required": 0,
            "requeued": 3,
            "paused": 1,
            "cancelled": 2,
            "requeued_download_ids": ["modeldl_running_1", "modeldl_running_2", "modeldl_running_3"],
            "paused_download_ids": ["modeldl_pausing_1"],
            "cancelled_download_ids": ["modeldl_cancelling_1", "modeldl_cancelling_2"],
        }

    async def claim_next_model_download(self) -> dict[str, Any] | None:
        self.model_download_claims += 1
        if self.model_download is None or self.model_download["status"] not in {"queued", "running", "pausing", "cancelling"}:
            return None
        if self.model_download["status"] in {"pausing", "cancelling"}:
            self.model_download.update({"stage": self.model_download["status"]})
            return dict(self.model_download)
        self.model_download.update({"status": "running", "stage": "downloading"})
        return dict(self.model_download)

    async def get_model_download(self, download_id: str) -> dict[str, Any] | None:
        if self.model_download is None or self.model_download["id"] != download_id:
            return None
        return dict(self.model_download)

    async def update_model_download(self, download_id: str, **changes: Any) -> dict[str, Any]:
        if self.model_download is None:
            raise AssertionError("missing model download")
        self.model_download.update(changes)
        self.updates.append(dict(changes))
        return dict(self.model_download)

    async def get_encrypted_secret(self, name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        row = self.encrypted_secrets.get(name)
        if row is None or (row.get("deleted_at") and not include_deleted):
            return None
        return dict(row)

    async def upsert_runtime_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"updated_at": datetime.now(tz=UTC), **payload}
        self.runtime_states[payload["runtime"]] = row
        self.runtime_state_updates.append(dict(row))
        return dict(row)

    async def list_runtime_states(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.runtime_states.values()]

    async def get_model_alias_policy(self, alias: str) -> dict[str, Any] | None:
        row = self.alias_policies.get(alias)
        return dict(row) if row is not None else None

    async def get_voice_profile(self, profile_id: str) -> dict[str, Any] | None:
        row = self.voice_profiles.get(profile_id)
        return dict(row) if row is not None else None


@unittest.skipIf(executor is None, "SQLAlchemy is not installed in this lightweight test environment")
class ExecutorTests(unittest.TestCase):
    def test_p40_seated_alias_uses_managed_seated_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))

        self.assertTrue(
            runner.is_studio_seated_character_job(
                {
                    "modality": "image",
                    "operation": "studio-seated-character",
                    "model_alias": "studio-seated-character-p40",
                    "runtime": "lan-p40-media",
                }
            )
        )

    def test_gpu_runner_does_not_claim_new_media_while_interactive_chat_waits(self) -> None:
        fake = FakeDatabase(runtime="lan-p40-media")

        async def has_interactive_gpu_waiter() -> bool:
            return True

        fake.has_interactive_gpu_waiter = has_interactive_gpu_waiter  # type: ignore[attr-defined]
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(fake.claims, [])
        self.assertEqual(fake.releases, [])

    def test_p40_media_jobs_use_remote_authenticated_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(
                Path(tmp),
                runtime_control_token="secret-token",
                runtime_urls={
                    "comfyui": "http://comfyui:8000",
                    "lipsync": "http://lipsync:8000",
                    "lan-p40-media": "https://p40-worker.b1.germering:9443/media",
                },
                runtime_tls_ca_files={"lan-p40-media": "/run/secrets/p40-ca.crt"},
            )

        remote_job = {"runtime": "lan-p40-media"}
        local_job = {"runtime": "comfyui"}
        self.assertEqual(runner.comfyui_url_for_job(remote_job), "https://p40-worker.b1.germering:9443/media/comfyui")
        self.assertEqual(runner.lipsync_url_for_job(remote_job), "https://p40-worker.b1.germering:9443/media/lipsync")
        self.assertEqual(runner.media_runtime_headers(remote_job), {"Authorization": "Bearer secret-token"})
        self.assertEqual(
            runner.lipsync_runtime_headers(remote_job),
            {"Authorization": "Bearer secret-token", "X-B1-Runtime-Token": "secret-token"},
        )
        self.assertEqual(runner.media_runtime_client_kwargs(remote_job), {"trust_env": False, "verify": "/run/secrets/p40-ca.crt"})
        self.assertEqual(runner.comfyui_url_for_job(local_job), "http://comfyui:8000")
        self.assertEqual(runner.lipsync_url_for_job(local_job), "http://lipsync:8000")
        self.assertEqual(runner.media_runtime_headers(local_job), {})
        self.assertEqual(runner.lipsync_runtime_headers(local_job), {"X-B1-Runtime-Token": "secret-token"})

    def setUp(self) -> None:
        original_resolver = security.resolve_hostname_addresses
        security.resolve_hostname_addresses = lambda hostname, port: ["93.184.216.34"]
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original_resolver))

    def patch_database(self, fake: FakeDatabase):
        original = executor.database
        executor.database = fake
        self.addCleanup(lambda: setattr(executor, "database", original))

    def test_native_scene_camera_plans_detail_sufficient_two_shot(self) -> None:
        regions = [
            {"participant_id": "grok", "seat": 5, "face_region": {"x": 0.589, "y": 0.455, "width": 0.072, "height": 0.145}},
            {"participant_id": "mistral", "seat": 6, "face_region": {"x": 0.674, "y": 0.455, "width": 0.072, "height": 0.145}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            plan = runner.native_scene_camera_plan(
                scene_width=1280,
                scene_height=720,
                output_width=1024,
                output_height=576,
                payload={
                    "camera_view": "panel_two_shot",
                    "speaker_participant_id": "grok",
                    "framed_participant_ids": ["grok", "mistral"],
                    "face_regions": regions,
                },
                speaker_region=regions[0],
            )

        self.assertEqual(plan["view"], "panel_two_shot")
        self.assertEqual(plan["framed_participant_ids"], ["grok", "mistral"])
        self.assertGreaterEqual(plan["speaker_face_height_px"], 110)
        self.assertGreater(plan["wall_screen"]["width"], 0)

    def test_native_scene_camera_rejects_low_resolution_master(self) -> None:
        region = {"participant_id": "grok", "seat": 5, "face_region": {"x": 0.589, "y": 0.455, "width": 0.072, "height": 0.145}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            with self.assertRaises(executor.CameraCoverageError):
                runner.native_scene_camera_plan(
                    scene_width=512,
                    scene_height=288,
                    output_width=1024,
                    output_height=576,
                    payload={"camera_view": "speaker_medium", "speaker_participant_id": "grok", "face_regions": [region]},
                    speaker_region=region,
                )

    def test_native_scene_camera_close_up_preserves_detail_at_rounding_boundary(self) -> None:
        region = {
            "participant_id": "mistral",
            "seat": 5,
            "face_region": {"x": 0.6041, "y": 0.4685, "width": 0.0393, "height": 0.0603},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            plan = runner.native_scene_camera_plan(
                scene_width=1280,
                scene_height=720,
                output_width=1024,
                output_height=576,
                payload={
                    "camera_view": "speaker_close",
                    "speaker_participant_id": "mistral",
                    "framed_participant_ids": ["mistral"],
                    "face_regions": [region],
                },
                speaker_region=region,
            )

        self.assertEqual(plan["view"], "speaker_close")
        self.assertGreaterEqual(plan["speaker_face_height_px"], 220)

    def test_declared_scene_face_region_is_transformed_into_speaker_crop(self) -> None:
        face_region = {"x": 0.40, "y": 0.30, "width": 0.10, "height": 0.20}
        crop = (400, 168, 100, 144)

        bbox = executor.GpuJobRunner.normalized_face_bbox_in_crop(1000, 600, face_region, crop)

        self.assertAlmostEqual(bbox["x"], 0.0)
        self.assertAlmostEqual(bbox["y"], 1 / 12)
        self.assertAlmostEqual(bbox["width"], 1.0)
        self.assertAlmostEqual(bbox["height"], 5 / 6)

    def test_seated_plate_anatomy_metrics_detects_spread_disconnected_legs(self) -> None:
        alpha = executor.Image.new("L", (200, 300), 0)
        draw = executor.ImageDraw.Draw(alpha)
        draw.ellipse((70, 15, 130, 100), fill=255)
        draw.rectangle((55, 85, 145, 165), fill=255)
        # The historic failure mode: broad separate legs with a large gap.
        draw.rectangle((20, 160, 75, 230), fill=255)
        draw.rectangle((125, 160, 180, 230), fill=255)

        metrics = executor.GpuJobRunner.seated_plate_anatomy_metrics(alpha)

        self.assertGreater(metrics["lower_body_width_ratio"], 0.66)
        self.assertGreater(metrics["lower_body_gap_ratio"], 0.18)

    def test_seated_plate_anatomy_metrics_allows_narrow_desk_occluded_pose(self) -> None:
        alpha = executor.Image.new("L", (200, 300), 0)
        draw = executor.ImageDraw.Draw(alpha)
        draw.ellipse((72, 15, 128, 100), fill=255)
        draw.rectangle((55, 85, 145, 190), fill=255)
        draw.rectangle((62, 165, 138, 220), fill=255)

        metrics = executor.GpuJobRunner.seated_plate_anatomy_metrics(alpha)

        self.assertLess(metrics["head_width_ratio"], 0.35)
        self.assertLess(metrics["head_max_width_ratio"], 0.35)
        self.assertLess(metrics["lower_body_width_ratio"], 0.66)
        self.assertLess(metrics["lower_body_gap_ratio"], 0.18)

    def test_seated_plate_halo_metrics_detects_opaque_source_card(self) -> None:
        alpha = executor.Image.new("L", (200, 300), 0)
        draw = executor.ImageDraw.Draw(alpha)
        draw.polygon([(60, 18), (140, 18), (168, 180), (32, 180)], fill=180)
        draw.ellipse((78, 24, 122, 100), fill=255)

        metrics = executor.GpuJobRunner.seated_plate_halo_metrics(alpha)

        self.assertGreater(metrics["matte_halo_max_alpha"], 8)

    def test_seated_plate_halo_metrics_allows_isolated_robot_silhouette(self) -> None:
        alpha = executor.Image.new("L", (200, 300), 0)
        draw = executor.ImageDraw.Draw(alpha)
        draw.ellipse((90, 24, 110, 100), fill=255)
        draw.rectangle((72, 88, 128, 190), fill=255)

        metrics = executor.GpuJobRunner.seated_plate_halo_metrics(alpha)

        self.assertEqual(metrics["matte_halo_max_alpha"], 0)

    def test_seated_general_matte_uses_pinned_content_addressed_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp) / "artifacts")

            self.assertEqual(
                runner.seated_general_matte_model_path(),
                Path(tmp) / "models" / "blobs" / executor.SEATED_GENERAL_MATTE_SHA256,
            )

    def test_runtime_control_payload_carries_selected_runtime_smoke_config(self) -> None:
        prompt = {"1": {"class_type": "B1RuntimeTinyImage", "inputs": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            payload = runner.runtime_control_payload(
                {
                    "id": "job_smoke",
                    "runtime": "comfyui",
                    "model_alias": "image-default",
                    "resolved_model_version": "image-small@1.0.0",
                    "modality": "image",
                    "operation": "model-smoke",
                    "request_params": {
                        "runtime_smoke": {
                            "schema": "b1-ai-hub-runtime-smoke/v1",
                            "localai": {"request": {"messages": [{"role": "user", "content": "ping"}]}},
                            "comfyui": {"prompt": prompt},
                        }
                    },
                }
            )

        self.assertEqual(payload["runtime_smoke"]["schema"], "b1-ai-hub-runtime-smoke/v1")
        self.assertEqual(payload["runtime_smoke_config"], {"prompt": prompt})
        self.assertEqual(payload["model"], "image-small")

    def test_cpu_runner_pause_hook_skips_claiming_work(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.CpuJobRunner(Path(tmp), pause_check=lambda: True)
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(fake.claims, [])

    def test_panel_cpu_runner_claims_compositor_without_gpu_lease(self) -> None:
        fake = FakeDatabase(runtime="panel-cpu")
        fake.job.update(
            {
                "model_alias": "studio-panel-shot",
                "resolved_model_version": "b1-studio-panel-shot-compositor@20260809.1",
                "modality": "image",
                "operation": "studio-panel-shot",
            }
        )
        self.patch_database(fake)

        class PanelRunner(executor.CpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root)
                self.panel_jobs: list[str] = []

            async def run_panel_cpu_job(self, job: dict[str, Any]) -> bool:
                self.panel_jobs.append(str(job["id"]))
                await executor.database.update_job(job["id"], state="completed", stage="completed", progress=100)
                return True

        with tempfile.TemporaryDirectory() as tmp:
            runner = PanelRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertEqual(runner.panel_jobs, ["job_gpu"])
        self.assertIn("panel-cpu", fake.claims[0]["runtime_names"])
        self.assertEqual(fake.releases, [])

    def test_panel_compositor_normalizes_visible_height_to_named_reference(self) -> None:
        def png(image: Any) -> bytes:
            output = executor.io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()

        studio = executor.Image.new("RGB", (1280, 720), (24, 34, 52))
        figures = {
            "claude": (300, 300, (70, 150, 230, 255)),
            "deepseek": (180, 390, (40, 180, 130, 255)),
            "grok": (340, 240, (180, 90, 190, 255)),
        }
        participants = []
        for seat, (participant_id, (figure_width, figure_height, color)) in enumerate(
            figures.items(), start=1
        ):
            plate = executor.Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
            left, top = 640 - figure_width // 2, 80
            executor.ImageDraw.Draw(plate).rectangle(
                (left, top, left + figure_width - 1, top + figure_height - 1),
                fill=color,
            )
            participants.append(
                {
                    "participant_id": participant_id,
                    "seat": seat,
                    "seated_plate": png(plate),
                    "seated_character": {
                        "face_region": {
                            "x": (left + figure_width * 0.3) / 1280,
                            "y": (top + figure_height * 0.04) / 720,
                            "width": figure_width * 0.4 / 1280,
                            "height": figure_height * 0.28 / 720,
                        }
                    },
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            _, _, occupancy, qc = runner.compose_studio_panel_image(
                studio=png(studio),
                participants=participants,
                width=1280,
                height=720,
                stature_reference_participant_id="claude",
            )

        body_heights = {
            entry["participant_id"]: round(entry["body_region"]["height"] * 720)
            for entry in occupancy
        }
        self.assertEqual(set(body_heights.values()), {96})
        self.assertEqual(qc["stature_reference_participant_id"], "claude")
        self.assertEqual(qc["target_body_height_px"], 96)
        self.assertEqual(qc["body_height_spread_px"], 0)
        self.assertEqual(qc["body_height_spread_ratio"], 0.0)

    def test_panel_compositor_rejects_unknown_stature_reference(self) -> None:
        studio = executor.Image.new("RGB", (1280, 720), (24, 34, 52))
        studio_output = executor.io.BytesIO()
        studio.save(studio_output, format="PNG")
        plate = executor.Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
        executor.ImageDraw.Draw(plate).rectangle((500, 80, 780, 400), fill=(70, 150, 230, 255))
        plate_output = executor.io.BytesIO()
        plate.save(plate_output, format="PNG")
        participant = {
            "participant_id": "claude",
            "seat": 1,
            "seated_plate": plate_output.getvalue(),
            "seated_character": {
                "face_region": {"x": 0.46, "y": 0.14, "width": 0.08, "height": 0.12}
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            with self.assertRaisesRegex(
                executor.StudioPanelQualityError, "stature reference"
            ):
                runner.compose_studio_panel_image(
                    studio=studio_output.getvalue(),
                    participants=[participant],
                    width=1280,
                    height=720,
                    stature_reference_participant_id="missing",
                )

    def test_gpu_runner_async_pause_hook_skips_claiming_work(self) -> None:
        fake = FakeDatabase(runtime="localai")
        self.patch_database(fake)

        async def paused() -> bool:
            return True

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp), interval_seconds=1, lease_ttl_seconds=60, pause_check=paused)
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(fake.claims, [])
        self.assertEqual(fake.releases, [])

    def test_model_download_runner_pause_hook_skips_claiming_work(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        fake.model_download = {"id": "modeldl_1", "status": "queued", "stage": "queued", "manifest": {}, "bytes_downloaded": 0}
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.ModelDownloadRunner(Path(tmp), pause_check=lambda: True)
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(fake.model_download_claims, 0)

    def test_model_download_runner_marks_pausing_download_paused(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        fake.model_download = {"id": "modeldl_pause", "status": "pausing", "stage": "pausing", "manifest": {}, "bytes_downloaded": 8}
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.ModelDownloadRunner(Path(tmp))
            stopped = asyncio.run(runner.stop_if_requested("modeldl_pause"))

        self.assertTrue(stopped)
        self.assertEqual(fake.model_download["status"], "paused")
        self.assertEqual(fake.model_download["stage"], "paused")

    def test_model_download_runner_recovers_persisted_pausing_download(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        fake.model_download = {"id": "modeldl_pause", "status": "pausing", "stage": "pausing", "manifest": {}, "bytes_downloaded": 8}
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.ModelDownloadRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertEqual(fake.model_download_claims, 1)
        self.assertEqual(fake.model_download["status"], "paused")
        self.assertEqual(fake.model_download["stage"], "paused")

    def test_cpu_runner_submits_tts_job_and_stores_audio_artifact(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        fake.job.update(
            {
                "model_alias": "tts-fast",
                "resolved_model_version": "b1-cpu-placeholder-tts@0.1.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {
                    "input": {
                        "workflow_id": "tts",
                        "workflow_version": "0.1.0",
                        "parameters": {"text": "hello", "voice": "default"},
                    }
                },
            }
        )
        self.patch_database(fake)

        class AudioRunner(executor.CpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, audio_cpu_url="http://audio-cpu")
                self.payloads: list[dict[str, Any]] = []

            async def post_audio_cpu_speech(self, payload: dict[str, Any]) -> executor.AudioCpuSpeechResult:
                self.payloads.append(payload)
                return b"wav-bytes", "audio/wav", {"b1_placeholder": True, "b1_cpu_audio_engine": "scaffold"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = AudioRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                runner.payloads,
                [
                    {
                        "model": "b1-cpu-placeholder-tts",
                        "b1_cpu_residency_allowed": True,
                        "b1_model_alias": "tts-fast",
                        "b1_resolved_model_version": "b1-cpu-placeholder-tts@0.1.0",
                        "text": "hello",
                        "voice": "default",
                    }
                ],
            )
            self.assertEqual(fake.job["state"], "completed")
            self.assertIsNotNone(fake.job["started_at"])
            self.assertIsInstance(fake.job["run_time_ms"], int)
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "audio-cpu")
            self.assertEqual(artifact["source"], "audio_cpu_runtime")
            self.assertEqual(artifact["kind"], "audio")
            self.assertEqual(artifact["mime_type"], "audio/wav")
            self.assertTrue(artifact["b1_placeholder"])
            self.assertEqual(artifact["b1_cpu_audio_engine"], "scaffold")
            self.assertEqual(artifact["sha256"], hashlib.sha256(b"wav-bytes").hexdigest())
            self.assertEqual((Path(tmp) / "audio-cpu" / "job_gpu" / "0.wav").read_bytes(), b"wav-bytes")

    def test_cpu_runner_cancels_blocking_tts_call(self) -> None:
        class CancellingDatabase(FakeDatabase):
            async def get_job(self, job_id: str) -> dict[str, Any]:
                if getattr(self, "call_started", False) and self.job.get("stage") == "audio_cpu_speech" and self.job.get("state") == "running":
                    self.job["state"] = "cancelling"
                return dict(self.job)

        fake = CancellingDatabase(runtime="audio-cpu")
        fake.call_started = False
        fake.job.update(
            {
                "model_alias": "tts-fast",
                "resolved_model_version": "b1-cpu-placeholder-tts@0.1.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "cancel me"}},
            }
        )
        self.patch_database(fake)

        class AudioCancelRunner(executor.CpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    audio_cpu_url="http://audio-cpu",
                    runtime_cancel_poll_seconds=0.05,
                )
                self.runtime_call_cancelled = False

            async def post_audio_cpu_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                fake.call_started = True
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.runtime_call_cancelled = True
                    raise
                raise AssertionError("audio-cpu speech call should be cancelled before it returns")

        with tempfile.TemporaryDirectory() as tmp:
            runner = AudioCancelRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertTrue(runner.runtime_call_cancelled)
        self.assertEqual(fake.job["state"], "cancelled")
        self.assertEqual(fake.job["stage"], "cancelled")
        self.assertEqual(fake.job["artifacts"], [])
        self.assertIsInstance(fake.job["run_time_ms"], int)

    def test_cpu_runner_submits_stt_job_and_stores_transcript_artifact(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        fake.job.update(
            {
                "model_alias": "stt-default",
                "resolved_model_version": "b1-cpu-placeholder-stt@0.1.0",
                "modality": "stt",
                "operation": "transcription",
                "request_params": {
                    "input": {
                        "parameters": {"audio": "UklGRg==", "language": "en"},
                    }
                },
            }
        )
        self.patch_database(fake)

        class AudioRunner(executor.CpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, audio_cpu_url="http://audio-cpu")
                self.payloads: list[dict[str, Any]] = []

            async def post_audio_cpu_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
                self.payloads.append(payload)
                return {
                    "text": "hello transcript",
                    "language": "en",
                    "b1_engine": "scaffold",
                    "b1_placeholder": True,
                }

        with tempfile.TemporaryDirectory() as tmp:
            runner = AudioRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                runner.payloads,
                [
                    {
                        "model": "b1-cpu-placeholder-stt",
                        "b1_cpu_residency_allowed": True,
                        "b1_model_alias": "stt-default",
                        "b1_resolved_model_version": "b1-cpu-placeholder-stt@0.1.0",
                        "audio": "UklGRg==",
                        "language": "en",
                    }
                ],
            )
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "audio-cpu")
            self.assertEqual(artifact["text"], "hello transcript")
            self.assertTrue(artifact["b1_placeholder"])
            self.assertEqual(artifact["b1_engine"], "scaffold")
            self.assertEqual(artifact["mime_type"], "application/json")
            self.assertIn(b"hello transcript", (Path(tmp) / "audio-cpu" / "job_gpu" / "0.json").read_bytes())

    def test_cpu_runner_marks_audio_payload_not_resident_when_policy_disables_alias(self) -> None:
        job = {
            "model_alias": "tts-fast",
            "resolved_model_version": "b1-cpu-placeholder-tts@0.1.0",
            "request_params": {"input": {"parameters": {"text": "hello"}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.CpuJobRunner(
                Path(tmp),
                audio_cpu_url="http://audio-cpu",
                resource_policy_provider=lambda: executor.ResourcePolicy(cpu_residency_enabled=False),
            )
            payload = runner.audio_payload_for_job(job)

        self.assertEqual(payload["model"], "b1-cpu-placeholder-tts")
        self.assertEqual(payload["b1_model_alias"], "tts-fast")
        self.assertFalse(payload["b1_cpu_residency_allowed"])

    def test_cpu_runner_expands_staged_audio_upload_for_transcription(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        self.patch_database(fake)

        class AudioRunner(executor.CpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, audio_cpu_url="http://audio-cpu")
                self.payloads: list[dict[str, Any]] = []

            async def post_audio_cpu_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
                self.payloads.append(payload)
                return {"text": "staged transcript"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="audio",
                filename="clip.wav",
                declared_mime_type="audio/wav",
                content=WAV_BYTES,
            )
            fake.job.update(
                {
                    "model_alias": "stt-default",
                    "resolved_model_version": "b1-cpu-placeholder-stt@0.1.0",
                    "modality": "stt",
                    "operation": "transcription",
                    "request_params": {"input": {"audio": staged, "parameters": {"language": "en"}}},
                }
            )
            runner = AudioRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(runner.payloads[0]["audio"], base64.b64encode(WAV_BYTES).decode("ascii"))
            self.assertEqual(runner.payloads[0]["audio_mime_type"], "audio/wav")
            self.assertEqual(runner.payloads[0]["filename"], "clip.wav")
            self.assertEqual(fake.job["state"], "completed")

    def test_cpu_runner_marks_stt_job_recovery_required_when_text_is_missing(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        fake.job.update(
            {
                "model_alias": "stt-default",
                "resolved_model_version": "b1-cpu-placeholder-stt@0.1.0",
                "modality": "stt",
                "operation": "transcription",
                "request_params": {"input": {"audio": "UklGRg=="}},
            }
        )
        self.patch_database(fake)

        class AudioRunner(executor.CpuJobRunner):
            async def post_audio_cpu_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
                return {"language": "en"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = AudioRunner(Path(tmp), audio_cpu_url="http://audio-cpu")
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["state"], "recovery_required")
            self.assertEqual(fake.job["failure_category"], "audio_cpu_transcription_missing_text")
            self.assertEqual(fake.job["artifacts"], [])

    def test_cpu_runner_fails_unknown_audio_cpu_job_without_placeholder_artifact(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        fake.job.update({"modality": "embedding", "operation": "embeddings", "model_alias": "embedding-default"})
        self.patch_database(fake)

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.CpuJobRunner(Path(tmp), audio_cpu_url="http://audio-cpu")
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["state"], "failed")
            self.assertEqual(fake.job["stage"], "unsupported_audio_cpu_operation")
            self.assertEqual(fake.job["failure_category"], "unsupported_audio_cpu_operation")
            self.assertIn("audio-cpu", fake.job["failure_message"])
            self.assertEqual(fake.job["artifacts"], [])
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())

    def test_gpu_runner_walks_exclusive_gpu_state_path_and_fails_unknown_job_shape(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp), interval_seconds=1, lease_ttl_seconds=60)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            states = [item["state"] for item in fake.updates if "state" in item]
            self.assertEqual(
                states,
                [
                    "waiting_for_gpu",
                    "unloading",
                    "verifying_vram",
                    "loading",
                    "warming",
                    "running",
                    "failed",
                ],
            )
            self.assertEqual(fake.claims[0]["claimable_states"], ["queued", "waiting_for_gpu"])
            self.assertTrue(fake.claims[0]["respect_runtime_reservations"])
            self.assertIsNotNone(fake.job["started_at"])
            self.assertIsInstance(fake.job["load_time_ms"], int)
            self.assertIsInstance(fake.job["run_time_ms"], int)
            self.assertEqual(fake.job["failure_category"], "unsupported_gpu_operation")
            self.assertEqual(fake.job["artifacts"], [])
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())
            self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_unloads_other_gpu_runtimes_before_vram_verification(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job.update({"modality": "llm", "operation": "chat"})
        self.patch_database(fake)

        class HookRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_agent_url="http://runtime-agent")
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return {"gpu": {"available": True, "devices": [{"memory_used_mib": 0}]}}

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "dry_run", "action": "unload"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = HookRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                [path for path, _ in runner.posts],
                ["/v1/runtime-actions/comfyui/unload", "/v1/runtime-actions/voicebox/unload"],
            )
            self.assertIn("prepare localai for job job_gpu", runner.posts[0][1]["reason"])
            self.assertEqual(fake.runtime_states["comfyui"]["status"], "unload_dry_run")
            self.assertIsNone(fake.runtime_states["comfyui"]["active_model"])
            self.assertEqual(fake.runtime_states["comfyui"]["details"]["target_runtime"], "localai")
            self.assertEqual(fake.job["state"], "failed")
            self.assertEqual(fake.job["failure_category"], "unsupported_gpu_operation")

    def test_gpu_runner_tries_graceful_unload_before_restart_fallback(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.runtime_states["comfyui"] = {
            "runtime": "comfyui",
            "status": "idle",
            "stage": "idle",
            "active_model": "sdxl-low-vram",
            "model_alias": "image-default",
            "resolved_model_version": "sdxl-low-vram@1",
            "job_id": "job_image",
            "details": {},
            "updated_at": datetime.now(tz=UTC),
        }
        fake.runtime_states["voicebox"] = {
            "runtime": "voicebox",
            "status": "idle",
            "stage": "idle",
            "active_model": "voicebox-quality",
            "model_alias": "tts-quality",
            "resolved_model_version": "voicebox-quality@1",
            "job_id": "job_voice",
            "details": {},
            "updated_at": datetime.now(tz=UTC),
        }
        self.patch_database(fake)

        class GracefulRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    runtime_urls={"comfyui": "http://comfyui", "voicebox": "http://voicebox"},
                )
                self.controls: list[tuple[str, str, dict[str, Any]]] = []
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "runtime": runtime, "action": action, "strategy": "backend_shutdown"}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return {"gpu": {"available": True, "devices": [{"memory_used_mib": 0}]}}

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "action": "unload"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = GracefulRunner(Path(tmp))
            results = asyncio.run(runner.unload_other_gpu_runtimes({"id": "job_gpu", "runtime": "localai"}))

        self.assertEqual([result["status"] for result in results if isinstance(result, dict)], ["ok", "ok"])
        self.assertEqual([(runtime, action) for runtime, action, _ in runner.controls], [("comfyui", "unload"), ("voicebox", "unload")])
        self.assertEqual(runner.posts, [])
        self.assertEqual(runner.controls[0][2]["model"], "sdxl-low-vram")
        self.assertEqual(runner.controls[1][2]["resolved_model_version"], "voicebox-quality@1")
        self.assertEqual(fake.runtime_states["comfyui"]["status"], "unload_ok")
        self.assertEqual(fake.runtime_states["comfyui"]["details"]["hook"]["strategy"], "backend_shutdown")
        self.assertEqual(fake.runtime_states["voicebox"]["status"], "unload_ok")

    def test_gpu_runner_skips_explicitly_unloaded_runtimes_on_warm_reuse(self) -> None:
        fake = FakeDatabase(runtime="lan-localai-worker")
        now = datetime.now(tz=UTC)
        fake.runtime_states.update(
            {
                "lan-localai-worker": {
                    "runtime": "lan-localai-worker",
                    "status": "idle",
                    "stage": "idle",
                    "active_model": "laguna-current",
                    "model_alias": "laguna-s-quality",
                    "resolved_model_version": "laguna-current@1",
                    "updated_at": now,
                },
                "lan-p40-media": {
                    "runtime": "lan-p40-media",
                    "status": "unload_ok",
                    "stage": "unloading",
                    "active_model": None,
                    "model_alias": None,
                    "resolved_model_version": None,
                    "updated_at": now,
                },
                "comfyui": {
                    "runtime": "comfyui",
                    "status": "unload_ok",
                    "stage": "unloading",
                    "active_model": None,
                    "model_alias": None,
                    "resolved_model_version": None,
                    "updated_at": now,
                },
                "localai": {
                    "runtime": "localai",
                    "status": "unload_ok",
                    "stage": "unloading",
                    "active_model": None,
                    "model_alias": None,
                    "resolved_model_version": None,
                    "updated_at": now,
                },
                "voicebox": {
                    "runtime": "voicebox",
                    "status": "unload_ok",
                    "stage": "unloading",
                    "active_model": None,
                    "model_alias": None,
                    "resolved_model_version": None,
                    "updated_at": now,
                },
            }
        )
        self.patch_database(fake)

        class WarmReuseRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_urls={
                        "lan-localai-worker": "https://p40-worker.example/localai",
                        "lan-p40-media": "https://p40-worker.example/media",
                        "comfyui": "http://comfyui",
                    },
                )
                self.controls: list[tuple[str, str]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action))
                return {"status": "ok", "runtime": runtime, "action": action}

        job = {
            "id": "sync_chat_warm",
            "runtime": "lan-localai-worker",
            "model_alias": "laguna-s-quality",
            "resolved_model_version": "laguna-current@1",
            "modality": "llm",
            "operation": "chat",
            "request_params": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = WarmReuseRunner(Path(tmp))
            results = asyncio.run(runner.unload_other_gpu_runtimes(job))

        self.assertEqual(results, [])
        self.assertEqual(runner.controls, [])

    def test_gpu_runner_restarts_runtime_when_graceful_unload_leaves_vram_resident(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.runtime_states["voicebox"] = {
            "runtime": "voicebox",
            "status": "idle",
            "stage": "idle",
            "active_model": "voicebox-quality",
            "model_alias": "tts-quality",
            "resolved_model_version": "voicebox-quality@1",
            "job_id": "job_voice",
            "details": {},
            "updated_at": datetime.now(tz=UTC),
        }
        self.patch_database(fake)

        class StaleVramRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    runtime_urls={"voicebox": "http://voicebox"},
                    reserve_vram_gib=1.0,
                )
                self.controls: list[tuple[str, str, dict[str, Any]]] = []
                self.posts: list[tuple[str, dict[str, Any]]] = []
                self.metrics = [5632, 128]

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "runtime": runtime, "action": action, "strategy": "backend_shutdown"}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                used = self.metrics.pop(0)
                return {"gpu": {"available": True, "devices": [{"memory_used_mib": used}]}}

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "runtime": "voicebox", "action": "unload", "strategy": "restart_service"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = StaleVramRunner(Path(tmp))
            result = asyncio.run(runner.unload_target_runtime_for_model_switch(
                {
                    "id": "job_voice_switch",
                    "runtime": "voicebox",
                    "model_alias": "tts-other",
                    "resolved_model_version": "voicebox-other@1",
                    "modality": "tts",
                    "operation": "speech",
                    "request_params": {},
                }
            ))

        self.assertEqual(result["status"], "ok")
        self.assertEqual([(runtime, action) for runtime, action, _ in runner.controls], [("voicebox", "unload")])
        self.assertEqual([path for path, _ in runner.posts], ["/v1/runtime-actions/voicebox/unload"])
        state = fake.runtime_states["voicebox"]
        self.assertEqual(state["status"], "unload_ok")
        self.assertEqual(state["details"]["graceful_vram_failure"]["memory_used_mib"], 5632)
        self.assertEqual(state["details"]["restart_vram_verification"]["memory_used_mib"], 128)

    def test_gpu_runner_unloads_target_runtime_when_model_changes(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        fake.runtime_states["comfyui"] = {
            "runtime": "comfyui",
            "status": "idle",
            "stage": "idle",
            "active_model": "sd15-image",
            "model_alias": "image-edit",
            "resolved_model_version": "sd15-image@1",
            "job_id": "job_image",
            "details": {},
            "updated_at": datetime.now(tz=UTC),
        }
        self.patch_database(fake)

        class SwitchRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_urls={"comfyui": "http://comfyui"})
                self.controls: list[tuple[str, str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "runtime": runtime, "action": action, "strategy": "model_unload"}

        job = {
            "id": "job_video",
            "runtime": "comfyui",
            "model_alias": "video-text",
            "resolved_model_version": "sd15-video-sequence@1",
            "modality": "video",
            "operation": "generation",
            "request_params": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = SwitchRunner(Path(tmp))
            result = asyncio.run(runner.unload_target_runtime_for_model_switch(job))

        self.assertEqual(result["status"], "ok")
        self.assertEqual([(runtime, action) for runtime, action, _ in runner.controls], [("comfyui", "unload")])
        self.assertEqual(runner.controls[0][2]["model"], "sd15-image")
        self.assertEqual(fake.runtime_states["comfyui"]["status"], "unload_ok")
        self.assertIsNone(fake.runtime_states["comfyui"]["active_model"])
        self.assertTrue(fake.runtime_states["comfyui"]["details"]["same_runtime_model_switch"])
        self.assertEqual(fake.runtime_states["comfyui"]["details"]["previous_model"], "sd15-image@1")
        self.assertEqual(fake.runtime_states["comfyui"]["details"]["target_model"], "sd15-video-sequence@1")

    def test_gpu_runner_keeps_target_runtime_when_model_matches(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        fake.runtime_states["comfyui"] = {
            "runtime": "comfyui",
            "status": "idle",
            "stage": "idle",
            "active_model": "sd15-image",
            "model_alias": "image-default",
            "resolved_model_version": "sd15-image@1",
            "job_id": "job_image",
            "details": {},
            "updated_at": datetime.now(tz=UTC),
        }
        self.patch_database(fake)

        class SwitchRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_urls={"comfyui": "http://comfyui"})
                self.controls: list[tuple[str, str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "runtime": runtime, "action": action}

        job = {
            "id": "job_image",
            "runtime": "comfyui",
            "model_alias": "image-default",
            "resolved_model_version": "sd15-image@1",
            "modality": "image",
            "operation": "generation",
            "request_params": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = SwitchRunner(Path(tmp))
            result = asyncio.run(runner.unload_target_runtime_for_model_switch(job))

        self.assertIsNone(result)
        self.assertEqual(runner.controls, [])
        self.assertEqual(fake.runtime_states["comfyui"]["active_model"], "sd15-image")

    def test_gpu_runner_calls_runtime_load_and_warm_hooks(self) -> None:
        fake = FakeDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "hello"}},
            }
        )
        self.patch_database(fake)

        class HookRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_urls={"voicebox": "http://voicebox"})
                self.controls: list[tuple[str, str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                return b"voicebox-wav", "audio/wav"

        with tempfile.TemporaryDirectory() as tmp:
            runner = HookRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                [(runtime, action) for runtime, action, _ in runner.controls],
                [("localai", "unload"), ("comfyui", "unload"), ("voicebox", "load"), ("voicebox", "warm")],
            )
            self.assertEqual(runner.controls[2][2]["job_id"], "job_gpu")
            self.assertEqual(runner.controls[2][2]["model"], "voicebox-quality")
            self.assertEqual(runner.controls[2][2]["resolved_model_version"], "voicebox-quality@1.0.0")
            self.assertEqual(fake.runtime_states["voicebox"]["status"], "idle")
            self.assertEqual(fake.runtime_states["voicebox"]["active_model"], "voicebox-quality")
            self.assertEqual(fake.runtime_states["voicebox"]["stage"], "idle")
            self.assertEqual(fake.job["state"], "completed")

    def test_runtime_control_post_injects_shared_bearer_token(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b'{"status":"ok"}'

            def json(self) -> dict[str, str]:
                return {"status": "ok"}

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
                self.calls.append({"url": url, "json": dict(json), "headers": dict(headers), "client_kwargs": dict(self.kwargs)})
                return FakeResponse()

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(
                Path(tmp),
                runtime_urls={"comfyui": "http://comfyui:8188"},
                runtime_control_token="hook-token",
            )
            result = asyncio.run(runner.post_runtime_control("comfyui", "load", {"model": "image-default"}))

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(FakeAsyncClient.calls[0]["url"], "http://comfyui:8188/b1/runtime/load")
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Accept"], "application/json")
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer hook-token")
        self.assertFalse(FakeAsyncClient.calls[0]["client_kwargs"]["trust_env"])

    def test_runtime_control_failure_preserves_bounded_idle_evidence(self) -> None:
        class FakeResponse:
            status_code = 503
            content = b'{"status":"failed"}'

            def json(self) -> dict[str, Any]:
                return {
                    "status": "failed",
                    "runtime": "lan-p40-media",
                    "action": "recover",
                    "memory_used_mib": 744,
                    "baseline_mib": 256,
                    "margin_mib": 256,
                    "idle_threshold_mib": 512,
                    "stable_sample_count": 0,
                    "stable_sample_target": 3,
                    "memory_samples_mib": [744, 744, 744],
                    "detail": {"access_token": "must-not-escape"},
                }

        class FakeAsyncClient:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def post(
                self,
                _url: str,
                json: dict[str, Any],
                headers: dict[str, str],
            ) -> FakeResponse:
                return FakeResponse()

        original_client = executor.httpx.AsyncClient
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(
                Path(tmp),
                runtime_urls={"lan-p40-media": "https://worker.example/media"},
                runtime_control_token="hook-token",
            )
            with self.assertRaisesRegex(RuntimeError, "idle_threshold_mib.*512") as raised:
                asyncio.run(
                    runner.post_runtime_control(
                        "lan-p40-media",
                        "recover",
                        {"operation": "talking-head-lipsync"},
                    )
                )

        self.assertIn('"memory_samples_mib":[744,744,744]', str(raised.exception))
        self.assertNotIn("must-not-escape", str(raised.exception))

    def test_lan_worker_control_and_metrics_use_pinned_ca_and_bearer_token(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b'{"status":"ok"}'

            def json(self) -> dict[str, Any]:
                return {
                    "status": "ok",
                    "gpu": {"available": True, "devices": [{"memory_used_mib": 0}]},
                }

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
                self.calls.append({"method": "GET", "url": url, "headers": dict(headers), "client_kwargs": dict(self.kwargs)})
                return FakeResponse()

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
                self.calls.append({"method": "POST", "url": url, "headers": dict(headers), "client_kwargs": dict(self.kwargs)})
                return FakeResponse()

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(
                Path(tmp),
                runtime_urls={
                    "lan-localai-worker": "https://p40-worker.b1.germering:9443",
                    "lan-deepseek-worker": "https://p40-worker.b1.germering:9443/deepseek",
                },
                runtime_tls_ca_files={
                    "lan-localai-worker": "/run/trust/p40-worker.crt",
                    "lan-deepseek-worker": "/run/trust/p40-worker.crt",
                },
                runtime_control_token="hook-token",
            )
            control = asyncio.run(runner.post_runtime_control("lan-localai-worker", "load", {"model": "chat-quality"}))
            deepseek_control = asyncio.run(runner.post_runtime_control("lan-deepseek-worker", "load", {"model": "deepseek-main"}))
            released, verification = asyncio.run(runner.unload_vram_release_check("lan-localai-worker"))
            deepseek_released, deepseek_verification = asyncio.run(runner.unload_vram_release_check("lan-deepseek-worker"))

        self.assertEqual(control["status"], "ok")
        self.assertEqual(deepseek_control["status"], "ok")
        self.assertTrue(released)
        self.assertTrue(deepseek_released)
        self.assertEqual(verification["memory_used_mib"], 0)
        self.assertEqual(deepseek_verification["memory_used_mib"], 0)
        self.assertEqual([call["method"] for call in FakeAsyncClient.calls], ["POST", "POST", "GET", "GET"])
        self.assertEqual(FakeAsyncClient.calls[1]["url"], "https://p40-worker.b1.germering:9443/deepseek/b1/runtime/load")
        for call in FakeAsyncClient.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer hook-token")
            self.assertEqual(call["client_kwargs"]["verify"], "/run/trust/p40-worker.crt")
            self.assertFalse(call["client_kwargs"]["trust_env"])

    def test_lipsync_runtime_control_uses_internal_runtime_token_header(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b'{"status":"ready"}'

            def json(self) -> dict[str, str]:
                return {"status": "ready"}

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
                self.calls.append({"url": url, "json": dict(json), "headers": dict(headers)})
                return FakeResponse()

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(
                Path(tmp),
                runtime_urls={"lipsync": "http://lipsync:8000"},
                runtime_control_token="hook-token",
            )
            result = asyncio.run(runner.post_runtime_control("lipsync", "warm", {"model": "talking-head-lipsync"}))

        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(FakeAsyncClient.calls[0]["url"], "http://lipsync:8000/b1/runtime/warm")
        self.assertEqual(FakeAsyncClient.calls[0]["headers"]["X-B1-Runtime-Token"], "hook-token")
        self.assertNotIn("Authorization", FakeAsyncClient.calls[0]["headers"])

    def test_gpu_runner_fails_before_submission_when_load_hook_reports_failure(self) -> None:
        fake = FakeDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "hello"}},
            }
        )
        self.patch_database(fake)

        class FailingLoadRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_urls={"voicebox": "http://voicebox"})
                self.controls: list[tuple[str, str, dict[str, Any]]] = []
                self.submitted = False

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                if action == "load":
                    return {"status": "failed", "runtime": runtime, "action": action, "reason": "model_missing"}
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                self.submitted = True
                return b"voicebox-wav", "audio/wav"

        with tempfile.TemporaryDirectory() as tmp:
            runner = FailingLoadRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                [(runtime, action) for runtime, action, _ in runner.controls],
                [("localai", "unload"), ("comfyui", "unload"), ("voicebox", "load")],
            )
            self.assertFalse(runner.submitted)
            self.assertEqual(fake.job["state"], "failed")
            self.assertEqual(fake.job["stage"], "runtime_prepare_failed")
            self.assertEqual(fake.job["failure_category"], "runtime_prepare_failed")
            self.assertIn("model_missing", fake.job["failure_message"])
            self.assertEqual(fake.runtime_states["voicebox"]["status"], "load_failed")
            self.assertEqual(fake.runtime_states["voicebox"]["stage"], "loading")
            self.assertEqual(fake.job["artifacts"], [])

    def test_gpu_runner_calls_runtime_smoke_hook_and_records_state(self) -> None:
        fake = FakeDatabase(runtime="localai")
        self.patch_database(fake)

        class SmokeRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_urls={"localai": "http://localai"})
                self.controls: list[tuple[str, str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action, payload))
                return {"status": "ok", "action": action, "runtime": runtime, "measurements": {"peak_vram_mib": 4096}}

        with tempfile.TemporaryDirectory() as tmp:
            runner = SmokeRunner(Path(tmp))
            result = asyncio.run(runner.smoke_runtime_model(fake.job))

        self.assertEqual(result["status"], "ok")
        self.assertEqual([(runtime, action) for runtime, action, _ in runner.controls], [("localai", "smoke")])
        self.assertEqual(runner.controls[0][2]["job_id"], "job_gpu")
        self.assertEqual(runner.controls[0][2]["model"], "model")
        self.assertEqual(fake.runtime_states["localai"]["status"], "smoke_ok")
        self.assertEqual(fake.runtime_states["localai"]["stage"], "smoke")

    def test_gpu_runner_unloads_idle_runtime_after_default_timeout(self) -> None:
        fake = FakeDatabase(runtime="localai", claim_job=False)
        fake.runtime_states["localai"] = {
            "runtime": "localai",
            "status": "idle",
            "stage": "idle",
            "active_model": "image-model",
            "model_alias": "image-default",
            "resolved_model_version": "image-model@1",
            "job_id": "job_old",
            "details": {},
            "updated_at": datetime.now(tz=UTC) - timedelta(seconds=61),
        }
        self.patch_database(fake)

        class IdleRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    default_idle_timeout_seconds=60,
                )
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "service": "localai", "action": "unload"}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return {"gpu": {"available": True, "devices": [{"memory_used_mib": 0}]}}

        with tempfile.TemporaryDirectory() as tmp:
            runner = IdleRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertEqual(fake.claims[0]["runtime_names"], executor.GPU_RUNTIMES)
        self.assertEqual([path for path, _ in runner.posts], ["/v1/runtime-actions/localai/unload"])
        self.assertIn("idle timeout expired", runner.posts[0][1]["reason"])
        self.assertEqual(fake.runtime_states["localai"]["status"], "unload_ok")
        self.assertEqual(fake.runtime_states["localai"]["stage"], "idle_unloaded")
        self.assertIsNone(fake.runtime_states["localai"]["active_model"])
        self.assertIsNone(fake.runtime_states["localai"]["model_alias"])
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_survives_failed_idle_unload_hook(self) -> None:
        fake = FakeDatabase(runtime="lan-p40-media", claim_job=False)
        fake.runtime_states["lan-p40-media"] = {
            "runtime": "lan-p40-media",
            "status": "idle",
            "stage": "idle",
            "active_model": "talking-head-lipsync",
            "model_alias": "talking-head-lipsync",
            "resolved_model_version": "musetalk@1",
            "job_id": "job_old",
            "details": {},
            "updated_at": datetime.now(tz=UTC) - timedelta(seconds=61),
        }
        self.patch_database(fake)

        class FailingIdleRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_urls={"lan-p40-media": "https://worker.example"},
                    default_idle_timeout_seconds=60,
                )

            async def graceful_or_forced_unload_runtime(self, *args, **kwargs):
                raise RuntimeError("runtime recover hook returned HTTP 503")

        with tempfile.TemporaryDirectory() as tmp:
            runner = FailingIdleRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        state = fake.runtime_states["lan-p40-media"]
        self.assertEqual(state["status"], "idle_unload_failed")
        self.assertEqual(state["stage"], "idle_unload_failed")
        self.assertEqual(state["active_model"], "talking-head-lipsync")
        self.assertIn("HTTP 503", state["details"]["error"])
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_uses_alias_idle_timeout_override(self) -> None:
        fake = FakeDatabase(runtime="localai", claim_job=False)
        fake.alias_policies["image-default"] = {"alias": "image-default", "idle_timeout_seconds": 120}
        fake.runtime_states["localai"] = {
            "runtime": "localai",
            "status": "idle",
            "stage": "idle",
            "active_model": "image-model",
            "model_alias": "image-default",
            "resolved_model_version": "image-model@1",
            "job_id": "job_old",
            "details": {},
            "updated_at": datetime.now(tz=UTC) - timedelta(seconds=90),
        }
        self.patch_database(fake)

        class IdleRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    default_idle_timeout_seconds=60,
                )

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                raise AssertionError("alias override should keep the model resident until 120 seconds")

        with tempfile.TemporaryDirectory() as tmp:
            runner = IdleRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(fake.releases, [])
        self.assertEqual(fake.runtime_states["localai"]["active_model"], "image-model")

    def test_gpu_runner_revalidates_idle_candidate_after_acquiring_lease(self) -> None:
        fake = FakeDatabase(runtime="localai", claim_job=False)
        fake.runtime_states["localai"] = {
            "runtime": "localai",
            "status": "idle",
            "stage": "idle",
            "active_model": "image-model",
            "model_alias": "image-default",
            "resolved_model_version": "image-model@1",
            "job_id": "job_old",
            "details": {},
            "updated_at": datetime.now(tz=UTC) - timedelta(seconds=61),
        }
        original_acquire = fake.acquire_scheduler_owner

        async def acquire_after_interactive_refresh(owner: str, ttl_seconds: int) -> dict[str, Any]:
            # Simulate an interactive request that held the lease, reloaded the
            # model, and marked it idle immediately before the runner acquired.
            fake.runtime_states["localai"]["updated_at"] = datetime.now(tz=UTC)
            return await original_acquire(owner, ttl_seconds)

        fake.acquire_scheduler_owner = acquire_after_interactive_refresh  # type: ignore[method-assign]
        self.patch_database(fake)

        class IdleRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    default_idle_timeout_seconds=60,
                )
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "service": "localai", "action": "unload"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = IdleRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertFalse(processed)
        self.assertEqual(runner.posts, [])
        self.assertEqual(fake.runtime_states["localai"]["active_model"], "image-model")
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_submits_comfyui_prompt_and_ingests_history_outputs(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        fake.job["request_params"] = {
            "input": {
                "comfyui_prompt": {
                    "prompt": {
                        "1": {
                            "class_type": "SaveImage",
                            "inputs": {"filename_prefix": "{{prompt}}"},
                        }
                    }
                },
                "parameters": {"prompt": "b1-test"},
            }
        }
        self.patch_database(fake)

        class ComfyRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    interval_seconds=1,
                    lease_ttl_seconds=60,
                    runtime_urls={"comfyui": "http://comfyui"},
                    comfyui_poll_seconds=1,
                    comfyui_completion_timeout_seconds=30,
                )
                self.payloads: list[dict[str, Any]] = []

            async def submit_comfyui_prompt(self, job: dict[str, Any], payload: dict[str, Any]) -> str:
                self.payloads.append(payload)
                return "prompt_native_1"

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def fetch_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
                return {
                    prompt_id: {
                        "status": {"completed": True},
                        "outputs": {
                            "save": {
                                "images": [
                                    {"filename": "result.png", "subfolder": "b1", "type": "output"},
                                ]
                            }
                        },
                    }
                }

            async def ingest_comfyui_artifacts(self, job: dict[str, Any], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    {
                        **artifact,
                        "source": "artifact_store",
                        "storage": "artifact-server",
                        "bytes": 11,
                        "sha256": "a" * 64,
                        "ingest_status": "stored",
                    }
                    for artifact in artifacts
                ]

        with tempfile.TemporaryDirectory() as tmp:
            runner = ComfyRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(runner.payloads[0]["client_id"], "job_gpu")
            self.assertEqual(runner.payloads[0]["prompt"]["1"]["inputs"]["filename_prefix"], "b1-test")
            self.assertEqual(runner.payloads[0]["extra_data"]["b1"]["job_id"], "job_gpu")
            self.assertEqual(fake.job["native_prompt_id"], "prompt_native_1")
            self.assertEqual(fake.job["state"], "completed")
            self.assertEqual(fake.job["artifacts"][0]["url"], "/artifacts/comfyui/prompt_native_1/0-result.png")
            self.assertEqual(fake.job["artifacts"][0]["source"], "artifact_store")
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())
            self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_routes_talking_head_lipsync_to_musetalk_path(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        audio = pcm_wav_bytes(duration_ms=500)
        audio_sha256 = hashlib.sha256(audio).hexdigest()

        class LipsyncRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    interval_seconds=1,
                    lease_ttl_seconds=60,
                    runtime_urls={"lipsync": "http://lipsync"},
                )
                self.generic_comfyui_called = False
                self.lipsync_payloads: list[dict[str, Any]] = []
                self.controls: list[tuple[str, str]] = []
                self.render_attempts = 0

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append((runtime, action))
                return {"status": "ok", "runtime": runtime, "action": action}

            async def run_comfyui_job(self, job: dict[str, Any]) -> None:
                self.generic_comfyui_called = True
                raise AssertionError("talking-head-lipsync must not use generic ComfyUI image/video animation")

            async def render_talking_head_lipsync_runtime(
                self,
                *,
                job: dict[str, Any],
                portrait: bytes,
                portrait_mime_type: str,
                audio: bytes,
                audio_sha256: str,
                timing_sha256: str,
                width: int,
                height: int,
                fps: int,
                duration_ms: int,
                payload: dict[str, Any],
            ) -> tuple[bytes, dict[str, Any]]:
                self.render_attempts += 1
                self.lipsync_payloads.append(
                    {
                        "portrait_mime_type": portrait_mime_type,
                        "audio_sha256": audio_sha256,
                        "timing_sha256": timing_sha256,
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "duration_ms": duration_ms,
                        "payload": dict(payload),
                    }
                )
                if self.render_attempts == 1:
                    raise executor.LipsyncRuntimeFailure(
                        "lipsync_cuda_out_of_memory",
                        "MuseTalk could not reserve enough CUDA memory",
                    )
                return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom", {
                    "backend": "b1-musetalk-v1.5",
                    "musetalk_commit": "test-commit",
                    "performance": {
                        "mode": "audio_driven_character_performance",
                        "applied": True,
                        "plan_sha256": "b" * 64,
                        "variation_seed": 2104,
                        "backend": "b1-musetalk-protected-performance-compositor/v1",
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            portrait_ref = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="portrait",
                content=PNG_BYTES,
                declared_mime_type="image/png",
            )
            audio_ref = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="audio",
                content=audio,
                declared_mime_type="audio/wav",
            )
            fake.job.update(
                {
                    "id": "job_lipsync",
                    "owner_id": "client_1",
                    "runtime": "comfyui",
                    "model_alias": "talking-head-lipsync",
                    "resolved_model_version": "tmelyralab-musetalk-v1.5-lipsync@0a89dec",
                    "modality": "video",
                    "operation": "talking-head-lipsync",
                    "request_params": {
                        "input": {
                            "portrait_artifact_id": portrait_ref["id"],
                            "audio_artifact_id": audio_ref["id"],
                            "audio_sha256": audio_sha256,
                            "width": 512,
                            "height": 512,
                            "fps": 12,
                            "duration_ms": 500,
                            "performance_plan": {
                                "schema_version": "dialecticore.character_performance.v1",
                                "on_camera_energy": "engaged",
                                "gaze_style": "responsive",
                                "head_motion": "subtle",
                                "expression_range": "warm",
                                "gesture_frequency": "occasional",
                                "signature_habit": "brief attentive head tilt",
                                "variation_seed": 2104,
                            },
                        }
                    },
                }
            )
            self.patch_database(fake)
            runner = LipsyncRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertFalse(runner.generic_comfyui_called)
            self.assertIn(("lipsync", "load"), runner.controls)
            self.assertIn(("lipsync", "warm"), runner.controls)
            self.assertIn(("lipsync", "unload"), runner.controls)
            self.assertNotIn(("comfyui", "load"), runner.controls)
            self.assertNotIn(("comfyui", "warm"), runner.controls)
            self.assertEqual(runner.render_attempts, 2)
            self.assertEqual(runner.lipsync_payloads[0]["audio_sha256"], audio_sha256)
            self.assertEqual(runner.lipsync_payloads[0]["timing_sha256"], executor.EMPTY_TIMING_SHA256)
            self.assertEqual(runner.lipsync_payloads[0]["width"], 512)
            self.assertEqual(runner.lipsync_payloads[0]["height"], 512)
            self.assertEqual(runner.lipsync_payloads[0]["fps"], 12)
            self.assertEqual(runner.lipsync_payloads[0]["duration_ms"], 500)
            self.assertEqual(runner.lipsync_payloads[0]["payload"]["performance_plan"]["variation_seed"], 2104)
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["mime_type"], "video/mp4")
            self.assertEqual(artifact["source"], "b1_lipsync_runtime")
            self.assertEqual(artifact["lip_sync"]["mode"], "audio_driven")
            self.assertEqual(artifact["lip_sync"]["backend"], "b1-musetalk-v1.5")
            self.assertEqual(artifact["lip_sync"]["audio_sha256"], audio_sha256)
            self.assertTrue(artifact["performance"]["applied"])
            self.assertEqual(artifact["performance"]["variation_seed"], 2104)

    def test_comfyui_native_merge_prefers_stored_artifact_metadata(self) -> None:
        existing = [
            {
                "url": "/artifacts/comfyui/prompt/0-result.png",
                "source": "comfyui_view",
                "ingest_status": "failed",
                "bytes": None,
            }
        ]
        stored = {
            "url": "/artifacts/comfyui/prompt/0-result.png",
            "source": "artifact_store",
            "ingest_status": "stored",
            "bytes": 12,
            "sha256": "a" * 64,
        }

        merged = executor.comfyui_native.merge_job_artifacts(existing, [stored])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "artifact_store")
        self.assertEqual(merged[0]["bytes"], 12)
        self.assertEqual(merged[0]["sha256"], "a" * 64)

    def test_comfyui_workflow_declares_staged_load_image_parameter(self) -> None:
        manifest = {
            "workflow_json": {
                "13": {"class_type": "LoadImage", "inputs": {"image": "{{source_image_filename}}"}},
                "14": {"class_type": "LoadImageMask", "inputs": {"image": "{{ignored_filename}}"}},
            }
        }
        self.assertEqual(executor.comfyui_native.workflow_comfyui_staged_upload_parameters(manifest), {"source_image"})

    def test_comfyui_workflow_declares_staged_load_video_parameter(self) -> None:
        manifest = {
            "workflow_json": {
                "10": {"class_type": "LoadVideo", "inputs": {"file": "{{source_video_filename}}"}},
                "11": {"class_type": "GetVideoComponents", "inputs": {"video": ["10", 0]}},
            }
        }
        self.assertEqual(
            executor.comfyui_native.workflow_comfyui_staged_upload_specs(manifest),
            {"source_video": "video"},
        )
        self.assertEqual(executor.comfyui_native.workflow_comfyui_staged_upload_parameters(manifest), {"source_video"})

    def test_gpu_runner_renders_published_workflow_parameter_mappings_for_comfyui(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        fake.workflow = {
            "id": "mapped-workflow",
            "version": "1.0.0",
            "manifest": {
                "id": "mapped-workflow",
                "version": "1.0.0",
                "workflow_json": {
                    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                    "3": {"class_type": "KSampler", "inputs": {"steps": 20}},
                },
                "comfyui_parameter_mappings": [
                    {"parameter": "prompt", "path": ["6", "inputs", "text"]},
                    {"parameter": "steps", "path": ["3", "inputs", "steps"]},
                ],
            },
        }
        fake.job["request_params"] = {
            "input": {
                "workflow_id": "mapped-workflow",
                "workflow_version": "1.0.0",
                "parameters": {"prompt": "mapped prompt", "steps": 12},
            }
        }
        self.patch_database(fake)

        class ComfyRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    interval_seconds=1,
                    lease_ttl_seconds=60,
                    runtime_urls={"comfyui": "http://comfyui"},
                    comfyui_poll_seconds=1,
                    comfyui_completion_timeout_seconds=30,
                )
                self.payloads: list[dict[str, Any]] = []

            async def submit_comfyui_prompt(self, job: dict[str, Any], payload: dict[str, Any]) -> str:
                self.payloads.append(payload)
                return "prompt_mapped"

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def fetch_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
                return {
                    prompt_id: {
                        "status": {"completed": True},
                        "outputs": {
                            "save": {
                                "images": [
                                    {"filename": "mapped.png", "subfolder": "", "type": "output"},
                                ]
                            }
                        },
                    }
                }

            async def ingest_comfyui_artifacts(self, job: dict[str, Any], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    {
                        **artifact,
                        "source": "artifact_store",
                        "storage": "artifact-server",
                        "bytes": 9,
                        "sha256": "b" * 64,
                        "ingest_status": "stored",
                    }
                    for artifact in artifacts
                ]

        with tempfile.TemporaryDirectory() as tmp:
            runner = ComfyRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            prompt = runner.payloads[0]["prompt"]
            self.assertEqual(prompt["6"]["inputs"]["text"], "mapped prompt")
            self.assertEqual(prompt["3"]["inputs"]["steps"], 12)
            self.assertEqual(fake.job["native_prompt_id"], "prompt_mapped")
            self.assertEqual(fake.job["state"], "completed")
            self.assertEqual(fake.job["artifacts"][0]["url"], "/artifacts/comfyui/prompt_mapped/0-mapped.png")

    def test_gpu_runner_marks_comfyui_job_recovery_required_when_no_media_outputs(self) -> None:
        fake = FakeDatabase(runtime="comfyui")
        fake.job["request_params"] = {
            "input": {
                "comfyui_prompt": {
                    "prompt": {
                        "1": {
                            "class_type": "PreviewOnly",
                            "inputs": {"text": "no saved output"},
                        }
                    }
                },
            }
        }
        self.patch_database(fake)

        class ComfyRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    interval_seconds=1,
                    lease_ttl_seconds=60,
                    runtime_urls={"comfyui": "http://comfyui"},
                    comfyui_poll_seconds=1,
                    comfyui_completion_timeout_seconds=30,
                )

            async def submit_comfyui_prompt(self, job: dict[str, Any], payload: dict[str, Any]) -> str:
                return "prompt_empty"

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def fetch_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
                return {prompt_id: {"status": {"completed": True}, "outputs": {}}}

            async def ingest_comfyui_artifacts(self, job: dict[str, Any], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
                raise AssertionError("empty ComfyUI outputs should not enter artifact ingestion")

        with tempfile.TemporaryDirectory() as tmp:
            runner = ComfyRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["native_prompt_id"], "prompt_empty")
            self.assertEqual(fake.job["state"], "recovery_required")
            self.assertEqual(fake.job["stage"], "comfyui_no_media_artifacts")
            self.assertEqual(fake.job["failure_category"], "comfyui_no_media_artifacts")
            self.assertEqual(fake.job["artifacts"], [])
            self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_cancels_blocking_comfyui_prompt_submission_and_recovers_runtime(self) -> None:
        class CancellingDatabase(FakeDatabase):
            async def get_job(self, job_id: str) -> dict[str, Any]:
                if self.job.get("stage") == "comfyui_submitting" and self.job.get("state") == "running":
                    self.job["state"] = "cancelling"
                return dict(self.job)

        fake = CancellingDatabase(runtime="comfyui")
        fake.job["request_params"] = {
            "input": {
                "comfyui_prompt": {
                    "prompt": {
                        "1": {
                            "class_type": "KSampler",
                            "inputs": {"seed": 7},
                        }
                    }
                },
            }
        }
        self.patch_database(fake)

        class ComfyCancelRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    runtime_urls={"comfyui": "http://comfyui"},
                    runtime_cancel_poll_seconds=0.05,
                )
                self.posts: list[tuple[str, dict[str, Any]]] = []
                self.runtime_call_cancelled = False
                self.interrupts = 0

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return None

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "action": path.rsplit("/", 1)[-1]}

            async def interrupt_comfyui(self, job: dict[str, Any]) -> None:
                self.interrupts += 1

            async def submit_comfyui_prompt(self, job: dict[str, Any], payload: dict[str, Any]) -> str:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.runtime_call_cancelled = True
                    raise
                raise AssertionError("ComfyUI prompt submission should be cancelled before it returns")

        with tempfile.TemporaryDirectory() as tmp:
            runner = ComfyCancelRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertTrue(runner.runtime_call_cancelled)
        self.assertEqual(runner.interrupts, 1)
        self.assertEqual(fake.job["state"], "cancelled")
        self.assertEqual(fake.job["stage"], "cancelled")
        self.assertEqual(fake.job["artifacts"], [])
        self.assertIn("/v1/runtime-actions/comfyui/recover", [path for path, _ in runner.posts])
        self.assertIn(
            ("comfyui", "recover_ok", "cancel_recovery"),
            [(row["runtime"], row["status"], row["stage"]) for row in fake.runtime_state_updates],
        )
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_cancels_active_p40_comfyui_history_wait_and_recovers_runtime(self) -> None:
        class CancellingDatabase(FakeDatabase):
            async def get_job(self, job_id: str) -> dict[str, Any]:
                self.job["state"] = "cancelling"
                return dict(self.job)

        fake = CancellingDatabase(runtime="lan-p40-media")
        fake.job.update({"state": "running", "stage": "comfyui_waiting_history"})
        self.patch_database(fake)

        class P40CancelRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_urls={"lan-p40-media": "https://p40-worker.example/internal/media/comfyui"},
                    comfyui_poll_seconds=0.05,
                )
                self.controls: list[str] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.controls.append(action)
                return {"status": "ok", "runtime": runtime, "action": action}

            async def interrupt_comfyui(self, job: dict[str, Any]) -> None:
                self.controls.append("interrupt")

            async def fetch_comfyui_history(self, job: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
                raise AssertionError("cancel must be observed before another history request")

        with tempfile.TemporaryDirectory() as tmp:
            runner = P40CancelRunner(Path(tmp))
            history = asyncio.run(runner.wait_for_comfyui_history(fake.job, "prompt_active"))

        self.assertIsNone(history)
        self.assertEqual(fake.job["state"], "cancelled")
        self.assertEqual(fake.job["stage"], "cancelled")
        self.assertEqual(runner.controls, ["interrupt", "cancel", "recover"])
        self.assertIn(
            ("lan-p40-media", "recover_ok", "cancel_recovery"),
            [(row["runtime"], row["status"], row["stage"]) for row in fake.runtime_state_updates],
        )

    def test_gpu_runner_submits_localai_image_generation_and_stores_b64_artifact(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job["request_params"] = {
            "input": {
                "model": "image-default",
                "prompt": "make image",
                "size": "512x512",
                "parameters": {"guidance_scale": 7},
                "runtime_policy": "any",
            }
        }
        self.patch_database(fake)

        class LocalAIRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.posts.append((endpoint, payload))
                return {"data": [{"b64_json": base64.b64encode(b"image-bytes").decode("ascii"), "mime_type": "image/png"}]}

        with tempfile.TemporaryDirectory() as tmp:
            runner = LocalAIRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(runner.posts, [("/v1/images/generations", {"model": "model", "prompt": "make image", "size": "512x512", "guidance_scale": 7})])
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "localai")
            self.assertEqual(artifact["source"], "localai_openai")
            self.assertEqual(artifact["kind"], "image")
            self.assertEqual(artifact["mime_type"], "image/png")
            self.assertEqual(artifact["sha256"], hashlib.sha256(b"image-bytes").hexdigest())
            self.assertEqual((Path(tmp) / "localai" / "job_gpu" / "0.png").read_bytes(), b"image-bytes")
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())

    def test_gpu_runner_downloads_same_origin_localai_image_url(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job["request_params"] = {"input": {"prompt": "url image"}}
        self.patch_database(fake)

        class LocalAIRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai:8000"})
                self.downloaded: list[str] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"data": [{"url": "/generated/job_gpu.png"}]}

            async def fetch_localai_artifact_url(self, url: str, default_mime_type: str = "application/octet-stream") -> tuple[bytes, str]:
                self.downloaded.append(url)
                return b"url-image", "image/webp"

        with tempfile.TemporaryDirectory() as tmp:
            runner = LocalAIRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(runner.downloaded, ["/generated/job_gpu.png"])
            self.assertEqual(fake.job["state"], "completed")
            self.assertEqual(fake.job["artifacts"][0]["mime_type"], "image/webp")
            self.assertEqual((Path(tmp) / "localai" / "job_gpu" / "0.webp").read_bytes(), b"url-image")

    def test_gpu_runner_submits_localai_text_to_video_and_stores_b64_artifact(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job.update(
            {
                "model_alias": "video-text",
                "resolved_model_version": "video-model@1.0.0",
                "modality": "video",
                "operation": "generation",
                "request_params": {
                    "input": {
                        "model": "video-text",
                        "prompt": "make short clip",
                        "parameters": {"frames": 12, "duration_seconds": 2},
                        "runtime_policy": "any",
                    }
                },
            }
        )
        self.patch_database(fake)

        class LocalAIVideoRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.posts.append((endpoint, payload))
                return {"data": [{"b64_json": base64.b64encode(b"video-bytes").decode("ascii"), "mime_type": "video/mp4"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = LocalAIVideoRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                runner.posts,
                [("/v1/videos/generations", {"model": "video-model", "prompt": "make short clip", "frames": 12, "duration_seconds": 2})],
            )
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "localai")
            self.assertEqual(artifact["kind"], "video")
            self.assertEqual(artifact["mime_type"], "video/mp4")
            self.assertEqual(artifact["sha256"], hashlib.sha256(b"video-bytes").hexdigest())
            self.assertEqual((root / "localai" / "job_gpu" / "0.mp4").read_bytes(), b"video-bytes")

    def test_gpu_runner_submits_localai_image_to_video_with_staged_upload(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.workflow = {
            "id": "localai-image-video",
            "version": "1.0.0",
            "manifest": {
                "id": "localai-image-video",
                "version": "1.0.0",
                "runtime_parameter_mappings": [
                    {"runtime": "localai", "parameter": "source_image", "target": "image"},
                ],
            },
        }
        fake.job.update(
            {
                "runtime": "localai",
                "model_alias": "video-image",
                "resolved_model_version": "image-video-model@1.0.0",
                "modality": "video",
                "operation": "image-to-video",
            }
        )
        self.patch_database(fake)

        class LocalAIImageVideoRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})
                self.multipart_posts: list[tuple[str, dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_multipart(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                data, files = self.localai_multipart_payload_for_job(payload)
                self.multipart_posts.append((endpoint, data, files))
                encoded = base64.b64encode(b"webm-video").decode("ascii")
                return {"data": [{"url": f"data:video/webm;base64,{encoded}"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="source_image",
                filename="source.png",
                declared_mime_type="image/png",
                content=PNG_BYTES,
            )
            fake.job["request_params"] = {
                "input": {
                    "workflow_id": "localai-image-video",
                    "workflow_version": "1.0.0",
                    "parameters": {
                        "source_image": source,
                        "prompt": "animate the image",
                        "frames": 8,
                    },
                }
            }
            runner = LocalAIImageVideoRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            endpoint, data, files = runner.multipart_posts[0]
            self.assertEqual(endpoint, "/v1/videos/image-to-video")
            self.assertEqual(data, {"model": "image-video-model", "prompt": "animate the image", "frames": "8"})
            self.assertEqual(files, [("image", ("source.png", PNG_BYTES, "image/png"))])
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["kind"], "video")
            self.assertEqual(artifact["mime_type"], "video/webm")
            self.assertEqual((root / "localai" / "job_gpu" / "0.webm").read_bytes(), b"webm-video")

    def test_gpu_runner_submits_localai_image_edit_with_staged_upload(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job["operation"] = "edit"
        self.patch_database(fake)

        class LocalAIEditRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})
                self.multipart_posts: list[tuple[str, dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_multipart(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                data, files = self.localai_multipart_payload_for_job(payload)
                self.multipart_posts.append((endpoint, data, files))
                return {"data": [{"b64_json": base64.b64encode(b"edited-image").decode("ascii"), "mime_type": "image/png"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="image",
                filename="input.png",
                declared_mime_type="image/png",
                content=PNG_BYTES,
            )
            fake.job["request_params"] = {
                "input": {
                    "model": "image-edit",
                    "prompt": "replace background",
                    "image": staged,
                    "parameters": {"size": "512x512"},
                }
            }
            runner = LocalAIEditRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            endpoint, data, files = runner.multipart_posts[0]
            self.assertEqual(endpoint, "/v1/images/edits")
            self.assertEqual(data, {"model": "model", "prompt": "replace background", "size": "512x512"})
            self.assertEqual(files, [("image", ("input.png", PNG_BYTES, "image/png"))])
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "localai")
            self.assertEqual(artifact["operation"], "edit")
            self.assertEqual((root / "localai" / "job_gpu" / "0.png").read_bytes(), b"edited-image")

    def test_gpu_runner_applies_workflow_runtime_parameter_mappings_for_localai_edit(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.workflow = {
            "id": "localai-edit-workflow",
            "version": "1.0.0",
            "manifest": {
                "id": "localai-edit-workflow",
                "version": "1.0.0",
                "runtime_parameter_mappings": [
                    {"runtime": "localai", "parameter": "source_image", "target": "image"},
                    {"runtime": "localai", "parameter": "mask_image", "target": "mask"},
                ],
            },
        }
        fake.job.update({"runtime": "localai", "model_alias": "image-edit", "operation": "edit"})
        self.patch_database(fake)

        class LocalAIEditRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})
                self.multipart_posts: list[tuple[str, dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_multipart(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                data, files = self.localai_multipart_payload_for_job(payload)
                self.multipart_posts.append((endpoint, data, files))
                return {"data": [{"b64_json": base64.b64encode(b"mapped-edit").decode("ascii"), "mime_type": "image/png"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="source_image",
                filename="source.png",
                declared_mime_type="image/png",
                content=PNG_BYTES,
            )
            mask = executor.media_artifacts.write_staged_input_bytes(
                root,
                owner_id="client_1",
                field_name="mask_image",
                filename="mask.png",
                declared_mime_type="image/png",
                content=PNG_BYTES,
            )
            fake.job["request_params"] = {
                "input": {
                    "workflow_id": "localai-edit-workflow",
                    "workflow_version": "1.0.0",
                    "parameters": {
                        "source_image": source,
                        "mask_image": mask,
                        "prompt": "fill the window",
                        "strength": 0.55,
                    },
                }
            }
            runner = LocalAIEditRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            endpoint, data, files = runner.multipart_posts[0]
            self.assertEqual(endpoint, "/v1/images/edits")
            self.assertEqual(data, {"model": "model", "prompt": "fill the window", "strength": "0.55"})
            self.assertEqual(
                files,
                [
                    ("image", ("source.png", PNG_BYTES, "image/png")),
                    ("mask", ("mask.png", PNG_BYTES, "image/png")),
                ],
            )
            self.assertEqual(fake.job["state"], "completed")
            self.assertEqual((root / "localai" / "job_gpu" / "0.png").read_bytes(), b"mapped-edit")

    def test_gpu_runner_submits_voicebox_speech_and_stores_audio_artifact(self) -> None:
        fake = FakeDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "hello", "voice": "sample", "runtime_policy": "any"}},
            }
        )
        self.patch_database(fake)

        class VoiceboxRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"voicebox": "http://voicebox"})
                self.payloads: list[dict[str, Any]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                self.payloads.append(payload)
                return b"voicebox-wav", "audio/wav"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = VoiceboxRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(runner.payloads, [{"model": "voicebox-quality", "text": "hello", "voice": "sample"}])
            self.assertEqual(fake.job["state"], "completed")
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["runtime"], "voicebox")
            self.assertEqual(artifact["source"], "voicebox_runtime")
            self.assertEqual(artifact["kind"], "audio")
            self.assertEqual(artifact["mime_type"], "audio/wav")
            self.assertEqual(artifact["sha256"], hashlib.sha256(b"voicebox-wav").hexdigest())
            self.assertEqual((root / "voicebox" / "job_gpu" / "0.wav").read_bytes(), b"voicebox-wav")

    def test_gpu_runner_resolves_voicebox_profile_before_speech_submission(self) -> None:
        fake = FakeDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {
                    "input": {
                        "text": "hello",
                        "voice_profile_id": "vp_narrator",
                        "runtime_policy": "any",
                        "parameters": {"speed": 0.9},
                    }
                },
            }
        )
        fake.voice_profiles["vp_narrator"] = {
            "id": "vp_narrator",
            "owner_id": "client_1",
            "runtime": "voicebox",
            "engine": "chatterbox",
            "model_alias": "tts-quality",
            "profile_type": "clone",
            "status": "active",
            "visibility_roles": ["user"],
            "metadata": {"upstream_voice": "native-narrator", "notes": "not forwarded"},
            "sample_artifacts": [
                {"url": "/artifacts/voicebox/references/narrator.wav", "sha256": "b" * 64, "mime_type": "audio/wav", "bytes": 456}
            ],
        }
        self.patch_database(fake)

        class VoiceboxRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"voicebox": "http://voicebox"})
                self.payloads: list[dict[str, Any]] = []

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                self.payloads.append(payload)
                return b"voicebox-wav", "audio/wav"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = VoiceboxRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            payload = runner.payloads[0]
            self.assertEqual(payload["model"], "voicebox-quality")
            self.assertEqual(payload["voice"], "native-narrator")
            self.assertEqual(payload["speed"], 0.9)
            self.assertEqual(payload["b1_voice_profile"]["id"], "vp_narrator")
            self.assertEqual(payload["b1_voice_profile"]["engine"], "chatterbox")
            self.assertEqual(payload["b1_voice_profile"]["sample_artifacts"][0]["url"], "/artifacts/voicebox/references/narrator.wav")
            self.assertNotIn("notes", json.dumps(payload, sort_keys=True))
            artifact = fake.job["artifacts"][0]
            self.assertEqual(artifact["voice_profile_id"], "vp_narrator")
            self.assertEqual((root / "voicebox" / "job_gpu" / "0.wav").read_bytes(), b"voicebox-wav")

    def test_gpu_runner_marks_voicebox_speech_recovery_required_when_empty(self) -> None:
        fake = FakeDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "hello"}},
            }
        )
        self.patch_database(fake)

        class VoiceboxRunner(executor.GpuJobRunner):
            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                return b"", "audio/wav"

        with tempfile.TemporaryDirectory() as tmp:
            runner = VoiceboxRunner(Path(tmp), runtime_urls={"voicebox": "http://voicebox"})
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["state"], "recovery_required")
            self.assertEqual(fake.job["failure_category"], "voicebox_empty_speech")
            self.assertEqual(fake.job["artifacts"], [])

    def test_gpu_runner_cancels_blocking_voicebox_call_and_recovers_runtime(self) -> None:
        class CancellingDatabase(FakeDatabase):
            async def get_job(self, job_id: str) -> dict[str, Any]:
                if self.job.get("stage") == "voicebox_speech" and self.job.get("state") == "running":
                    self.job["state"] = "cancelling"
                return dict(self.job)

        fake = CancellingDatabase(runtime="voicebox")
        fake.job.update(
            {
                "model_alias": "tts-quality",
                "resolved_model_version": "voicebox-quality@1.0.0",
                "modality": "tts",
                "operation": "speech",
                "request_params": {"input": {"text": "cancel me"}},
            }
        )
        self.patch_database(fake)

        class VoiceboxCancelRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    runtime_urls={"voicebox": "http://voicebox"},
                    runtime_cancel_poll_seconds=0.05,
                )
                self.posts: list[tuple[str, dict[str, Any]]] = []
                self.runtime_call_cancelled = False

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return None

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "action": path.rsplit("/", 1)[-1]}

            async def post_voicebox_speech(self, payload: dict[str, Any]) -> tuple[bytes, str]:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.runtime_call_cancelled = True
                    raise
                raise AssertionError("runtime call should be cancelled before it returns")

        with tempfile.TemporaryDirectory() as tmp:
            runner = VoiceboxCancelRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertTrue(runner.runtime_call_cancelled)
        self.assertEqual(fake.job["state"], "cancelled")
        self.assertEqual(fake.job["stage"], "cancelled")
        self.assertEqual(fake.job["artifacts"], [])
        self.assertIn("/v1/runtime-actions/voicebox/recover", [path for path, _ in runner.posts])
        self.assertIn(
            ("voicebox", "recover_ok", "cancel_recovery"),
            [(row["runtime"], row["status"], row["stage"]) for row in fake.runtime_state_updates],
        )
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_marks_localai_job_recovery_required_when_no_media_is_returned(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job["request_params"] = {"input": {"prompt": "no artifact"}}
        self.patch_database(fake)

        class LocalAIRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, interval_seconds=1, lease_ttl_seconds=60, runtime_urls={"localai": "http://localai"})

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"data": [{"revised_prompt": "no image bytes"}]}

        with tempfile.TemporaryDirectory() as tmp:
            runner = LocalAIRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["state"], "recovery_required")
            self.assertEqual(fake.job["failure_category"], "localai_no_media_artifacts")
            self.assertEqual(fake.job["artifacts"], [])
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())

    def test_gpu_runner_cancels_blocking_localai_media_call_and_recovers_runtime(self) -> None:
        class CancellingDatabase(FakeDatabase):
            async def get_job(self, job_id: str) -> dict[str, Any]:
                if self.job.get("stage") == "localai_submitting" and self.job.get("state") == "running":
                    self.job["state"] = "cancelling"
                return dict(self.job)

        fake = CancellingDatabase(runtime="localai")
        fake.job["request_params"] = {"input": {"prompt": "cancel me"}}
        self.patch_database(fake)

        class LocalAICancelRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(
                    artifact_root,
                    runtime_agent_url="http://runtime-agent",
                    runtime_urls={"localai": "http://localai"},
                    runtime_cancel_poll_seconds=0.05,
                )
                self.posts: list[tuple[str, dict[str, Any]]] = []
                self.runtime_call_cancelled = False

            async def post_runtime_control(self, runtime: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"status": "ok", "runtime": runtime, "action": action}

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                return None

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "ok", "action": path.rsplit("/", 1)[-1]}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.runtime_call_cancelled = True
                    raise
                raise AssertionError("runtime call should be cancelled before it returns")

        with tempfile.TemporaryDirectory() as tmp:
            runner = LocalAICancelRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

        self.assertTrue(processed)
        self.assertTrue(runner.runtime_call_cancelled)
        self.assertEqual(fake.job["state"], "cancelled")
        self.assertEqual(fake.job["stage"], "cancelled")
        self.assertEqual(fake.job["artifacts"], [])
        self.assertIn("/v1/runtime-actions/localai/recover", [path for path, _ in runner.posts])
        self.assertIn(
            ("localai", "recover_ok", "cancel_recovery"),
            [(row["runtime"], row["status"], row["stage"]) for row in fake.runtime_state_updates],
        )
        self.assertEqual(fake.releases, [runner.lease_owner])

    def test_gpu_runner_leaves_waiting_job_when_lease_is_held(self) -> None:
        fake = FakeDatabase(lease_acquired=False)
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp), interval_seconds=1, lease_ttl_seconds=60)
            processed = asyncio.run(runner.run_once())

            self.assertFalse(processed)
            self.assertEqual([item["state"] for item in fake.updates if "state" in item], ["waiting_for_gpu"])
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())

    def test_gpu_runner_reconciles_interrupted_gpu_states(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            result = asyncio.run(runner.reconcile_startup())
            self.assertEqual(result["marked_recovery_required"], 2)
            self.assertEqual(result["requeued"], 1)
            self.assertEqual(result["recovery_required_job_ids"], ["job_active_1", "job_active_2"])
            self.assertEqual(result["requeued_job_ids"], ["job_waiting_1"])

    def test_gpu_runner_records_startup_reconciliation(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.GpuJobRunner(Path(tmp))
            result = asyncio.run(executor.record_runner_startup_reconciliation(runner, executor.GPU_RUNTIMES))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime_names"], executor.GPU_RUNTIMES)
        self.assertEqual(result["marked_recovery_required"], 2)
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(result["recovery_required_job_ids"], ["job_active_1", "job_active_2"])
        self.assertEqual(result["requeued_job_ids"], ["job_waiting_1"])
        self.assertTrue(result["started_at"])
        self.assertTrue(result["completed_at"])
        self.assertEqual(runner.startup_reconciliation, result)

    def test_model_download_runner_records_startup_reconciliation(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.ModelDownloadRunner(Path(tmp))
            result = asyncio.run(executor.record_runner_startup_reconciliation(runner, executor.MODEL_DOWNLOAD_RUNTIMES))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime_names"], ["model-download"])
        self.assertEqual(result["marked_recovery_required"], 0)
        self.assertEqual(result["requeued"], 3)
        self.assertEqual(result["paused"], 1)
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(result["requeued_download_ids"], ["modeldl_running_1", "modeldl_running_2", "modeldl_running_3"])
        self.assertEqual(result["paused_download_ids"], ["modeldl_pausing_1"])
        self.assertEqual(result["cancelled_download_ids"], ["modeldl_cancelling_1", "modeldl_cancelling_2"])
        self.assertTrue(result["started_at"])
        self.assertTrue(result["completed_at"])
        self.assertEqual(runner.startup_reconciliation, result)

    def test_cpu_runner_reconciles_waiting_claims_without_requeueing_recovery_required(self) -> None:
        fake = FakeDatabase(runtime="audio-cpu")
        fake.waiting_requeues = 3
        self.patch_database(fake)
        with tempfile.TemporaryDirectory() as tmp:
            runner = executor.CpuJobRunner(Path(tmp))
            result = asyncio.run(runner.reconcile_startup())

        self.assertEqual(result["marked_recovery_required"], 2)
        self.assertEqual(result["requeued"], 3)
        self.assertEqual(result["recovery_required_job_ids"], ["job_active_1", "job_active_2"])
        self.assertEqual(result["requeued_job_ids"], ["job_waiting_1", "job_waiting_2", "job_waiting_3"])

    def test_gpu_runner_recovers_when_vram_exceeds_reserve(self) -> None:
        fake = FakeDatabase(runtime="localai")
        fake.job["request_params"] = {"input": {"prompt": "image after recovery"}}
        self.patch_database(fake)

        class MetricsRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_agent_url="http://runtime-agent", reserve_vram_gib=1.5)
                self.metrics = [
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 512}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 512}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 4096}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 512}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 1024}]}},
                ]
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                if len(self.metrics) > 1:
                    return self.metrics.pop(0)
                return self.metrics[0]

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                self.posts.append((path, payload))
                return {"status": "dry_run", "action": "recover"}

            async def post_localai_media_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {"data": [{"b64_json": base64.b64encode(b"image-after-recovery").decode("ascii"), "mime_type": "image/png"}]}

        with tempfile.TemporaryDirectory() as tmp:
            runner = MetricsRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(
                [path for path, _ in runner.posts],
                [
                    "/v1/runtime-actions/comfyui/unload",
                    "/v1/runtime-actions/voicebox/unload",
                    "/v1/runtime-actions/localai/recover",
                ],
            )
            stages = [item["stage"] for item in fake.updates if "stage" in item]
            self.assertIn("verifying_vram_recovering", stages)
            self.assertIn("verifying_vram_recovered", stages)
            self.assertEqual(fake.job["state"], "completed")
            self.assertEqual(fake.job["peak_vram_mib"], 4096)
            self.assertIsNone(fake.job.get("failure_category"))

    def test_gpu_runner_fails_when_vram_remains_above_reserve_after_recovery(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)

        class MetricsRunner(executor.GpuJobRunner):
            def __init__(self, artifact_root: Path) -> None:
                super().__init__(artifact_root, runtime_agent_url="http://runtime-agent", reserve_vram_gib=1.5)
                self.metrics = [
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 4096}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 3072}]}},
                    {"gpu": {"available": True, "devices": [{"memory_used_mib": 3072}]}},
                ]

            async def runtime_agent_get(self, path: str) -> dict[str, Any] | None:
                if len(self.metrics) > 1:
                    return self.metrics.pop(0)
                return self.metrics[0]

            async def runtime_agent_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
                return {"status": "dry_run", "action": "recover"}

        with tempfile.TemporaryDirectory() as tmp:
            runner = MetricsRunner(Path(tmp))
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.job["state"], "failed")
            self.assertEqual(fake.job["failure_category"], "gpu_runner_error")
            self.assertEqual(
                fake.job["failure_message"],
                "VRAM remains above reserve after recovery: 3072 MiB > 1536 MiB",
            )
            self.assertFalse((Path(tmp) / "temporary" / "job_gpu.json").exists())

    def test_model_download_runner_completes_when_blob_already_verified(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"already present"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"},
            "files": [{"path": "chat-small.gguf", "sha256": digest, "size_bytes": len(payload)}],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }
        fake.model_download = {
            "id": "modeldl_1",
            "status": "queued",
            "stage": "queued",
            "manifest": manifest,
            "target_size_bytes": len(payload),
            "target_sha256": digest,
            "bytes_downloaded": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(payload)

            runner = executor.ModelDownloadRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.model_download["status"], "completed")
            self.assertEqual(fake.model_download["stage"], "already_available")
            self.assertEqual(fake.model_download["bytes_downloaded"], len(payload))

    def test_model_download_runner_uses_persisted_license_acceptance(self) -> None:
        payload = b"licensed model already present"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"},
            "files": [{"path": "chat-small.gguf", "sha256": digest, "size_bytes": len(payload)}],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable", "acceptance_required": True},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(payload)

            rejected = FakeDatabase()
            self.patch_database(rejected)
            rejected.model_download = {
                "id": "modeldl_unaccepted",
                "status": "queued",
                "stage": "queued",
                "manifest": manifest,
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "bytes_downloaded": 0,
                "license_accepted": False,
            }

            processed = asyncio.run(executor.ModelDownloadRunner(root).run_once())

            self.assertTrue(processed)
            self.assertEqual(rejected.model_download["status"], "failed")
            self.assertEqual(rejected.model_download["stage"], "failed")
            self.assertEqual(rejected.model_download["error_category"], "ModelLifecycleError")
            self.assertIn("licence acceptance is required", rejected.model_download["error_message"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(payload)

            accepted = FakeDatabase()
            self.patch_database(accepted)
            accepted.model_download = {
                "id": "modeldl_accepted",
                "status": "queued",
                "stage": "queued",
                "manifest": manifest,
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "bytes_downloaded": 0,
                "license_accepted": True,
            }

            processed = asyncio.run(executor.ModelDownloadRunner(root).run_once())

            self.assertTrue(processed)
            self.assertEqual(accepted.model_download["status"], "completed")
            self.assertEqual(accepted.model_download["stage"], "already_available")
            self.assertEqual(accepted.model_download["bytes_downloaded"], len(payload))

    def test_model_download_runner_counts_multi_file_verified_blobs(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        first = b"already present"
        second = b"also present"
        first_digest = hashlib.sha256(first).hexdigest()
        second_digest = hashlib.sha256(second).hexdigest()
        manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {"type": "direct-url", "url": "https://downloads.example.org/model/", "revision": "1.0.0"},
            "files": [
                {"path": "weights/model.gguf", "sha256": first_digest, "size_bytes": len(first)},
                {"path": "tokenizer.json", "sha256": second_digest, "size_bytes": len(second)},
            ],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }
        fake.model_download = {
            "id": "modeldl_2",
            "status": "queued",
            "stage": "queued",
            "manifest": manifest,
            "target_size_bytes": len(first) + len(second),
            "target_sha256": first_digest,
            "bytes_downloaded": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / first_digest).write_bytes(first)
            (blob_dir / second_digest).write_bytes(second)

            runner = executor.ModelDownloadRunner(root)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.model_download["status"], "completed")
            self.assertEqual(fake.model_download["stage"], "already_available")
            self.assertEqual(fake.model_download["bytes_downloaded"], len(first) + len(second))

    def test_model_download_runner_uses_encrypted_bearer_credential(self) -> None:
        if secret_store is None or secret_store.AESGCM is None:
            self.skipTest("cryptography is not installed in this lightweight test environment")
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"private model"
        digest = hashlib.sha256(payload).hexdigest()
        master_key = "m" * 64
        now = datetime.now(tz=UTC)
        fake.encrypted_secrets["model-download:hf"] = {
            "name": "model-download:hf",
            "display_name": "Hugging Face token",
            "category": "model-download",
            "description": "test token",
            "secret_envelope": secret_store.encrypt_value(master_key, "model-download:hf", "hf_read_token", created_at=now),
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {"type": "direct-url", "url": "https://downloads.example.org/model.gguf", "revision": "1.0.0"},
            "files": [{"path": "chat-small.gguf", "sha256": digest, "size_bytes": len(payload)}],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }
        fake.model_download = {
            "id": "modeldl_private",
            "status": "queued",
            "stage": "queued",
            "manifest": manifest,
            "target_size_bytes": len(payload),
            "target_sha256": digest,
            "bytes_downloaded": 0,
            "credential_secret_name": "model-download:hf",
        }

        class FakeResponse:
            status_code = 200

            async def aiter_bytes(self, chunk_size: int):
                yield payload

        class FakeStreamContext:
            async def __aenter__(self) -> FakeResponse:
                return FakeResponse()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext()

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = executor.ModelDownloadRunner(root, master_key=master_key)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.model_download["status"], "completed")
            self.assertEqual((root / "models" / "blobs" / digest).read_bytes(), payload)
            self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer hf_read_token")
            self.assertNotIn("hf_read_token", repr(fake.model_download))

    def test_model_download_runner_revalidates_source_url_before_streaming(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"runtime safe check"
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_rebind",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": 0,
        }

        class FakeStreamContext:
            async def __aenter__(self) -> Any:
                raise AssertionError("stream should not open for an unsafe runtime URL")

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext()

        original_client = executor.httpx.AsyncClient
        original_resolver = security.resolve_hostname_addresses
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        security.resolve_hostname_addresses = lambda hostname, port: ["127.0.0.1"]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original_resolver))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(root / "models" / "blobs" / digest),
                "partial_path": str(root / "models" / "blobs" / ".partial" / f"{digest}.partial"),
            }

            with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, "not allowed by import policy"):
                asyncio.run(runner.download_file("modeldl_rebind", file_plan, 0, "file_1"))

            self.assertEqual(FakeAsyncClient.calls, [])

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "symlink"), "symlink parent refusal is POSIX-specific")
    def test_model_download_runner_rejects_symlinked_blob_parent_before_mkdir(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"parent link"
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_parent_link",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "models").symlink_to(outside, target_is_directory=True)
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(root / "models" / "blobs" / digest),
                "partial_path": str(root / "models" / "blobs" / ".partial" / f"{digest}.partial"),
            }

            with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, "target blob path contains a symlink"):
                asyncio.run(runner.download_file("modeldl_parent_link", file_plan, 0, "file_1"))

            self.assertFalse((outside / "blobs").exists())

    def test_model_download_runner_rejects_non_regular_target_blob(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"target dir"
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_target_dir",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = root / "models" / "blobs" / digest
            target_path.mkdir(parents=True)
            partial_path = root / "models" / "blobs" / ".partial" / f"{digest}.partial"
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(target_path),
                "partial_path": str(partial_path),
            }

            with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, "target blob path is not a regular file"):
                asyncio.run(runner.download_file("modeldl_target_dir", file_plan, 0, "file_1"))

    def test_model_download_runner_rejects_non_regular_partial_blob(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"partial dir"
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_partial_dir",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial_path = root / "models" / "blobs" / ".partial" / f"{digest}.partial"
            partial_path.mkdir(parents=True)
            target_path = root / "models" / "blobs" / digest
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(target_path),
                "partial_path": str(partial_path),
            }

            with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, "partial blob path is not a regular file"):
                asyncio.run(runner.download_file("modeldl_partial_dir", file_plan, 0, "file_1"))

    def test_model_download_runner_rejects_tampered_content_addressed_paths(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"target digest"
        digest = hashlib.sha256(payload).hexdigest()
        other_digest = hashlib.sha256(b"other digest").hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = executor.ModelDownloadRunner(root)
            canonical_target = root / "models" / "blobs" / digest
            canonical_partial = root / "models" / "blobs" / ".partial" / f"{digest}.partial"

            cases = [
                (
                    {
                        "target_path": str(root / "models" / "blobs" / other_digest),
                        "partial_path": str(canonical_partial),
                    },
                    "target blob path does not match target SHA-256",
                ),
                (
                    {
                        "target_path": str(canonical_target),
                        "partial_path": str(root / "models" / "blobs" / ".partial" / f"{other_digest}.partial"),
                    },
                    "partial blob path does not match target SHA-256",
                ),
                (
                    {
                        "target_path": str(canonical_target),
                        "partial_path": str(canonical_partial),
                        "target_sha256": "not-a-sha",
                    },
                    "target blob SHA-256 is invalid",
                ),
            ]
            for override, expected in cases:
                with self.subTest(expected=expected):
                    file_plan = {
                        "source_type": "direct-url",
                        "source_url": "https://downloads.example.org/model.gguf",
                        "target_size_bytes": len(payload),
                        "target_sha256": digest,
                        "target_path": str(canonical_target),
                        "partial_path": str(canonical_partial),
                        **override,
                    }

                    with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, expected):
                        asyncio.run(runner.download_file("modeldl_tampered", file_plan, 0, "file_1"))

            self.assertFalse((root / "models").exists())

    def test_model_download_runner_validates_resumed_content_range(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        partial_payload = b"abc"
        remaining_payload = b"def"
        payload = partial_payload + remaining_payload
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_resume",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": len(partial_payload),
        }

        class FakeResponse:
            status_code = 206
            headers = {"Content-Range": f"bytes {len(partial_payload)}-{len(payload) - 1}/{len(payload)}", "Content-Length": str(len(remaining_payload))}

            async def aiter_bytes(self, chunk_size: int):
                yield remaining_payload

        class FakeStreamContext:
            async def __aenter__(self) -> FakeResponse:
                return FakeResponse()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext()

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial_path = root / "models" / "blobs" / ".partial" / f"{digest}.partial"
            partial_path.parent.mkdir(parents=True)
            partial_path.write_bytes(partial_payload)
            target_path = root / "models" / "blobs" / digest
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(target_path),
                "partial_path": str(partial_path),
            }

            downloaded = asyncio.run(runner.download_file("modeldl_resume", file_plan, 0, "file_1"))

            self.assertTrue(downloaded)
            self.assertEqual(target_path.read_bytes(), payload)
            self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Range"], f"bytes={len(partial_payload)}-")
            self.assertFalse(partial_path.exists())

    def test_model_download_runner_rejects_resumed_content_range_mismatch_before_append(self) -> None:
        fake = FakeDatabase()
        self.patch_database(fake)
        partial_payload = b"abc"
        remaining_payload = b"def"
        payload = partial_payload + remaining_payload
        digest = hashlib.sha256(payload).hexdigest()
        fake.model_download = {
            "id": "modeldl_bad_resume",
            "status": "running",
            "stage": "downloading",
            "manifest": {},
            "bytes_downloaded": len(partial_payload),
        }

        class FakeResponse:
            status_code = 206
            headers = {"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}", "Content-Length": str(len(remaining_payload))}

            async def aiter_bytes(self, chunk_size: int):
                yield remaining_payload

        class FakeStreamContext:
            async def __aenter__(self) -> FakeResponse:
                return FakeResponse()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                return FakeStreamContext()

        original_client = executor.httpx.AsyncClient
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial_path = root / "models" / "blobs" / ".partial" / f"{digest}.partial"
            partial_path.parent.mkdir(parents=True)
            partial_path.write_bytes(partial_payload)
            target_path = root / "models" / "blobs" / digest
            runner = executor.ModelDownloadRunner(root)
            file_plan = {
                "source_type": "direct-url",
                "source_url": "https://downloads.example.org/model.gguf",
                "target_size_bytes": len(payload),
                "target_sha256": digest,
                "target_path": str(target_path),
                "partial_path": str(partial_path),
            }

            with self.assertRaisesRegex(executor.model_lifecycle.ModelLifecycleError, "unexpected Content-Range"):
                asyncio.run(runner.download_file("modeldl_bad_resume", file_plan, 0, "file_1"))

            self.assertEqual(partial_path.read_bytes(), partial_payload)
            self.assertFalse(target_path.exists())

    def test_model_download_runner_follows_safe_huggingface_redirect_without_forwarding_token(self) -> None:
        if secret_store is None or secret_store.AESGCM is None:
            self.skipTest("cryptography is not installed in this lightweight test environment")
        fake = FakeDatabase()
        self.patch_database(fake)
        payload = b"hf model bytes"
        digest = hashlib.sha256(payload).hexdigest()
        master_key = "m" * 64
        now = datetime.now(tz=UTC)
        fake.encrypted_secrets["model-download:hf"] = {
            "name": "model-download:hf",
            "display_name": "Hugging Face token",
            "category": "model-download",
            "description": "test token",
            "secret_envelope": secret_store.encrypt_value(master_key, "model-download:hf", "hf_read_token", created_at=now),
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {
                "type": "huggingface",
                "url": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
                "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
            },
            "files": [{"path": "model.safetensors", "sha256": digest, "size_bytes": len(payload)}],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }
        fake.model_download = {
            "id": "modeldl_hf",
            "status": "queued",
            "stage": "queued",
            "manifest": manifest,
            "target_size_bytes": len(payload),
            "target_sha256": digest,
            "bytes_downloaded": 0,
            "credential_secret_name": "model-download:hf",
        }

        class FakeResponse:
            def __init__(self, status_code: int, headers: dict[str, str] | None = None, chunks: list[bytes] | None = None) -> None:
                self.status_code = status_code
                self.headers = headers or {}
                self.chunks = chunks or []

            async def aiter_bytes(self, chunk_size: int):
                for chunk in self.chunks:
                    yield chunk

        class FakeStreamContext:
            def __init__(self, response: FakeResponse) -> None:
                self.response = response

            async def __aenter__(self) -> FakeResponse:
                return self.response

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        class FakeAsyncClient:
            calls: list[dict[str, Any]] = []
            responses: list[FakeResponse] = [
                FakeResponse(302, {"location": "https://us.aws.cdn.hf.co/xet-bridge-us/model?Signature=temporary"}),
                FakeResponse(200, chunks=[payload]),
            ]

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def stream(self, method: str, url: str, headers: dict[str, str]) -> FakeStreamContext:
                self.calls.append({"method": method, "url": url, "headers": dict(headers)})
                return FakeStreamContext(self.responses.pop(0))

        original_client = executor.httpx.AsyncClient
        FakeAsyncClient.calls = []
        executor.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(executor.httpx, "AsyncClient", original_client))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = executor.ModelDownloadRunner(root, master_key=master_key)
            processed = asyncio.run(runner.run_once())

            self.assertTrue(processed)
            self.assertEqual(fake.model_download["status"], "completed")
            self.assertEqual((root / "models" / "blobs" / digest).read_bytes(), payload)
            self.assertEqual(len(FakeAsyncClient.calls), 2)
            self.assertEqual(
                FakeAsyncClient.calls[0]["url"],
                "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/model.safetensors",
            )
            self.assertEqual(FakeAsyncClient.calls[0]["headers"]["Authorization"], "Bearer hf_read_token")
            self.assertTrue(FakeAsyncClient.calls[1]["url"].startswith("https://us.aws.cdn.hf.co/"))
            self.assertNotIn("Authorization", FakeAsyncClient.calls[1]["headers"])


if __name__ == "__main__":
    unittest.main()
