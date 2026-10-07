import os
import uuid

import pytest_asyncio
from sqlalchemy import text

from app.models import Base, ProviderRecord, ServiceRecord, User
from app.repository import Repository


@pytest_asyncio.fixture
async def repo(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    repository = Repository(url or "sqlite+aiosqlite:///" + str(tmp_path / "state.db"))
    schema = "test_" + uuid.uuid4().hex if url else None
    if schema:
        async with repository.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        repository.engine = repository.engine.execution_options(schema_translate_map={None: schema})
        repository.sessions.configure(bind=repository.engine)
    async with repository.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with repository.sessions.begin() as session:
        session.add_all(
            [
                ProviderRecord(id="dmsu", data={}),
                ServiceRecord(id="passport", name="Паспорт"),
                User(id=5555555555, chat_id=5555555555),
                User(id=6666666666, chat_id=6666666666),
            ]
        )
    try:
        yield repository
    finally:
        if schema:
            async with repository.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await repository.close()
