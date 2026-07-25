from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230009"
down_revision = "202607230008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_resource_policies ADD COLUMN IF NOT EXISTS cpu_residency_enabled boolean NOT NULL DEFAULT true"))
    op.execute(text("ALTER TABLE b1_resource_policies ADD COLUMN IF NOT EXISTS cpu_residency_max_ram_gib double precision NOT NULL DEFAULT 2.0"))
    op.execute(
        text(
            "ALTER TABLE b1_resource_policies ADD COLUMN IF NOT EXISTS cpu_resident_aliases "
            "jsonb NOT NULL DEFAULT '[\"embedding-default\", \"tts-fast\", \"stt-default\"]'::jsonb"
        )
    )


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
