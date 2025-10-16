# api/app/db.py
from __future__ import annotations

from typing import AsyncGenerator   # <- importa o tipo correto
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from .config import ASYNC_DATABASE_URL

if not ASYNC_DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida (verifique api/.env)")

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency do FastAPI para abrir/fechar sessão assíncrona."""
    async with SessionLocal() as session:
        yield session

__all__ = ["engine", "SessionLocal", "get_session"]
