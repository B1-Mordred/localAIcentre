from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230013"
down_revision = "202607230012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_model_alias_policies ADD COLUMN IF NOT EXISTS modality varchar(64)"))
    op.execute(text("ALTER TABLE b1_model_alias_policies ADD COLUMN IF NOT EXISTS status varchar(64)"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
