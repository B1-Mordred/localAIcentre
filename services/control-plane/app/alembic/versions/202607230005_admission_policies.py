from __future__ import annotations

from alembic import op

from app import database


revision = "202607230005"
down_revision = "202607230004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    database.admission_policies.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
