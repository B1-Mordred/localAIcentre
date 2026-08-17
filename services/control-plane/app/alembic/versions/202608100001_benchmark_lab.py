"""add persistent benchmark lab

Revision ID: 202608100001
Revises: 202607290001
"""
from alembic import op

revision = "202608100001"
down_revision = "202607290001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in """
    CREATE TABLE b1_benchmark_suites (id varchar(128) NOT NULL, version varchar(64) NOT NULL, display_name varchar(256) NOT NULL, modality varchar(64) NOT NULL, status varchar(32) NOT NULL DEFAULT 'draft', definition jsonb NOT NULL DEFAULT '{}'::jsonb, content_sha256 varchar(64) NOT NULL, created_by varchar(128), created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, published_at timestamptz, PRIMARY KEY(id, version));
    CREATE TABLE b1_benchmark_profiles (id varchar(128) NOT NULL, version varchar(64) NOT NULL, display_name varchar(256) NOT NULL, status varchar(32) NOT NULL DEFAULT 'published', definition jsonb NOT NULL DEFAULT '{}'::jsonb, content_sha256 varchar(64) NOT NULL, created_by varchar(128), created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, PRIMARY KEY(id, version));
    CREATE TABLE b1_benchmark_campaigns (id varchar(64) PRIMARY KEY, owner_id varchar(128) NOT NULL, suite_id varchar(128) NOT NULL, suite_version varchar(64) NOT NULL, profile_id varchar(128) NOT NULL, profile_version varchar(64) NOT NULL, status varchar(32) NOT NULL DEFAULT 'draft', stage varchar(128) NOT NULL DEFAULT 'draft', progress integer NOT NULL DEFAULT 0, candidates jsonb NOT NULL DEFAULT '[]'::jsonb, judge_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb, device_groups jsonb NOT NULL DEFAULT '[]'::jsonb, summary jsonb NOT NULL DEFAULT '{}'::jsonb, failure_message text, raw_expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, started_at timestamptz, completed_at timestamptz);
    CREATE TABLE b1_benchmark_results (id varchar(64) PRIMARY KEY, campaign_id varchar(64) NOT NULL, candidate_ref varchar(256) NOT NULL, case_id varchar(128) NOT NULL, attempt integer NOT NULL DEFAULT 1, status varchar(32) NOT NULL DEFAULT 'pending', job_id varchar(64), output jsonb NOT NULL DEFAULT '{}'::jsonb, metrics jsonb NOT NULL DEFAULT '{}'::jsonb, error_message text, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, completed_at timestamptz);
    CREATE TABLE b1_benchmark_reviews (id varchar(64) PRIMARY KEY, campaign_id varchar(64) NOT NULL, reviewer_id varchar(128) NOT NULL, blind_token varchar(128) NOT NULL, result_a_id varchar(64) NOT NULL, result_b_id varchar(64) NOT NULL, winner varchar(16), scores jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL);
    CREATE TABLE b1_benchmark_reports (id varchar(64) PRIMARY KEY, campaign_id varchar(64) NOT NULL, status varchar(32) NOT NULL, summary jsonb NOT NULL DEFAULT '{}'::jsonb, files jsonb NOT NULL DEFAULT '{}'::jsonb, created_by varchar(128), created_at timestamptz NOT NULL);
    CREATE TABLE b1_benchmark_judge_policy (id varchar(64) PRIMARY KEY, secret_name varchar(128), top_models jsonb NOT NULL DEFAULT '[]'::jsonb, codex_model varchar(256), overrides jsonb NOT NULL DEFAULT '{}'::jsonb, catalog_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb, updated_by varchar(128), created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL);
    CREATE TABLE b1_benchmark_device_locks (device_group varchar(64) PRIMARY KEY, campaign_id varchar(64) NOT NULL, owner_id varchar(128) NOT NULL, status varchar(32) NOT NULL DEFAULT 'active', expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL);
    CREATE INDEX b1_benchmark_campaigns_status_idx ON b1_benchmark_campaigns(status);
    CREATE INDEX b1_benchmark_results_campaign_idx ON b1_benchmark_results(campaign_id);
    CREATE INDEX b1_benchmark_reviews_campaign_idx ON b1_benchmark_reviews(campaign_id);
    CREATE UNIQUE INDEX b1_benchmark_reviews_blind_uq ON b1_benchmark_reviews(reviewer_id, blind_token);
    CREATE INDEX b1_benchmark_reports_campaign_idx ON b1_benchmark_reports(campaign_id);
    """.strip().split(";\n"):
        op.execute(statement)


def downgrade() -> None:
    for table in ("b1_benchmark_device_locks", "b1_benchmark_judge_policy", "b1_benchmark_reports", "b1_benchmark_reviews", "b1_benchmark_results", "b1_benchmark_campaigns", "b1_benchmark_profiles", "b1_benchmark_suites"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
