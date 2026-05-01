"""Unit tests for feature_flags.service.FeatureFlagService — 100% coverage.

Resolution truth-table:
    | global | mailbox | result |
    |--------|---------|--------|
    |   -    |   any   | False  |  (row absent)
    |  OFF   |   any   | False  |  (global disabled)
    |   ON   |   N/A   | True   |  (no mailbox supplied, global ON)
    |   ON   |    -    | False  |  (mailbox row absent)
    |   ON   |  OFF    | False  |  (mailbox row disabled)
    |   ON   |   ON    | True   |  (both enabled)

Covers:
    - is_enabled() — all truth-table paths
    - get_config() — shallow merge, empty-when-disabled
    - cache hit (no DB call on second request)
    - cache expiry triggers fresh DB call
    - DB error → False, NOT cached
    - mailbox case-insensitivity
    - SQLAPoolAdapter (in-memory aiosqlite via SQLAlchemy)
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from feature_flags.service import _FLAG_CACHE, FeatureFlagService, clear_flag_cache

# ---------------------------------------------------------------------------
# asyncpg-style mock pool builder
# ---------------------------------------------------------------------------


def _row(enabled: bool, config: dict | None = None) -> dict:
    return {"enabled": enabled, "config": config or {}}


def _make_pool(
    *,
    tenant_row: dict | None = None,
    mailbox_row: dict | None = None,
) -> Any:
    """Build a minimal asyncpg-pool mock for FeatureFlagService."""

    async def _fetchrow(sql: str, *args: Any) -> dict | None:
        sql_lower = sql.lower()
        if "mailbox_address = ''" in sql_lower or "mailbox_address=''" in sql_lower:
            return tenant_row
        # Per-mailbox lookup (lower(...) = lower(...))
        if "lower(mailbox_address)" in sql_lower:
            return mailbox_row
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    @asynccontextmanager
    async def _acquire_impl() -> Any:
        yield conn

    pool = MagicMock()
    # Use a MagicMock wrapper so call_count is trackable; delegate to the
    # asynccontextmanager via side_effect so `async with pool.acquire()` works.
    pool.acquire = MagicMock(side_effect=lambda: _acquire_impl())
    return pool


# ---------------------------------------------------------------------------
# Truth-table: is_enabled()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tenant_row_returns_false() -> None:
    """Global row absent → False regardless of mailbox."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=None))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_tenant_row_disabled_returns_false() -> None:
    """Global row enabled=False → False (kill-switch)."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_global_on_no_mailbox_returns_true() -> None:
    """Global ON and no mailbox supplied → True (mailbox gate skipped)."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1") is True


@pytest.mark.asyncio
async def test_global_on_mailbox_row_absent_returns_false() -> None:
    """Global ON but mailbox row absent → False (mailbox not opted in)."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=None))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_global_on_mailbox_disabled_returns_false() -> None:
    """Global ON but mailbox row enabled=False → False."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_both_on_returns_true() -> None:
    """Global ON + mailbox ON → True."""
    clear_flag_cache()
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is True


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_cache_key_is_lowercased() -> None:
    """Cache key uses lower(mailbox) so mixed-case matches the same entry."""
    clear_flag_cache()
    pool = _make_pool(tenant_row=_row(True), mailbox_row=_row(True))
    svc = FeatureFlagService(pool)

    r1 = await svc.is_enabled("f", "INBOX@TEST.DE")
    r2 = await svc.is_enabled("f", "inbox@test.de")
    assert r1 is True
    assert r2 is True  # served from cache — same lowercase key

    # Only one DB fetch per unique flag+mailbox key (lower).
    # The pool was called once (for the first is_enabled call).
    ctx_mgr_calls = pool.acquire.call_count  # type: ignore[attr-defined]
    assert ctx_mgr_calls == 1


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_db() -> None:
    """Second call for same (flag, mailbox) returns cached value — no DB call."""
    clear_flag_cache()
    pool = _make_pool(tenant_row=_row(True), mailbox_row=_row(True))
    svc = FeatureFlagService(pool)

    await svc.is_enabled("flag_x", "inbox@x.de")
    first_call_count = pool.acquire.call_count  # type: ignore[attr-defined]

    # Second call — pool.acquire must NOT be called again.
    await svc.is_enabled("flag_x", "inbox@x.de")
    assert pool.acquire.call_count == first_call_count  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_expired_cache_triggers_fresh_db_call() -> None:
    """After TTL expires, the next call re-queries the DB."""
    clear_flag_cache()
    pool = _make_pool(tenant_row=_row(True), mailbox_row=_row(True))
    svc = FeatureFlagService(pool)

    await svc.is_enabled("flag_ttl", "inbox@ttl.de")
    first_call_count = pool.acquire.call_count  # type: ignore[attr-defined]

    # Manually expire the cache entry.
    key = ("flag_ttl", "inbox@ttl.de")
    _FLAG_CACHE[key] = (True, {}, time.monotonic() - 1.0)

    await svc.is_enabled("flag_ttl", "inbox@ttl.de")
    assert pool.acquire.call_count > first_call_count  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DB error → fail-closed, not cached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_error_returns_false_and_is_not_cached() -> None:
    """DB error → return False; result NOT cached so flag recovers on next call."""
    clear_flag_cache()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB unavailable"))

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire

    svc = FeatureFlagService(pool)
    result = await svc.is_enabled("flag_err", "inbox@err.de")
    assert result is False

    # Nothing in cache — next call will retry DB.
    key = ("flag_err", "inbox@err.de")
    assert key not in _FLAG_CACHE


