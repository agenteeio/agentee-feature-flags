"""SQLAlchemy-to-asyncpg pool adapter for FeatureFlagService.

Communication-agent uses SQLAlchemy AsyncSession / async_sessionmaker instead
of a raw asyncpg pool.  This adapter wraps an SQLAlchemy async session factory
(``async_sessionmaker`` or any callable returning an ``AsyncSession``) to
expose the asyncpg-pool interface that FeatureFlagService expects:

    pool.acquire() → async context manager → connection-like object with
        .fetchrow(sql, *args) → dict | None
        .fetch(sql, *args)    → list[dict]

Usage::

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from feature_flags import FeatureFlagService
    from feature_flags._sqla_adapter import SQLAPoolAdapter

    ff_service = FeatureFlagService(SQLAPoolAdapter(async_session_maker))

The adapter translates ``SELECT`` results from SQLAlchemy ``Row`` objects to
plain dicts so FeatureFlagService can read them as ``row["column"]``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any


class _SQLAConnection:
    """Thin wrapper around an open SQLAlchemy AsyncSession.

    Exposes .fetchrow() and .fetch() with asyncpg semantics.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """Execute *sql* with positional *args* and return the first row as a dict.

        Returns None when no rows match.
        PostgreSQL ``$1, $2, ...`` placeholders are rewritten to ``:p1, :p2, ...``
        for SQLAlchemy's text() binding syntax.
        """
        from sqlalchemy import text

        sqla_sql, params = _pg_to_sqla(sql, args)
        result = await self._session.execute(text(sqla_sql), params)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Execute *sql* with positional *args* and return all rows as a list of dicts."""
        from sqlalchemy import text

        sqla_sql, params = _pg_to_sqla(sql, args)
        result = await self._session.execute(text(sqla_sql), params)
        return [dict(row) for row in result.mappings().all()]


def _pg_to_sqla(sql: str, args: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    """Rewrite ``$1, $2, ...`` placeholders to ``:p1, :p2, ...`` for SQLAlchemy.

    Only rewrites positional parameter markers — does not parse SQL structure.
    """
    params: dict[str, Any] = {}
    result = sql
    for i, val in enumerate(args, start=1):
        placeholder = f":p{i}"
        result = result.replace(f"${i}", placeholder, 1)
        params[f"p{i}"] = val
    return result, params


class SQLAPoolAdapter:
    """Wraps a SQLAlchemy ``async_sessionmaker`` as an asyncpg-pool-compatible object.

    FeatureFlagService calls ``pool.acquire()`` as an async context manager.
    This adapter provides that interface using SQLAlchemy async sessions.
    """

    def __init__(self, session_factory: Any) -> None:
        """
        Args:
            session_factory: A callable (typically ``async_sessionmaker``) that
                returns an SQLAlchemy ``AsyncSession`` when called.  Can also be
                a raw ``AsyncSession`` for single-session use in tests.
        """
        self._factory = session_factory

    @asynccontextmanager  # type: ignore[misc]
    async def acquire(self) -> Any:
        """Yield a ``_SQLAConnection`` backed by a fresh AsyncSession."""
        # Support both session factories (async_sessionmaker) and
        # raw AsyncSession objects (useful in tests).
        if callable(self._factory):
            session = self._factory()
        else:
            session = self._factory

        async with session as s:
            yield _SQLAConnection(s)
