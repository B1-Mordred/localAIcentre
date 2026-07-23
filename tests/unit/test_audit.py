from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import audit  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
