import os
import time
from types import SimpleNamespace

import aiohttp
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.main import serve_health
from app.models import Base
from app.repository import Repository


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires PostgreSQL")
async def test_postgres_migration_and_session_lock():
    repo = Repository(os.environ["TEST_DATABASE_URL"])
    try:
        async with repo.engine.connect() as first, repo.engine.connect() as second:
            assert await first.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
            differences = await first.run_sync(
                lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
            )
            assert not differences
            assert await first.scalar(text("SELECT pg_try_advisory_lock(739194621)"))
            await first.commit()
            assert not await second.scalar(text("SELECT pg_try_advisory_lock(739194621)"))
        # Returning a connection to the pool does not release a session-level lock.
        # Process shutdown disposes the pool, releasing the physical connection.
        await repo.close()
        async with repo.engine.connect() as replacement:
            assert await replacement.scalar(text("SELECT pg_try_advisory_lock(739194621)"))
            await replacement.execute(text("SELECT pg_advisory_unlock(739194621)"))
    finally:
        await repo.close()


async def test_health_metrics_and_stalled_worker(repo):
    scheduler = SimpleNamespace(heartbeats={"dmsu": time.monotonic()})
    runner = await serve_health(repo, scheduler, SimpleNamespace(metrics_port=0))
    try:
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                assert response.status == 200
                assert (await response.json())["status"] == "ok"
            async with client.get(f"http://127.0.0.1:{port}/metrics") as response:
                assert response.status == 200
                assert "queue_" in await response.text()
            scheduler.heartbeats["dmsu"] -= 601
            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                assert response.status == 503
                assert (await response.json())["status"] == "worker_stalled"
    finally:
        await runner.cleanup()
