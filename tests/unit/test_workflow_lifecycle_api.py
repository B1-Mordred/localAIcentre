from __future__ import annotations

import asyncio
import json
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
    HTTPException = None  # type: ignore[assignment]
    main = None
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


def workflow_row(version: str = "1.0.0", *, ready: bool = True, unpublished: bool = False) -> dict[str, Any]:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    payload = {**BASE_WORKFLOW, "version": version}
    manifest = parse_workflow(payload).to_dict()
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "display_name": manifest["display_name"],
        "backend_policy": manifest["backend_policy"],
        "resource_class": manifest["resource_class"],
        "status": "unpublished" if unpublished else "published" if ready else "needs_dependencies",
        "visibility_roles": manifest["visibility_roles"],
        "manifest": manifest,
        "dependency_status": {"ready": ready, "dependencies": []},
        "created_at": now,
        "updated_at": now,
        "unpublished_at": now if unpublished else None,
    }


class FakeWorkflowLifecycleDatabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.restored_payloads: list[dict[str, Any]] = []

    async def list_workflows(self, include_unpublished: bool = False) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in sorted(self.rows, key=lambda item: (item["id"], item["version"]), reverse=False)
            if include_unpublished or row.get("unpublished_at") is None
        ]

    async def get_workflow(self, workflow_id: str, version: str | None = None, include_unpublished: bool = False) -> dict[str, Any] | None:
        candidates = [
            row
            for row in self.rows
            if row["id"] == workflow_id
            and (version is None or row["version"] == version)
            and (include_unpublished or row.get("unpublished_at") is None)
        ]
        if not candidates:
            return None
        return dict(sorted(candidates, key=lambda item: item["version"], reverse=True)[0])

    async def restore_workflow(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.restored_payloads.append(dict(payload))
        for row in self.rows:
            if row["id"] == payload["id"] and row["version"] == payload["version"]:
                row.update(
                    {
                        "display_name": payload["display_name"],
                        "backend_policy": payload["backend_policy"],
                        "resource_class": payload["resource_class"],
                        "status": payload["status"],
                        "visibility_roles": payload["visibility_roles"],
                        "manifest": payload["manifest"],
                        "dependency_status": payload["dependency_status"],
                        "updated_at": datetime(2026, 7, 26, 12, 5, tzinfo=UTC),
                        "unpublished_at": None,
                    }
                )
                return dict(row)
        return None


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class WorkflowLifecycleApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, fake_database: FakeWorkflowLifecycleDatabase, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"}))

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_refresh_node_pin_registry() -> tuple[dict[Any, Any], dict[Any, Any], dict[Any, Any]]:
            return {}, {}, {}

        def fake_workflow_record_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
            manifest = parse_workflow(payload).to_dict()
            return {**manifest, "status": "published", "dependency_status": {"ready": True, "dependencies": []}, "publishable": True}

        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("refresh_node_pin_registry", fake_refresh_node_pin_registry)
        self.patch_attr("workflow_record_from_payload", fake_workflow_record_from_payload)
        self.patch_attr("database", fake_database)

    def test_workflow_execution_summary_is_api_derived_not_manifest_persisted(self) -> None:
        manifest = parse_workflow(BASE_WORKFLOW).to_dict()
        dependency_status = {
            "ready": True,
            "dependencies": [
                {"type": "runtime", "id": "comfyui", "status": "available", "ready": True},
                {
                    "type": "model",
                    "id": "image-default",
                    "status": "installed",
                    "ready": True,
                    "preferred_runtime": "comfyui",
                    "runtimes": ["comfyui"],
                    "resolved_model": {"id": "sdxl", "version": "1.0.0"},
                },
            ],
        }
        record = {
            **manifest,
            "status": "published",
            "dependency_status": dependency_status,
            "execution_summary": {"selected_runtime": "comfyui"},
            "publishable": True,
        }

        payload = main.db_workflow_payload(record)
        self.assertNotIn("execution_summary", payload["manifest"])

        row = workflow_row()
        row["dependency_status"] = dependency_status
        public = main.public_workflow(row)
        self.assertEqual(public["execution_summary"]["selected_runtime"], "comfyui")
        self.assertTrue(public["execution_summary"]["server_side_comfyui_required"])
        self.assertTrue(public["execution_summary"]["requires_gpu_lease"])

    def test_workflow_test_validates_draft_without_echoing_parameter_values(self) -> None:
        audit_events: list[dict[str, Any]] = []
        self.patch_common(FakeWorkflowLifecycleDatabase([]), audit_events)

        result = asyncio.run(
            main.workflow_test(
                main.WorkflowTestRequest(
                    workflow=BASE_WORKFLOW,
                    parameters={"prompt": "private operator prompt", "steps": 20},
                )
            )
        )

        serialized = json.dumps(result, sort_keys=True)
        self.assertTrue(result["can_submit"])
        self.assertEqual(result["request"]["input"]["parameter_names"], ["prompt", "steps"])
        self.assertNotIn("private operator prompt", serialized)
        self.assertEqual(audit_events[0]["event_type"], "workflow.tested")
        self.assertEqual(audit_events[0]["metadata"]["parameter_names"], ["prompt", "steps"])
        self.assertNotIn("private operator prompt", json.dumps(audit_events, sort_keys=True))

    def test_workflow_test_rejects_missing_required_parameter(self) -> None:
        audit_events: list[dict[str, Any]] = []
        self.patch_common(FakeWorkflowLifecycleDatabase([workflow_row()]), audit_events)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.workflow_test(
                    main.WorkflowTestRequest(
                        workflow_id="media-workflow",
                        workflow_version="1.0.0",
                        parameters={"prompt": "ok"},
                    )
                )
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("missing required", caught.exception.detail)
        self.assertEqual(audit_events, [])

    def test_workflow_versions_include_unpublished_rows_for_governance(self) -> None:
        audit_events: list[dict[str, Any]] = []
        self.patch_common(
            FakeWorkflowLifecycleDatabase([workflow_row("1.0.0", unpublished=True), workflow_row("1.1.0")]),
            audit_events,
        )

        result = asyncio.run(main.workflow_published_versions("media-workflow"))

        self.assertEqual(result["object"], "list")
        statuses = {row["version"]: row["status"] for row in result["data"]}
        self.assertEqual(statuses["1.0.0"], "unpublished")
        self.assertEqual(statuses["1.1.0"], "published")

    def test_workflow_restore_requires_confirmation_and_clears_unpublished_status(self) -> None:
        audit_events: list[dict[str, Any]] = []
        fake_database = FakeWorkflowLifecycleDatabase([workflow_row("1.0.0", unpublished=True)])
        self.patch_common(fake_database, audit_events)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.workflow_restore(
                    "media-workflow",
                    "1.0.0",
                    main.WorkflowRestoreRequest(confirm=False),
                )
            )
        self.assertEqual(caught.exception.status_code, 400)

        restored = asyncio.run(
            main.workflow_restore(
                "media-workflow",
                "1.0.0",
                main.WorkflowRestoreRequest(confirm=True, reason="rollback rehearsal"),
            )
        )

        self.assertEqual(restored["status"], "published")
        self.assertIsNone(restored["unpublished_at"])
        self.assertEqual(fake_database.restored_payloads[0]["id"], "media-workflow")
        self.assertEqual(audit_events[0]["event_type"], "workflow.restored")
        self.assertTrue(audit_events[0]["metadata"]["reason_provided"])


if __name__ == "__main__":
    unittest.main()
