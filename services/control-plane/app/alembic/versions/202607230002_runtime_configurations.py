from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app import database


revision = "202607230002"
down_revision = "202607230001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    database.runtime_configurations.create(bind=bind, checkfirst=True)
    op.execute(text("CREATE INDEX IF NOT EXISTS b1_runtime_configurations_enabled_idx ON b1_runtime_configurations (enabled)"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
