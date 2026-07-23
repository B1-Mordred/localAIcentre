from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230004"
down_revision = "202607230003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_model_downloads ADD COLUMN IF NOT EXISTS credential_secret_name varchar(128)"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
