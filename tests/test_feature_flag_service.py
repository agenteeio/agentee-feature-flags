"""Unit tests for feature_flags.service.FeatureFlagService — 100% coverage.

Resolution truth-table (override semantics — mailbox row wins over tenant):
    | tenant | mailbox | result |
    |--------|---------|--------|
    |   -    |   -     | False  |  (no rows at all)
    |   -    |  OFF    | False  |  (mailbox row decides)
    |   -    |   ON    | True   |  (mailbox can opt-in alone)
    |  OFF   |   -     | False  |  (tenant row decides)
    |  OFF   |  OFF    | False  |  (mailbox row decides)
    |  OFF   |   ON    | True   |  (mailbox overrides tenant OFF)
    |   ON   |   -     | True   |  (tenant default applies)
    |   ON   |  OFF    | False  |  (per-mailbox kill-switch)
    |   ON   |   ON    | True   |  (both ON)

Plus: no-mailbox calls obey the tenant row (missing/OFF/ON).

Covers:
    - is_enabled() — all truth-table paths
    - get_config() — shallow merge, empty-when-disabled
    - cache hit (no DB call on second request)
    - cache expiry triggers fresh DB call
    - DB error → False, NOT cached
    - mailbox case-insensitivity
    - SQLAPoolAdapter (in-memory aiosqlite via SQLAlchemy)
    - per-instance cache isolation (regression for ISSUE-891 cross-tenant poisoning)
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from feature_flags.service import FeatureFlagService

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
async def test_no_rows_returns_false() -> None:
    """Neither tenant nor mailbox row exists → False (fail-closed)."""
    svc = FeatureFlagService(_make_pool(tenant_row=None, mailbox_row=None))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_tenant_missing_mailbox_off_returns_false() -> None:
    """Tenant row missing, mailbox OFF → False (mailbox row decides)."""
    svc = FeatureFlagService(_make_pool(tenant_row=None, mailbox_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_tenant_missing_mailbox_on_returns_true() -> None:
    """Tenant row missing, mailbox ON → True (mailbox can opt-in alone)."""
    svc = FeatureFlagService(_make_pool(tenant_row=None, mailbox_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is True


@pytest.mark.asyncio
async def test_tenant_off_no_mailbox_returns_false() -> None:
    """Tenant OFF, no mailbox supplied → False (tenant row decides)."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1") is False


@pytest.mark.asyncio
async def test_tenant_off_mailbox_off_returns_false() -> None:
    """Tenant OFF, mailbox OFF → False (mailbox row decides — and it's OFF)."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(False), mailbox_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_tenant_off_mailbox_on_returns_true() -> None:
    """Tenant OFF, mailbox ON → True (mailbox overrides tenant OFF)."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(False), mailbox_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is True


@pytest.mark.asyncio
async def test_tenant_on_no_mailbox_returns_true() -> None:
    """Tenant ON and no mailbox supplied → True."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1") is True


@pytest.mark.asyncio
async def test_tenant_on_mailbox_missing_returns_true() -> None:
    """Tenant ON, no per-mailbox row → True (tenant default applies)."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=None))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is True


