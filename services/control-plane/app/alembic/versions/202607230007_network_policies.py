from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230007"
down_revision = "202607230006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE TABLE IF NOT EXISTS b1_network_policies ("
            "id varchar(64) PRIMARY KEY, "
            "cors_allow_origins jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "trusted_proxy_cidrs jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "updated_by varchar(128), "
            "created_at timestamp with time zone NOT NULL, "
            "updated_at timestamp with time zone NOT NULL"
            ")"
        )
    )


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
