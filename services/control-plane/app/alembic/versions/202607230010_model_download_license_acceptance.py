from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230010"
down_revision = "202607230009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_model_downloads ADD COLUMN IF NOT EXISTS license_accepted boolean NOT NULL DEFAULT false"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
