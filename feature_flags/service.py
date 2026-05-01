"""FeatureFlagService — shared feature flag resolution for Alea services.

Resolution semantics (strict AND, fail-closed):
    1. Tenant-wide row (mailbox_address = '') must exist AND have enabled = True.
    2. If a mailbox is provided, the per-mailbox row must ALSO exist AND be enabled.
    3. Any missing row or disabled value → False.
    4. Any DB/pool error → False (never re-raised; logged as WARNING).
    5. Mailbox matching is case-insensitive.

Config merge (shallow, mailbox keys override tenant defaults):
    merged = {**tenant_row.config, **mailbox_row.config}

Cache:
    60-second TTL per (flag_name, lower(mailbox_address|'')) key.
    Dict operations are GIL-atomic for single keys — no asyncio.Lock needed.
    Error results are NOT cached so the flag recovers after transient DB failures.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from loguru import logger

FLAG_CACHE_TTL_S: float = 60.0

# Cache: (flag_name, lower(mailbox_address or '')) → (enabled, config, expires_at)
_FLAG_CACHE: dict[tuple[str, str], tuple[bool, dict[str, Any], float]] = {}


def clear_flag_cache() -> None:
    """Invalidate the entire cache (used in tests and admin endpoints)."""
    _FLAG_CACHE.clear()


def _mailbox_hash(mailbox: str | None) -> str | None:
    """SHA-256/12 prefix for PII-safe logging."""
    if not mailbox:
        return None
    return hashlib.sha256(mailbox.encode()).hexdigest()[:12]


def _to_dict(value: Any) -> dict[str, Any]:
    """Coerce a config value to a dict.

    asyncpg returns JSONB columns as Python dicts automatically.
    SQLAlchemy with SQLite returns TEXT columns as JSON strings.
    This helper handles both cases.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json as _json

        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


class FeatureFlagService:
    """Resolves feature flags from the canonical ``feature_flags`` table.

    Accepts any asyncpg-compatible pool (asyncpg.Pool or any object that
    exposes ``pool.acquire()`` as an async context manager yielding a
    connection with ``.fetchrow(sql, *params)`` and ``.fetch(sql, *params)``
    methods).

    For SQLAlchemy-backed services, wrap the engine/session with
    ``_sqla_adapter.SQLAPoolAdapter`` before passing it here.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def is_enabled(
        self,
        flag_name: str,
        mailbox: str | None = None,
    ) -> bool:
        """Return True iff the flag is enabled for the given mailbox.

        Both the global (mailbox='') row AND the per-mailbox row (if mailbox
        is supplied) must be explicitly enabled.  Any absent or disabled row
        returns False.  DB errors are caught and return False (fail-closed).
        """
        cache_key = (flag_name, (mailbox or "").lower())
        cached = _FLAG_CACHE.get(cache_key)
        if cached is not None:
            enabled, _, expires_at = cached
            if time.monotonic() < expires_at:
                return enabled

        try:
            result, config = await self._resolve(flag_name, mailbox)
        except Exception as exc:
            logger.warning(
                "feature_flag_resolve_error",
                flag=flag_name,
                mailbox_hash=_mailbox_hash(mailbox),
                error=str(exc),
            )
            # Do NOT cache errors — flag must recover after transient failures.
            return False

        _FLAG_CACHE[cache_key] = (result, config, time.monotonic() + FLAG_CACHE_TTL_S)
        logger.debug(
            "feature_flag_resolved",
            flag=flag_name,
            mailbox_hash=_mailbox_hash(mailbox),
            result=result,
        )
        return result

    async def get_config(
        self,
        flag_name: str,
        mailbox: str | None = None,
    ) -> dict[str, Any]:
        """Return the merged config dict for this flag (tenant defaults + mailbox overrides).

        Returns ``{}`` if the flag is disabled or does not exist.
        """
        cache_key = (flag_name, (mailbox or "").lower())
        cached = _FLAG_CACHE.get(cache_key)
        if cached is not None:
            _, config, expires_at = cached
            if time.monotonic() < expires_at:
                return config

        try:
            enabled, config = await self._resolve(flag_name, mailbox)
        except Exception as exc:
            logger.warning(
                "feature_flag_config_error",
                flag=flag_name,
                mailbox_hash=_mailbox_hash(mailbox),
                error=str(exc),
            )
            return {}

        _FLAG_CACHE[cache_key] = (enabled, config, time.monotonic() + FLAG_CACHE_TTL_S)
        return config

    async def _resolve(
        self,
        flag_name: str,
        mailbox: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Query DB; return (enabled, merged_config).

        Raises on DB error (caller catches and returns fail-closed False).
        """
        async with self._pool.acquire() as conn:
            # Step 1: tenant-wide row (mailbox_address = '') must be ON.
            tenant_row = await conn.fetchrow(
                "SELECT enabled, config FROM feature_flags"
                " WHERE flag_name = $1 AND mailbox_address = ''",
                flag_name,
            )
            if tenant_row is None or not tenant_row["enabled"]:
                return False, {}

            tenant_config: dict[str, Any] = _to_dict(tenant_row["config"])

            # Step 2 (conditional): per-mailbox row must ALSO be ON.
            if not mailbox:
                return True, tenant_config

            mailbox_row = await conn.fetchrow(
                "SELECT enabled, config FROM feature_flags"
                " WHERE flag_name = $1 AND lower(mailbox_address) = lower($2)",
                flag_name,
                mailbox,
            )
            if mailbox_row is None or not mailbox_row["enabled"]:
                return False, {}

            mailbox_config: dict[str, Any] = _to_dict(mailbox_row["config"])
            # Shallow merge: mailbox keys override tenant defaults.
            merged = {**tenant_config, **mailbox_config}
            return True, merged
