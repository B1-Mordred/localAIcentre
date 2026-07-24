from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    HTTPException = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeRequest:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None, json_payload: Any = None) -> None:
        self._body = body
        self.headers = headers or {}
        self._json_payload = json_payload

    async def stream(self):
        yield self._body

    async def json(self) -> Any:
        return self._json_payload


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class MediaUploadApiTests(unittest.TestCase):
    def patch_settings(self, **changes: Any) -> None:
        original = main.settings
        main.settings = replace(main.settings, **changes)
        self.addCleanup(lambda: setattr(main, "settings", original))

    def test_raw_image_edit_body_is_staged_as_image_reference(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, upload_max_bytes=1024, artifact_storage_reserve_bytes=0)
            request = FakeRequest(
                body=png_bytes,
                headers={"content-type": "application/octet-stream", "X-B1-Filename": "../input.png"},
            )

            payload = asyncio.run(main.image_edit_input_from_request(request, auth))

            self.assertEqual(payload["image"]["source"], "staged_upload")
            self.assertEqual(payload["image"]["mime_type"], "image/png")
            self.assertEqual(payload["image"]["filename"], "input.png")
            self.assertEqual((Path(tmp) / payload["image"]["path"]).read_bytes(), png_bytes)

    def test_raw_upload_rejects_configured_size_limit(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_settings(artifact_root=tmp, upload_max_bytes=4, artifact_storage_reserve_bytes=0)
            request = FakeRequest(body=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"})

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.image_edit_input_from_request(request, auth))

            self.assertEqual(caught.exception.status_code, 413)

    def test_json_image_edit_body_is_preserved_for_data_url_or_references(self) -> None:
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))
        request = FakeRequest(
            headers={"content-type": "application/json"},
            json_payload={"model": "image-edit", "prompt": "edit", "image": "data:image/png;base64,AA=="},
        )

        payload = asyncio.run(main.image_edit_input_from_request(request, auth))

        self.assertEqual(payload["prompt"], "edit")
        self.assertEqual(payload["image"], "data:image/png;base64,AA==")


if __name__ == "__main__":
    unittest.main()
