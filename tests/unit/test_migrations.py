from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_ROOT = ROOT / "services" / "control-plane"
APP_ROOT = CONTROL_PLANE_ROOT / "app"


class MigrationPackagingTests(unittest.TestCase):
    def test_control_plane_image_runs_migration_wrapper(self) -> None:
        dockerfile = (CONTROL_PLANE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('CMD ["python", "-m", "app.run"]', dockerfile)
        self.assertIn("postgresql-client", dockerfile)
        self.assertIn("alembic==1.16.4", (CONTROL_PLANE_ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertTrue((APP_ROOT / "alembic" / "script.py.mako").is_file())

    def test_compose_exposes_migration_toggle(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("B1_DB_MIGRATIONS_ENABLED: ${B1_DB_MIGRATIONS_ENABLED:-true}", compose)
        self.assertIn("B1_DB_MIGRATIONS_ENABLED=true", env_example)

    def test_fastapi_startup_verifies_schema_instead_of_creating_it(self) -> None:
        source = (APP_ROOT / "main.py").read_text(encoding="utf-8")
        startup_start = source.index('async def startup() -> None:')
        startup_end = source.index('@app.on_event("shutdown")', startup_start)
        startup_source = source[startup_start:startup_end]
        self.assertIn("await database.verify_schema_current()", startup_source)
        self.assertNotIn("await database.init_schema()", startup_source)


try:
    sys.path.insert(0, str(CONTROL_PLANE_ROOT))
    from app import database  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "sqlalchemy":
        raise
    database = None


@unittest.skipIf(database is None, "SQLAlchemy is not installed in this lightweight test environment")
class MigrationSchemaTests(unittest.TestCase):
    def test_expected_schema_snapshot_covers_export_tables(self) -> None:
        snapshot = database.expected_schema_snapshot()
        exported = {table.name for table in database.EXPORT_TABLES}

        self.assertTrue(exported.issubset(snapshot))
        self.assertIn("idempotency_key", snapshot["b1_jobs"])
        self.assertIn("image_stage", snapshot["b1_update_plans"])
        self.assertIn("compose_override", snapshot["b1_update_plans"])
        self.assertIn("b1_runtime_configurations", snapshot)
        self.assertIn("api_key_secret_name", snapshot["b1_runtime_configurations"])
        self.assertIn("b1_model_alias_policies", snapshot)
        self.assertIn("preferred_runtime", snapshot["b1_model_alias_policies"])
        self.assertIn("visibility_roles", snapshot["b1_model_alias_policies"])
        self.assertIn("credential_secret_name", snapshot["b1_model_downloads"])

    def test_schema_compatibility_sql_is_shared_with_initial_revision(self) -> None:
        revision_source = (APP_ROOT / "alembic" / "versions" / "202607230001_initial_control_plane_schema.py").read_text(encoding="utf-8")

        self.assertIn("database.metadata.create_all", revision_source)
        self.assertIn("database.SCHEMA_COMPATIBILITY_SQL", revision_source)
        self.assertTrue(any("ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS image_stage" in sql for sql in database.SCHEMA_COMPATIBILITY_SQL))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS b1_runtime_configurations" in sql for sql in database.SCHEMA_COMPATIBILITY_SQL))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS b1_model_alias_policies" in sql for sql in database.SCHEMA_COMPATIBILITY_SQL))
        self.assertTrue(any("CREATE UNIQUE INDEX IF NOT EXISTS b1_jobs_owner_idempotency_key_uq" in sql for sql in database.SCHEMA_COMPATIBILITY_SQL))


try:
    migrate_spec = importlib.util.find_spec("app.migrate")
    alembic_spec = importlib.util.find_spec("alembic.script")
    if migrate_spec is None or alembic_spec is None:
        raise ModuleNotFoundError("alembic")
    from alembic.script import ScriptDirectory  # noqa: E402
    from app import migrate  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "alembic":
        raise
    migrate = None
    ScriptDirectory = None  # type: ignore[assignment]


@unittest.skipIf(migrate is None, "Alembic is not installed in this lightweight test environment")
class AlembicConfigTests(unittest.TestCase):
    def test_alembic_config_points_at_packaged_async_migrations(self) -> None:
        config = migrate.alembic_config("postgresql+asyncpg://user:pass@postgres:5432/b1_ai_hub")

        self.assertEqual(config.get_main_option("sqlalchemy.url"), "postgresql+asyncpg://user:pass@postgres:5432/b1_ai_hub")
        self.assertEqual(Path(config.get_main_option("script_location")), APP_ROOT / "alembic")
        scripts = ScriptDirectory.from_config(config)
        self.assertEqual(scripts.get_current_head(), "202607230004")


if __name__ == "__main__":
    unittest.main()