@pytest.mark.asyncio
async def test_tenant_on_mailbox_off_returns_false() -> None:
    """Tenant ON, mailbox OFF → False (per-mailbox kill-switch)."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=_row(False)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is False


@pytest.mark.asyncio
async def test_both_on_returns_true() -> None:
    """Tenant ON + mailbox ON → True."""
    svc = FeatureFlagService(_make_pool(tenant_row=_row(True), mailbox_row=_row(True)))
    assert await svc.is_enabled("auto_execute.invoice_v1", "inbox@test.de") is True


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_cache_key_is_lowercased() -> None:
    """Cache key uses lower(mailbox) so mixed-case matches the same entry."""
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
    pool = _make_pool(tenant_row=_row(True), mailbox_row=_row(True))
    svc = FeatureFlagService(pool)

    await svc.is_enabled("flag_ttl", "inbox@ttl.de")
    first_call_count = pool.acquire.call_count  # type: ignore[attr-defined]

    # Manually expire the cache entry on this instance.
    key = ("flag_ttl", "inbox@ttl.de")
    svc._cache[key] = (True, {}, time.monotonic() - 1.0)

    await svc.is_enabled("flag_ttl", "inbox@ttl.de")
    assert pool.acquire.call_count > first_call_count  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_clear_cache_invalidates_instance_cache() -> None:
    """clear_cache() drops cached entries on the instance, forcing a DB re-query."""
    pool = _make_pool(tenant_row=_row(True), mailbox_row=_row(True))
    svc = FeatureFlagService(pool)

    await svc.is_enabled("flag_cc", "inbox@cc.de")
    assert ("flag_cc", "inbox@cc.de") in svc._cache

    svc.clear_cache()
    assert svc._cache == {}

    first_call_count = pool.acquire.call_count  # type: ignore[attr-defined]
    await svc.is_enabled("flag_cc", "inbox@cc.de")
    # Cache was cleared, so the next call must hit the DB again.
    assert pool.acquire.call_count > first_call_count  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DB error → fail-closed, not cached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_error_returns_false_and_is_not_cached() -> None:
    """DB error → return False; result NOT cached so flag recovers on next call."""
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
    assert key not in svc._cache


# ---------------------------------------------------------------------------
# get_config()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_shallow_merge() -> None:
    """get_config returns tenant defaults with mailbox overrides merged in."""
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
    tenant_cfg = {"x": 1}
    pool = _make_pool(tenant_row=_row(True, tenant_cfg))
    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag")
    assert cfg == {"x": 1}


@pytest.mark.asyncio
async def test_get_config_disabled_returns_empty() -> None:
    """get_config returns {} when flag is disabled."""
    pool = _make_pool(tenant_row=_row(False))
    svc = FeatureFlagService(pool)
    cfg = await svc.get_config("some_flag", "inbox@test.de")
    assert cfg == {}


@pytest.mark.asyncio
async def test_get_config_cache_hit_skips_db() -> None:
    """Second get_config call for same key is served from per-instance cache."""
    tenant_cfg = {"x": 1}
    pool = _make_pool(tenant_row=_row(True, tenant_cfg))
    svc = FeatureFlagService(pool)

    cfg1 = await svc.get_config("flag_gc", "inbox@gc.de")
    first_call_count = pool.acquire.call_count  # type: ignore[attr-defined]

    cfg2 = await svc.get_config("flag_gc", "inbox@gc.de")
    assert cfg1 == cfg2
    assert pool.acquire.call_count == first_call_count  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_config_db_error_returns_empty() -> None:
    """get_config returns {} on DB error."""
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
# Cross-tenant cache isolation (regression for ISSUE-891)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_instance_cache_isolation_no_cross_tenant_poisoning() -> None:
    """Regression for ISSUE-891.

    Before v0.3.0, _FLAG_CACHE was a module-global dict keyed only on
    (flag, mailbox). Two FeatureFlagService instances backed by different
    tenant pools would share the cache: tenant A's cached value would leak
    into tenant B's lookup with the same (flag, mailbox) key.

    With per-instance cache, each FeatureFlagService keeps its own values.
    Tenant A's cache must NOT influence tenant B's resolution, even when
    the (flag, mailbox) key is identical.
    """
    # Tenant A has the flag ENABLED tenant-wide.
    tenant_a_pool = _make_pool(tenant_row=_row(True), mailbox_row=None)
    # Tenant B has the flag DISABLED tenant-wide.
    tenant_b_pool = _make_pool(tenant_row=_row(False), mailbox_row=None)

    svc_a = FeatureFlagService(tenant_a_pool)
    svc_b = FeatureFlagService(tenant_b_pool)

    # Resolve via tenant A first — caches True against (flag, '').
    assert await svc_a.is_enabled("shared_flag") is True

    # Tenant B must resolve from its own pool (False), NOT from A's cache.
    assert await svc_b.is_enabled("shared_flag") is False

    # Verify caches are physically distinct dicts.
    assert svc_a._cache is not svc_b._cache
    assert ("shared_flag", "") in svc_a._cache
    assert ("shared_flag", "") in svc_b._cache
    # Confirm each cache contains its own tenant's resolved value.
    assert svc_a._cache[("shared_flag", "")][0] is True
    assert svc_b._cache[("shared_flag", "")][0] is False

    # Tenant B's pool must have been queried (cache was NOT poisoned by A).
    assert tenant_b_pool.acquire.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_per_instance_cache_isolation_with_mailbox() -> None:
    """Regression for ISSUE-891 with a mailbox-keyed lookup.

    Two tenants both have a mailbox row for the same address (rare but
    possible during a migration / re-onboarding). The cached value from
    one tenant must never be returned for the other.
    """
    tenant_a_pool = _make_pool(tenant_row=None, mailbox_row=_row(True))
    tenant_b_pool = _make_pool(tenant_row=None, mailbox_row=_row(False))

    svc_a = FeatureFlagService(tenant_a_pool)
    svc_b = FeatureFlagService(tenant_b_pool)

    assert await svc_a.is_enabled("shared_flag", "shared@inbox.de") is True
    assert await svc_b.is_enabled("shared_flag", "shared@inbox.de") is False

    # Both pools were queried independently — no shared cache.
    assert tenant_a_pool.acquire.call_count == 1  # type: ignore[attr-defined]
    assert tenant_b_pool.acquire.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_clear_cache_does_not_affect_other_instances() -> None:
    """clear_cache() on one instance must not invalidate another instance's cache."""
    pool_a = _make_pool(tenant_row=_row(True))
    pool_b = _make_pool(tenant_row=_row(True))

    svc_a = FeatureFlagService(pool_a)
    svc_b = FeatureFlagService(pool_b)

    await svc_a.is_enabled("f")
    await svc_b.is_enabled("f")
    assert ("f", "") in svc_a._cache
    assert ("f", "") in svc_b._cache

    svc_a.clear_cache()
    assert svc_a._cache == {}
    # B's cache must still have its entry.
    assert ("f", "") in svc_b._cache


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

    svc = FeatureFlagService(SQLAPoolAdapter(Session))
    result = await svc.is_enabled("test_flag", "inbox@test.de")
    assert result is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqla_adapter_tenant_on_no_mailbox_row_returns_true() -> None:
    """SQLAPoolAdapter: tenant ON + no mailbox row → True (tenant default applies)."""
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
        # NO mailbox row — under override semantics, tenant default applies.

    svc = FeatureFlagService(SQLAPoolAdapter(Session))
    result = await svc.is_enabled("f2", "inbox@test.de")
    assert result is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqla_adapter_mailbox_overrides_tenant_off() -> None:
    """SQLAPoolAdapter: tenant OFF + mailbox ON → True (override)."""
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
        # Tenant row OFF.
        await conn.execute(text("INSERT INTO feature_flags VALUES ('f3', '', 0, '{}')"))
        # Mailbox row ON — should override tenant OFF.
        await conn.execute(
            text("INSERT INTO feature_flags VALUES ('f3', 'inbox@test.de', 1, '{}')")
        )

    svc = FeatureFlagService(SQLAPoolAdapter(Session))
    result = await svc.is_enabled("f3", "inbox@test.de")
    assert result is True

    await engine.dispose()
