from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230012"
down_revision = "202607230011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS promotion_result jsonb NOT NULL DEFAULT '{}'::jsonb"))
    op.execute(text("ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS promotion_requested_at timestamp with time zone"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
