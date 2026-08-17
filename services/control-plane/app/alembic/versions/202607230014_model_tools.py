from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607230014"
down_revision = "202607230013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE TABLE IF NOT EXISTS b1_model_tool_policy ("
            "id varchar(64) PRIMARY KEY, "
            "enabled boolean NOT NULL DEFAULT true, "
            "allowed_tools jsonb NOT NULL DEFAULT '[\"web_search\", \"web_fetch\"]'::jsonb, "
            "allow_private_network boolean NOT NULL DEFAULT false, "
            "allowed_hosts jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "max_result_chars integer NOT NULL DEFAULT 12000, "
            "max_search_results integer NOT NULL DEFAULT 5, "
            "timeout_seconds double precision NOT NULL DEFAULT 12.0, "
            "search_endpoint_template text NOT NULL DEFAULT 'https://duckduckgo.com/html/?q={query}', "
            "notes text NOT NULL DEFAULT '', "
            "updated_by varchar(128), "
            "created_at timestamp with time zone NOT NULL, "
            "updated_at timestamp with time zone NOT NULL"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE TABLE IF NOT EXISTS b1_model_tool_definitions ("
            "name varchar(128) PRIMARY KEY, "
            "enabled boolean NOT NULL DEFAULT true, "
            "kind varchar(64) NOT NULL, "
            "display_name varchar(256) NOT NULL, "
            "description text NOT NULL DEFAULT '', "
            "parameters_schema jsonb NOT NULL DEFAULT '{}'::jsonb, "
            "config jsonb NOT NULL DEFAULT '{}'::jsonb, "
            "visibility_roles jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "notes text NOT NULL DEFAULT '', "
            "updated_by varchar(128), "
            "created_at timestamp with time zone NOT NULL, "
            "updated_at timestamp with time zone NOT NULL"
            ")"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS b1_model_tool_definitions_enabled_idx ON b1_model_tool_definitions (enabled)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS b1_model_tool_definitions_kind_idx ON b1_model_tool_definitions (kind)"))


def downgrade() -> None:
    raise RuntimeError("B1 AI Hub production schema downgrades are disabled; restore a verified backup instead")
