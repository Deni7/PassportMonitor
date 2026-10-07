import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base


def test_initial_migration_matches_models_and_can_rollback(tmp_path):
    path = tmp_path / "migrations.db"
    environment = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///" + str(path)}
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "alembic"]
    subprocess.run(
        command + ["upgrade", "head"], env=environment, cwd=root, check=True, capture_output=True
    )
    engine = create_engine("sqlite:///" + str(path))
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()
    subprocess.run(
        command + ["downgrade", "base"], env=environment, cwd=root, check=True, capture_output=True
    )
