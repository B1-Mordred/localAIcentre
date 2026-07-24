from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import admission, main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    admission = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def resolution() -> Any:
    return main.RuntimeResolution(
        public_alias="image-default",
        model_id="sdxl-light",
        model_version="1.0.0",
        resolved_model_version="sdxl-light@1.0.0",
        runtime="comfyui",
        preferred_runtime="comfyui",
        requires_gpu=True,
        resource_label="expected",
        runtime_policy="any",
    )


def job_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    row = {
        "id": "job_existing",
        "correlation_id": "corr_existing",
        "idempotency_key": "idem_1",
        "owner_id": "client_1",
        "modality": "image",
        "operation": "generation",
        "model_alias": "image-default",
        "resolved_model_version": "sdxl-light@1.0.0",
        "runtime": "comfyui",
        "priority": "single_image",
        "state": "queued",
        "request_params": {},
        "redacted_request": {},
        "progress": 10,
        "stage": "queued",
        "native_prompt_id": None,
        "artifacts": [],
        "retry_count": 0,
        "failure_category": None,
        "failure_message": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def media_job_payload(**overrides: Any) -> Any:
    values = {
        "modality": "image",
        "operation": "generation",
        "model": "image-default",
    }
    values.update(overrides)
    return main.MediaJobCreate(**values)


def media_job_request_params(**overrides: Any) -> dict[str, Any]:
    return media_job_payload(**overrides).model_dump()


class FakeAdmissionDatabase:
    def __init__(
        self,
        *,
        owner_queued: int = 0,
        owner_active: int = 0,
        owner_recent: int = 0,
        global_queued: int = 0,
        existing: dict[str, Any] | None = None,
    ) -> None:
        self.owner_queued = owner_queued
        self.owner_active = owner_active
        self.owner_recent = owner_recent
        self.global_queued = global_queued
        self.existing = existing
        self.inserted: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.row: dict[str, Any] | None = None

    async def get_job_by_idempotency_key(self, owner_id: str, idempotency_key: str) -> dict[str, Any] | None:
        if self.existing and self.existing["owner_id"] == owner_id and self.existing["idempotency_key"] == idempotency_key:
            return dict(self.existing)
        return None

    async def count_jobs(
        self,
        *,
        owner_id: str | None = None,
        states: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
        created_after: datetime | None = None,
    ) -> int:
        if owner_id == "client_1" and created_after is not None:
            return self.owner_recent
        if owner_id == "client_1" and states is not None and set(states) == set(admission.QUEUE_STATES):
            return self.owner_queued
        if owner_id == "client_1" and states is not None and set(states) == set(admission.ACTIVE_STATES):
            return self.owner_active
        if owner_id is None and states is not None and set(states) == set(admission.QUEUE_STATES):
            return self.global_queued
        return 0

    async def insert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = job_row(
            id=payload["id"],
            correlation_id=payload["correlation_id"],
            idempotency_key=payload.get("idempotency_key"),
            owner_id=payload["owner_id"],
            request_params=payload["request_params"],
            redacted_request=payload["redacted_request"],
            resolved_model_version=payload["resolved_model_version"],
            runtime=payload["runtime"],
        )
        self.inserted.append(payload)
        self.row = row
        return dict(row)

    async def update_job(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        if self.row is None or self.row["id"] != job_id:
            return None
        self.updated.append((job_id, dict(changes)))
        self.row = {**self.row, **changes}
        return dict(self.row)


class FakeRequest:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def stream(self):
        yield self._body


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AdmissionApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_settings(self, **changes: Any) -> None:
        original = main.settings
        main.settings = replace(main.settings, **changes)
        self.addCleanup(lambda: setattr(main, "settings", original))

    def patch_auth(self, auth: Any) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", fake_authenticate)

    def test_create_job_record_rejects_owner_queue_limit_before_insert(self) -> None:
        fake_database = FakeAdmissionDatabase(owner_queued=2, global_queued=2)
        self.patch_attr("database", fake_database)
        self.patch_settings(
            max_queued_jobs_per_owner=2,
            max_active_jobs_per_owner=0,
            max_jobs_per_hour_per_owner=0,
            max_queued_jobs_global=0,
            artifact_storage_max_bytes=0,
            artifact_storage_reserve_bytes=0,
        )

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.create_job_record(
                    "client_1",
                    main.MediaJobCreate(modality="image", operation="generation", model="image-default"),
                    resolution=resolution(),
                )
            )

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.detail["code"], "owner_queue_limit")
        self.assertEqual(fake_database.inserted, [])

    def test_idempotent_create_returns_existing_job_before_admission_limits(self) -> None:
        existing = job_row(idempotency_key="idem_1", request_params=media_job_request_params())
        fake_database = FakeAdmissionDatabase(owner_queued=99, existing=existing)
        self.patch_attr("database", fake_database)
        self.patch_settings(max_queued_jobs_per_owner=1, artifact_storage_reserve_bytes=0)

        result = asyncio.run(
            main.create_job_record(
                "client_1",
                media_job_payload(),
                idempotency_key="idem_1",
                resolution=resolution(),
            )
        )

        self.assertEqual(result["id"], "job_existing")
        self.assertEqual(fake_database.inserted, [])

    def test_idempotent_replay_returns_existing_when_alias_resolution_changed(self) -> None:
        existing = job_row(
            idempotency_key="idem_1",
            resolved_model_version="sdxl-light@1.0.0",
            runtime="comfyui",
            request_params=media_job_request_params(),
        )
        fake_database = FakeAdmissionDatabase(existing=existing)
        self.patch_attr("database", fake_database)
        current_resolution = main.RuntimeResolution(
            public_alias="image-default",
            model_id="sdxl-new",
            model_version="2.0.0",
            resolved_model_version="sdxl-new@2.0.0",
            runtime="localai",
            preferred_runtime="localai",
            requires_gpu=True,
            resource_label="expected",
            runtime_policy="any",
        )

        result = asyncio.run(
            main.create_job_record(
                "client_1",
                media_job_payload(),
                idempotency_key="idem_1",
                resolution=current_resolution,
            )
        )

        self.assertEqual(result["id"], "job_existing")
        self.assertEqual(fake_database.inserted, [])

    def test_idempotent_create_rejects_key_reuse_for_different_request(self) -> None:
        existing = job_row(idempotency_key="idem_1", request_params=media_job_request_params())
        fake_database = FakeAdmissionDatabase(existing=existing)
        self.patch_attr("database", fake_database)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.create_job_record(
                    "client_1",
                    media_job_payload(model="image-edit", operation="edit"),
                    idempotency_key="idem_1",
                    resolution=resolution(),
                )
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "idempotency_key_conflict")
        self.assertIn("operation", caught.exception.detail["mismatched_fields"])
        self.assertIn("model_alias", caught.exception.detail["mismatched_fields"])
        self.assertEqual(fake_database.inserted, [])

    def test_create_job_record_rejects_invalid_idempotency_key_before_insert(self) -> None:
        fake_database = FakeAdmissionDatabase()
        self.patch_attr("database", fake_database)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.create_job_record(
                    "client_1",
                    media_job_payload(),
                    idempotency_key="bad key",
                    resolution=resolution(),
                )
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "invalid_idempotency_key")
        self.assertEqual(fake_database.inserted, [])

    def test_image_edit_idempotency_returns_existing_job_without_reprocessing_body(self) -> None:
        existing = job_row(
            idempotency_key="edit_1",
            operation="edit",
            model_alias="image-edit",
            request_params=media_job_request_params(operation="edit", model="image-edit"),
        )
        fake_database = FakeAdmissionDatabase(existing=existing)
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"})))

        async def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("image edit request body should not be processed for an idempotent replay")

        self.patch_attr("image_edit_input_from_request", fail_if_called)
        request = FakeRequest(body=b"not read", headers={"content-type": "application/octet-stream"})

        result = asyncio.run(main.image_edits(request, idempotency_key="edit_1"))

        self.assertEqual(result["b1_job_id"], "job_existing")
        self.assertEqual(fake_database.inserted, [])

    def test_image_generation_idempotency_returns_existing_before_alias_resolution(self) -> None:
        payload = {"model": "image-default", "prompt": "castle"}
        existing = job_row(idempotency_key="img_1", request_params=media_job_request_params(input=payload))
        fake_database = FakeAdmissionDatabase(existing=existing)
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"})))

        def fail_resolver(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("idempotent image generation replay should not resolve mutable aliases")

        self.patch_attr("resolve_catalog_alias_for_auth", fail_resolver)

        result = asyncio.run(main.image_generations(payload, idempotency_key="img_1"))

        self.assertEqual(result["b1_job_id"], "job_existing")
        self.assertEqual(fake_database.inserted, [])

    def test_media_job_idempotency_returns_existing_before_workflow_or_alias_checks(self) -> None:
        payload = media_job_payload(input={"workflow_id": "workflow_1", "workflow_version": "1.0.0", "parameters": {"prompt": "castle"}})
        existing = job_row(idempotency_key="media_1", request_params=payload.model_dump())
        fake_database = FakeAdmissionDatabase(existing=existing)
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:write"})))

        async def fail_workflow_check(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("idempotent media-job replay should not revalidate mutable workflow state")

        def fail_resolver(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("idempotent media-job replay should not resolve mutable aliases")

        self.patch_attr("enforce_workflow_backed_media_job", fail_workflow_check)
        self.patch_attr("resolve_catalog_alias_for_auth", fail_resolver)

        result = asyncio.run(main.media_job_create(payload, idempotency_key="media_1"))

        self.assertEqual(result["id"], "job_existing")
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertNotIn("castle", str(result))
        self.assertEqual(fake_database.inserted, [])

    def test_media_upload_rejects_artifact_storage_limit_before_staging(self) -> None:
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:write"})))
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(
                artifact_root=tmp,
                upload_max_bytes=1024,
                artifact_storage_max_bytes=4,
                artifact_storage_reserve_bytes=0,
            )
            request = FakeRequest(body=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"})

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.media_upload_create(request))

            self.assertEqual(caught.exception.status_code, 507)
            self.assertEqual(caught.exception.detail["code"], "artifact_storage_limit")
            self.assertFalse(any(Path(tmp).rglob("*.*")))

    def test_admin_admission_returns_queue_and_storage_report(self) -> None:
        fake_database = FakeAdmissionDatabase(owner_queued=1, owner_active=2, owner_recent=3, global_queued=4)
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read"})))
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, artifact_storage_max_bytes=0, artifact_storage_reserve_bytes=0)

            result = asyncio.run(main.admin_admission(owner_id="client_1"))

        self.assertEqual(result["queue"]["owner_id"], "client_1")
        self.assertEqual(result["queue"]["owner_queued_jobs"], 1)
        self.assertEqual(result["queue"]["owner_active_jobs"], 2)
        self.assertEqual(result["queue"]["owner_jobs_last_hour"], 3)
        self.assertEqual(result["queue"]["global_queued_jobs"], 4)
        self.assertEqual(result["storage"]["artifact_storage_reserve_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
