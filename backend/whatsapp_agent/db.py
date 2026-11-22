from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from whatsapp_agent.config import get_settings


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://", "postgresql+asyncpg://", 1
        )
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    if database_url.startswith("postgresql+asyncpg://"):
        url = make_url(database_url)
        query = {
            key: value
            for key, value in url.query.items()
            if key != "channel_binding"
        }
        return url.set(query=query).render_as_string(hide_password=False)
    return database_url


def _remove_asyncpg_channel_binding(
    _dialect: object,
    _connection_record: object,
    _cargs: tuple[object, ...],
    cparams: dict[str, object],
) -> None:
    cparams.pop("channel_binding", None)


database_url = async_database_url(str(get_settings().database_url))
engine_options: dict[str, object] = {"pool_pre_ping": True}
if database_url.startswith("postgresql+asyncpg://"):
    engine_options.update(pool_size=1, max_overflow=0, pool_recycle=300)

engine = create_async_engine(database_url, **engine_options)
if database_url.startswith("postgresql+asyncpg://"):
    event.listen(
        engine.sync_engine,
        "do_connect",
        _remove_asyncpg_channel_binding,
    )

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session
