from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230011"
down_revision = "202607230010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_runtime_reservations ADD COLUMN IF NOT EXISTS idempotency_key varchar(256)"))
    op.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS b1_runtime_reservations_owner_idempotency_key_uq "
            "ON b1_runtime_reservations (owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    )


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
