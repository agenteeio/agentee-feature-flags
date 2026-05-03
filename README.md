# agentee-feature-flags

Shared feature-flag library for Alea backend services (email-planner-agent,
communication-agent, inbox-sorting-agent).

## Cache scoping (v0.3.0+)

The 60-second TTL cache is **per-instance**: each `FeatureFlagService`
keeps its own `_cache` dict. Construct **one service per tenant pool**.
Sharing a single instance across tenants will silently poison the cache
(see ISSUE-891 — fixed in v0.3.0). Prior to v0.3.0 the cache was a module
global keyed only on `(flag, mailbox)`, which leaked values across
tenants that happened to share a key.

Use `service.clear_cache()` (instance method) to invalidate. The
module-level `clear_flag_cache()` helper was **removed** in v0.3.0.

## Resolution semantics

`FeatureFlagService` uses **mailbox-overrides-tenant** semantics and is
**fail-closed**:

1. If a mailbox is supplied AND a per-mailbox row exists, that row's `enabled`
   value wins (regardless of the tenant-wide row).
2. Otherwise, the tenant-wide row (`mailbox_address = ''`) decides.
3. If neither row exists → `False` (fail-closed).
4. Any DB error → `False`.
5. Mailbox matching is case-insensitive.
6. Configs are shallow-merged (`{**tenant.config, **mailbox.config}`) so per-mailbox
   keys override tenant defaults.

### Truth table

| tenant row | mailbox row | resolved |
|------------|-------------|----------|
| missing    | missing     | `False`  |
| missing    | OFF         | `False`  |
| missing    | ON          | `True`   |
| OFF        | missing     | `False`  |
| OFF        | OFF         | `False`  |
| OFF        | ON          | `True`   |
| ON         | missing     | `True`   |
| ON         | OFF         | `False`  |
| ON         | ON          | `True`   |

This is more useful than strict-AND: a single mailbox can opt into a flag
without a tenant-wide row, and a per-mailbox kill-switch still works while the
tenant is ON.

A 60-second in-process TTL cache is keyed on
`(flag_name, lower(mailbox_address or ''))`. Errors are NOT cached, so a flag
recovers automatically after a transient DB failure.

## Schema

The library expects a `feature_flags` table with at least:

| column            | type      | notes                                            |
|-------------------|-----------|--------------------------------------------------|
| `flag_name`       | `text`    | e.g. `"new_planner_routing"`                     |
| `mailbox_address` | `text`    | empty string `''` for tenant-wide row            |
| `enabled`         | `boolean` | mailbox row (if present) overrides tenant row    |
| `config`          | `jsonb`   | optional per-flag config; merged across rows     |

## Usage

### asyncpg-backed services

```python
from feature_flags import FeatureFlagService

service = FeatureFlagService(asyncpg_pool)
if await service.is_enabled("new_planner_routing", mailbox="alice@kitarino.com"):
    ...
```

### SQLAlchemy-backed services

Wrap the async engine with the included adapter:

```python
from feature_flags import FeatureFlagService
from feature_flags._sqla_adapter import SQLAPoolAdapter

service = FeatureFlagService(SQLAPoolAdapter(async_engine))
```

## Installing as a dependency

Pin to a tag via Git URL in your service's `pyproject.toml`:

```toml
[project]
dependencies = [
    "agentee-feature-flags @ git+https://github.com/agenteeio/agentee-feature-flags.git@v0.3.0",
]
```

For local development against a checkout, use `[tool.uv.sources]`:

```toml
[tool.uv.sources]
agentee-feature-flags = { path = "../agentee-feature-flags", editable = true }
```

## Development

```bash
uv sync
uv run pytest tests/ --cov=feature_flags --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
```

100% line coverage is enforced in CI.
