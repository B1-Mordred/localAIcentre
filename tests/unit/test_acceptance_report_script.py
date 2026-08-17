from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "deploy" / "scripts" / "acceptance_report.py"
spec = importlib.util.spec_from_file_location("b1_acceptance_report_script", SCRIPT_PATH)
acceptance_report = importlib.util.module_from_spec(spec)
sys.modules["b1_acceptance_report_script"] = acceptance_report
assert spec.loader is not None
spec.loader.exec_module(acceptance_report)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class AcceptanceReportScriptTests(unittest.TestCase):
    def patch_env(self, **values: str) -> None:
        keys = {
            "B1_ACCEPTANCE_API_BASE",
            "B1_ACCEPTANCE_API_KEY",
            "B1_ACCEPTANCE_API_KEY_FILE",
            "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP",
            "B1_ACCEPTANCE_CA_FILE",
            "B1_ACCEPTANCE_REPORT_LABEL",
            "B1_ACCEPTANCE_REPORT_NOTES",
            "B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_JSON",
            "B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_FILE",
            "B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_JSON",
            "B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_FILE",
            "B1_ACCEPTANCE_REPORT_OUTPUT",
            "B1_ACCEPTANCE_REPORT_REQUIRE_READY",
            "B1_BACKUP_ROOT",
        }
        patcher = mock.patch.dict(os.environ, {key: value for key, value in values.items()}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in keys - set(values):
            os.environ.pop(key, None)

    def test_validate_api_base_requires_https_unless_explicitly_allowed(self) -> None:
        with self.assertRaises(acceptance_report.AcceptanceReportClientError):
            acceptance_report.validate_api_base("http://api.ai.b1.germering")

        self.assertEqual(
            acceptance_report.validate_api_base("http://api.ai.b1.germering/", allow_insecure_http=True),
            "http://api.ai.b1.germering",
        )

        with self.assertRaises(acceptance_report.AcceptanceReportClientError):
            acceptance_report.validate_api_base("https://token@example.invalid")

    def test_report_payload_reads_operator_evidence_and_notes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence.json"
            notes = Path(tmp) / "notes.json"
            evidence.write_text('{"live_stack_smoke": true}', encoding="utf-8")
            notes.write_text('{"live_stack_smoke": "reviewed smoke evidence"}', encoding="utf-8")
            os.chmod(evidence, 0o600)
            os.chmod(notes, 0o600)

            self.patch_env(
                B1_ACCEPTANCE_REPORT_LABEL="cutover",
                B1_ACCEPTANCE_REPORT_NOTES="temporary hostnames validated",
                B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_FILE=str(evidence),
                B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_FILE=str(notes),
            )
            payload = acceptance_report.report_payload()

        self.assertEqual(payload["label"], "cutover")
        self.assertEqual(payload["notes"], "temporary hostnames validated")
        self.assertEqual(payload["operator_evidence"], {"live_stack_smoke": True})
        self.assertEqual(payload["operator_evidence_notes"], {"live_stack_smoke": "reviewed smoke evidence"})

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are required for public-file checks")
    def test_public_ca_file_can_be_regular_without_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "root.crt"
            ca_file.write_text("not a real cert\n", encoding="utf-8")
            os.chmod(ca_file, 0o644)

            self.assertEqual(acceptance_report.safe_regular_file(ca_file, "B1_ACCEPTANCE_CA_FILE"), ca_file)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are required for private-file checks")
    def test_api_key_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "api-key"
            token_file.write_text("b1k_test\n", encoding="utf-8")
            os.chmod(token_file, 0o644)
            self.patch_env(B1_ACCEPTANCE_API_KEY_FILE=str(token_file))

            with self.assertRaises(acceptance_report.AcceptanceReportClientError):
                acceptance_report.read_token()

    def test_create_posts_report_writes_private_output_and_requires_ready(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, *, context: object = None, timeout: float = 0) -> FakeResponse:
            captured["url"] = getattr(request, "full_url")
            captured["headers"] = dict(getattr(request, "headers"))
            captured["data"] = json.loads(getattr(request, "data").decode("utf-8"))
            captured["context"] = context
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "summary": {"id": "acceptance_1", "status": "ok", "operator_handoff_ready": True},
                    "report": {"id": "acceptance_1", "operator_handoff_ready": True, "acceptance_blockers": []},
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "handoff.json"
            self.patch_env(
                B1_ACCEPTANCE_API_BASE="https://api.ai.b1.germering",
                B1_ACCEPTANCE_API_KEY="b1k_test",
                B1_ACCEPTANCE_REPORT_OUTPUT=str(output),
                B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_JSON='{"live_stack_smoke":true}',
                B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_JSON='{"live_stack_smoke":"ok"}',
                B1_ACCEPTANCE_REPORT_REQUIRE_READY="true",
            )
            with mock.patch.object(acceptance_report.urllib.request, "urlopen", side_effect=fake_urlopen):
                code = acceptance_report.main(["create"])

            written = json.loads(output.read_text(encoding="utf-8"))
            mode = output.stat().st_mode & 0o777

        self.assertEqual(code, 0)
        self.assertEqual(captured["url"], "https://api.ai.b1.germering/admin/acceptance-reports")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer b1k_test")
        self.assertEqual(captured["data"]["operator_evidence"], {"live_stack_smoke": True})
        self.assertEqual(captured["data"]["operator_evidence_notes"], {"live_stack_smoke": "ok"})
        self.assertEqual(written["summary"]["id"], "acceptance_1")
        self.assertEqual(mode, 0o600)

    def test_create_returns_nonzero_when_report_is_blocked(self) -> None:
        def fake_urlopen(request: object, *, context: object = None, timeout: float = 0) -> FakeResponse:
            return FakeResponse(
                {
                    "summary": {"id": "acceptance_2", "status": "incomplete", "operator_handoff_ready": False},
                    "report": {"id": "acceptance_2", "operator_handoff_ready": False, "acceptance_blockers": ["missing GPU proof"]},
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "blocked.json"
            self.patch_env(
                B1_ACCEPTANCE_API_BASE="https://api.ai.b1.germering",
                B1_ACCEPTANCE_API_KEY="b1k_test",
                B1_ACCEPTANCE_REPORT_OUTPUT=str(output),
            )
            with mock.patch.object(acceptance_report.urllib.request, "urlopen", side_effect=fake_urlopen):
                code = acceptance_report.main(["create"])
            output_exists = output.exists()

        self.assertEqual(code, 3)
        self.assertTrue(output_exists)


if __name__ == "__main__":
    unittest.main()
