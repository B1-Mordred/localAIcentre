from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
    from app.workflows import parse_workflow  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    HTTPException = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    parse_workflow = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


BASE_WORKFLOW = {
    "id": "media-workflow",
    "version": "1.0.0",
    "display_name": "Media Workflow",
    "modality": "image",
    "operation": "generation",
    "model_alias": "image-default",
    "backend_policy": "comfyui-only",
    "runtime_policy": "any",
    "output_mime_types": ["image/png"],
    "workflow_json": {},
    "input_schema": {
        "type": "object",
        "required": ["prompt", "steps"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 100},
            "steps": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "additionalProperties": False,
    },
    "output_schema": {"type": "object", "properties": {"images": {"type": "array"}}},
    "resource_class": "rtx3060-32gb",
    "dependencies": [{"type": "runtime", "id": "comfyui"}, {"type": "model", "id": "image-default"}],
    "limits": {"max_steps": 40, "max_batch_size": 1},
    "visibility_roles": ["admin", "creator", "user"],
}


class FakeWorkflowDatabase:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def get_workflow(self, workflow_id: str, version: str | None = None) -> dict[str, Any] | None:
        if self.row and self.row["id"] == workflow_id and self.row["version"] == version:
            return dict(self.row)
        return None


def workflow_row(*, ready: bool = True) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    manifest = parse_workflow(BASE_WORKFLOW).to_dict()
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "display_name": manifest["display_name"],
        "backend_policy": manifest["backend_policy"],
        "resource_class": manifest["resource_class"],
        "status": "published" if ready else "needs_dependencies",
        "visibility_roles": manifest["visibility_roles"],
        "manifest": manifest,
        "dependency_status": {"ready": ready, "dependencies": []},
        "created_at": now,
        "updated_at": now,
        "unpublished_at": None,
    }


def media_payload(**overrides: Any) -> Any:
    payload = {
        "modality": "image",
        "operation": "generation",
        "model": "image-default",
        "runtime_policy": "any",
        "priority": "single_image",
        "input": {
            "workflow_id": "media-workflow",
            "workflow_version": "1.0.0",
            "parameters": {"prompt": "make image", "steps": 20},
        },
    }
    payload.update(overrides)
    return main.MediaJobCreate(**payload)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class MediaJobWorkflowEnforcementTests(unittest.TestCase):
    def patch_database(self, fake_database: FakeWorkflowDatabase) -> None:
        original = main.database
        main.database = fake_database
        self.addCleanup(lambda: setattr(main, "database", original))

    def test_workflow_backed_media_job_is_validated_against_published_workflow(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row()))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        workflow = asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload()))

        self.assertIsNotNone(workflow)
        self.assertEqual(workflow["id"], "media-workflow")

    def test_workflow_backed_media_job_rejects_unready_dependencies(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row(ready=False)))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload()))

        self.assertEqual(caught.exception.status_code, 424)
        self.assertEqual(caught.exception.detail["message"], "workflow dependencies are not ready")

    def test_workflow_backed_media_job_rejects_metadata_tampering(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row()))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload(model="image-edit")))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("model must match workflow value", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
