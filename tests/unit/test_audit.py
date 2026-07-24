from __future__ import annotations

import json
import logging
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import audit  # noqa: E402

try:
    from app import main  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    MISSING_MAIN_DEPENDENCY = exc.name
else:
    MISSING_MAIN_DEPENDENCY = ""


class AuditTests(unittest.TestCase):
    def test_redaction_recurses_without_hiding_safe_identifiers(self) -> None:
        payload = {
            "api_key": "b1k_public.secret",
            "key_prefix": "b1k_public",
            "prompt": "private prompt",
            "model": "chat-default",
            "nested": {
                "messages": [{"role": "user", "content": "secret"}],
                "allowed_models": ["chat-default"],
                "voice_sample": "sample bytes",
            },
            "upload": {"image_path": "/tmp/private.png"},
        }

        redacted = audit.redact_audit_metadata(payload)

        self.assertEqual(redacted["api_key"], audit.REDACTED)
        self.assertEqual(redacted["key_prefix"], "b1k_public")
        self.assertEqual(redacted["prompt"], audit.REDACTED)
        self.assertEqual(redacted["model"], "chat-default")
        self.assertEqual(redacted["nested"]["messages"], audit.REDACTED)
        self.assertEqual(redacted["nested"]["allowed_models"], ["chat-default"])
        self.assertEqual(redacted["nested"]["voice_sample"], audit.REDACTED)
        self.assertEqual(redacted["upload"]["image_path"], audit.REDACTED)

    def test_make_audit_event_adds_identity_and_redacts_metadata(self) -> None:
        created = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

        event = audit.make_audit_event(
            event_type="api_client.created",
            actor_id="admin",
            actor_role="admin",
            actor_key_prefix="b1k_public",
            target_type="api_client",
            target_id="client_1",
            summary="Created API client",
            metadata={"display_name": "external", "token": "secret"},
            created_at=created,
            event_id="audit_1",
        )

        self.assertEqual(event["id"], "audit_1")
        self.assertEqual(event["created_at"], created)
        self.assertEqual(event["metadata"]["display_name"], "external")
        self.assertEqual(event["metadata"]["token"], audit.REDACTED)

        public = audit.public_audit_event(event)
        self.assertEqual(public["created_at"], created.isoformat())

    def test_event_type_is_required(self) -> None:
        with self.assertRaises(ValueError):
            audit.make_audit_event(event_type="", actor_id="admin", actor_role="admin")

    def test_log_redaction_handles_nested_payloads_and_token_like_strings(self) -> None:
        fields = audit.redact_log_fields(
            {
                "model": "chat-default",
                "Authorization": "Bearer b1k_public.secret",
                "upstream_url": "https://runtime/ws?clientId=ok&token=secret-token&api_key=secret-key",
                "nested": {
                    "prompt": "private prompt",
                    "safe": "Authorization: Bearer secret-value",
                    "url": "https://example.invalid/path?credential=abc123",
                },
                "items": [{"voice_sample": "bytes"}, "github_pat_secretvalue"],
            }
        )

        encoded = json.dumps(fields)
        self.assertEqual(fields["model"], "chat-default")
        self.assertEqual(fields["Authorization"], audit.REDACTED)
        self.assertEqual(fields["nested"]["prompt"], audit.REDACTED)
        self.assertEqual(fields["items"][0]["voice_sample"], audit.REDACTED)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("secret-key", encoded)
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn("abc123", encoded)
        self.assertNotIn("github_pat_secretvalue", encoded)
        self.assertIn(audit.REDACTED, encoded)

    def test_log_redaction_bounds_long_strings_and_lists(self) -> None:
        redacted = audit.redact_log_fields({"safe": "x" * (audit.LOG_MAX_STRING_LENGTH + 20), "items": list(range(audit.MAX_LIST_ITEMS + 2))})

        self.assertTrue(redacted["safe"].endswith("...<truncated>"))
        self.assertEqual(redacted["items"][-1], {"truncated_items": 2})


@unittest.skipIf(main is None, f"{MISSING_MAIN_DEPENDENCY} is not installed in this lightweight test environment")
class ControlPlaneLogEventTests(unittest.TestCase):
    def test_log_event_emits_redacted_json(self) -> None:
        messages: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = CaptureHandler()
        main.LOG.addHandler(handler)
        original_level = main.LOG.level
        original_propagate = main.LOG.propagate
        main.LOG.setLevel(logging.INFO)
        main.LOG.propagate = False
        self.addCleanup(lambda: main.LOG.removeHandler(handler))
        self.addCleanup(lambda: main.LOG.setLevel(original_level))
        self.addCleanup(lambda: setattr(main.LOG, "propagate", original_propagate))

        main.log_event(
            "sensitive_test",
            prompt="private prompt",
            upstream_url="https://runtime/ws?token=secret-token",
            metadata={"api_key": "secret-key", "model": "chat-default"},
        )

        self.assertEqual(len(messages), 1)
        payload: dict[str, Any] = json.loads(messages[0])
        self.assertEqual(payload["event"], "sensitive_test")
        self.assertEqual(payload["prompt"], audit.REDACTED)
        self.assertEqual(payload["metadata"]["api_key"], audit.REDACTED)
        self.assertEqual(payload["metadata"]["model"], "chat-default")
        encoded = json.dumps(payload)
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("secret-key", encoded)


if __name__ == "__main__":
    unittest.main()
