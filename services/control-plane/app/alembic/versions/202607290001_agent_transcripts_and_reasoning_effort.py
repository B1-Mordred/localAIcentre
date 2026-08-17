from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "202607290001"
down_revision = "202607230015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE b1_model_alias_policies ADD COLUMN IF NOT EXISTS reasoning_effort varchar(16)"))
    op.execute(
        text(
            "CREATE TABLE IF NOT EXISTS b1_agent_transcripts ("
            "conversation_id varchar(128) NOT NULL, "
            "owner_id varchar(128) NOT NULL, "
            "model_alias varchar(128) NOT NULL, "
            "transcript_envelope jsonb NOT NULL DEFAULT '{}'::jsonb, "
            "message_count integer NOT NULL DEFAULT 0, "
            "expires_at timestamp with time zone NOT NULL, "
            "created_at timestamp with time zone NOT NULL, "
            "updated_at timestamp with time zone NOT NULL, "
            "PRIMARY KEY (conversation_id, owner_id)"
            ")"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS b1_agent_transcripts_owner_expiry_idx ON b1_agent_transcripts (owner_id, expires_at)"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS b1_agent_transcripts_owner_expiry_idx"))
    op.execute(text("DROP TABLE IF EXISTS b1_agent_transcripts"))
    op.execute(text("ALTER TABLE b1_model_alias_policies DROP COLUMN IF EXISTS reasoning_effort"))
