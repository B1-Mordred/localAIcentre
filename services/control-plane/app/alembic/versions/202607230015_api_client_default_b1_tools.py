from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230015"
down_revision = "202607230014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_api_clients ADD COLUMN IF NOT EXISTS default_b1_tools jsonb NOT NULL DEFAULT '[]'::jsonb"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