# ---------------------------------------------------------------------------
# get_config()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_shallow_merge() -> None:
    """get_config returns tenant defaults with mailbox overrides merged in."""
    clear_flag_cache()
    tenant_cfg = {"threshold": 0.9, "mode": "strict"}
    mailbox_cfg = {"threshold": 0.7}  # overrides tenant threshold
    pool = _make_pool(
        tenant_row=_row(True, tenant_cfg),
        mailbox_row=_row(True, mailbox_cfg),
    )
    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag", "inbox@test.de")
    assert cfg == {"threshold": 0.7, "mode": "strict"}


@pytest.mark.asyncio
async def test_get_config_tenant_only_when_no_mailbox() -> None:
    """get_config with no mailbox returns tenant config only."""
    clear_flag_cache()
    tenant_cfg = {"x": 1}
    pool = _make_pool(tenant_row=_row(True, tenant_cfg))
    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag")
    assert cfg == {"x": 1}


@pytest.mark.asyncio
async def test_get_config_disabled_returns_empty() -> None:
    """get_config returns {} when flag is disabled."""
    clear_flag_cache()
    pool = _make_pool(tenant_row=_row(False))
    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag", "inbox@test.de")
    assert cfg == {}


@pytest.mark.asyncio
async def test_get_config_db_error_returns_empty() -> None:
    """get_config returns {} on DB error."""
    clear_flag_cache()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire

    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag", "inbox@x.de")
    assert cfg == {}


# ---------------------------------------------------------------------------
# _to_dict() helper
# ---------------------------------------------------------------------------


def test_to_dict_with_dict_passthrough() -> None:
    """_to_dict passes a dict through unchanged."""
    from feature_flags.service import _to_dict

    d = {"a": 1, "b": [1, 2]}
    assert _to_dict(d) is d


def test_to_dict_with_json_string() -> None:
    """_to_dict parses a JSON object string."""
    from feature_flags.service import _to_dict

    assert _to_dict('{"k": 1}') == {"k": 1}


def test_to_dict_with_invalid_json_returns_empty() -> None:
    """_to_dict returns {} for unparseable strings."""
    from feature_flags.service import _to_dict

    assert _to_dict("{bad json}") == {}


def test_to_dict_with_json_non_object_returns_empty() -> None:
    """_to_dict returns {} when JSON parses to a non-dict (e.g. list)."""
    from feature_flags.service import _to_dict

    assert _to_dict("[1, 2, 3]") == {}


def test_to_dict_with_none_returns_empty() -> None:
    """_to_dict returns {} for None input."""
    from feature_flags.service import _to_dict

    assert _to_dict(None) == {}


def test_to_dict_with_int_returns_empty() -> None:
    """_to_dict returns {} for non-string, non-dict input."""
    from feature_flags.service import _to_dict

    assert _to_dict(42) == {}


# ---------------------------------------------------------------------------
# _mailbox_hash() helper
# ---------------------------------------------------------------------------


def test_mailbox_hash_returns_none_for_empty() -> None:
    """_mailbox_hash returns None for empty/None input."""
    from feature_flags.service import _mailbox_hash

    assert _mailbox_hash(None) is None
    assert _mailbox_hash("") is None


def test_mailbox_hash_returns_12char_hex() -> None:
    """_mailbox_hash returns a 12-character hex string."""
    from feature_flags.service import _mailbox_hash

    h = _mailbox_hash("inbox@test.de")
    assert h is not None
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# SQLAPoolAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqla_adapter_basic_resolution() -> None:
    """FeatureFlagService works via SQLAPoolAdapter backed by in-memory SQLite."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from feature_flags._sqla_adapter import SQLAPoolAdapter

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Create the feature_flags table (SQLite-compatible; no JSONB, use TEXT).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE feature_flags (
                    flag_name TEXT NOT NULL,
                    mailbox_address TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    config TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        )
        # Insert tenant-wide row (enabled).
        await conn.execute(
            text("INSERT INTO feature_flags VALUES ('test_flag', '', 1, '{\"k\": 1}')")
        )
        # Insert mailbox row (enabled).
        await conn.execute(
            text("INSERT INTO feature_flags VALUES ('test_flag', 'inbox@test.de', 1, '{}')")
        )

    clear_flag_cache()
    svc = FeatureFlagService(SQLAPoolAdapter(Session))
    result = await svc.is_enabled("test_flag", "inbox@test.de")
    assert result is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqla_adapter_absent_mailbox_row_returns_false() -> None:
    """SQLAPoolAdapter: absent mailbox row → False."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from feature_flags._sqla_adapter import SQLAPoolAdapter

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE feature_flags (
                    flag_name TEXT NOT NULL,
                    mailbox_address TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    config TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        )
        await conn.execute(text("INSERT INTO feature_flags VALUES ('f2', '', 1, '{}')"))
        # NO mailbox row.

    clear_flag_cache()
    svc = FeatureFlagService(SQLAPoolAdapter(Session))
    result = await svc.is_enabled("f2", "inbox@test.de")
    assert result is False

    await engine.dispose()
