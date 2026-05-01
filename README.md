# agentee-feature-flags

Shared feature-flag library for Alea backend services (email-planner-agent,
communication-agent, inbox-sorting-agent).

## Resolution semantics

`FeatureFlagService` is **strict-AND** and **fail-closed**:

1. The tenant-wide row (`mailbox_address = ''`) must exist AND be `enabled = True`.
2. If a mailbox is provided, the per-mailbox row must ALSO exist AND be enabled.
3. Any missing row, disabled value, or DB error → `False`.
4. Mailbox matching is case-insensitive.
5. Configs are shallow-merged (`{**tenant.config, **mailbox.config}`) so per-mailbox
   keys override tenant defaults.

A 60-second in-process TTL cache is keyed on
`(flag_name, lower(mailbox_address or ''))`. Errors are NOT cached, so a flag
recovers automatically after a transient DB failure.

## Schema

The library expects a `feature_flags` table with at least:

| column            | type      | notes                                            |
|-------------------|-----------|--------------------------------------------------|
| `flag_name`       | `text`    | e.g. `"new_planner_routing"`                     |
| `mailbox_address` | `text`    | empty string `''` for tenant-wide row            |
| `enabled`         | `boolean` | both rows must be true for `is_enabled` to pass  |
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
    "agentee-feature-flags @ git+https://github.com/agenteeio/agentee-feature-flags.git@v0.1.0",
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
