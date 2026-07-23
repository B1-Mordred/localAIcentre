from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app import database


revision = "202607230001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    database.metadata.create_all(bind=bind)
    for statement in database.SCHEMA_COMPATIBILITY_SQL:
        op.execute(text(statement))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
