from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import compose_override  # noqa: E402


GOOD_DIGEST = "b" * 64


class ComposeOverrideTests(unittest.TestCase):
    def image_refs(self) -> list[dict[str, str]]:
        return [
            {"service": "control-plane", "image": f"ghcr.io/b1/control-plane:0.2.0@sha256:{GOOD_DIGEST}"},
            {"service": "gateway", "image": f"caddy:2.10.2-alpine@sha256:{GOOD_DIGEST}"},
        ]

    def test_render_override_sets_images_and_disables_build_contexts(self) -> None:
        rendered = compose_override.render_compose_image_override(
            update_id="update_abcdef123456",
            target_version="0.2.0",
            image_refs=self.image_refs(),
            created_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )

        self.assertIn("services:", rendered)
        self.assertIn("  control-plane:", rendered)
        self.assertIn(f"image: \"ghcr.io/b1/control-plane:0.2.0@sha256:{GOOD_DIGEST}\"", rendered)
        self.assertIn("    build: null", rendered)
        self.assertIn("# Update: update_abcdef123456", rendered)

    def test_write_override_records_checksum_and_dry_run_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = compose_override.write_compose_image_override(
                data_root=root,
                update_id="update_abcdef123456",
                target_version="0.2.0",
                image_refs=self.image_refs(),
                image_stage=[
                    {"service": "control-plane", "status": "dry_run"},
                    {"service": "gateway", "status": "ok"},
                ],
                created_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )
            path = Path(metadata["path"])
            digest = compose_override.sha256_file(path)

            self.assertEqual(metadata["format"], "b1-ai-hub-compose-image-override/v1")
            self.assertEqual(metadata["relative_path"], "data/control-plane/updates/update_abcdef123456/compose.images.yaml")
            self.assertFalse(metadata["ready_for_promotion"])
            self.assertTrue(metadata["requires_image_pull_before_promotion"])
            self.assertEqual(metadata["not_pulled_services"], ["control-plane"])
            self.assertEqual(metadata["sha256"], digest)
            self.assertIn("docker compose -f compose.yaml -f", metadata["usage"])

    def test_rejects_bad_update_id_and_unpinned_images(self) -> None:
        with self.assertRaises(compose_override.ComposeOverrideError):
            compose_override.render_compose_image_override(
                update_id="../bad",
                target_version="0.2.0",
                image_refs=self.image_refs(),
            )
        with self.assertRaises(compose_override.ComposeOverrideError):
            compose_override.render_compose_image_override(
                update_id="update_abcdef123456",
                target_version="0.2.0",
                image_refs=[{"service": "control-plane", "image": "ghcr.io/b1/control-plane:latest"}],
            )


if __name__ == "__main__":
    unittest.main()
