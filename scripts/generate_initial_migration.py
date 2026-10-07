"""Development utility: render a frozen initial migration from an empty SQLite database."""

from pathlib import Path

from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

engine = create_engine("sqlite://")
with engine.connect() as connection:
    migration = produce_migrations(MigrationContext.configure(connection), Base.metadata)
upgrade = render_python_code(migration.upgrade_ops)
downgrade = render_python_code(migration.downgrade_ops)
Path("alembic/versions").mkdir(parents=True, exist_ok=True)
Path("alembic/versions/0001_initial.py").write_text(
    '"""Initial persistent monitoring schema."""\nfrom alembic import op\nimport sqlalchemy as sa\n\n'
    'revision = "0001"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n\n'
    f"def upgrade():\n{upgrade}\n\ndef downgrade():\n{downgrade}\n",
    encoding="utf-8",
)
