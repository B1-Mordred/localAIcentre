from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230008"
down_revision = "202607230007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE TABLE IF NOT EXISTS b1_comfyui_node_pins ("
            "node_id varchar(128) NOT NULL, "
            "commit varchar(40) NOT NULL, "
            "repository_url text NOT NULL, "
            "display_name varchar(256), "
            "status varchar(32) NOT NULL DEFAULT 'approved', "
            "approved_by varchar(128), "
            "approved_at timestamp with time zone, "
            "dependency_lock_sha256 varchar(64), "
            "allowed_route_prefixes jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "notes text, "
            "created_at timestamp with time zone NOT NULL, "
            "updated_at timestamp with time zone NOT NULL, "
            "PRIMARY KEY (node_id, commit)"
            ")"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS b1_comfyui_node_pins_status_idx ON b1_comfyui_node_pins (status)"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
