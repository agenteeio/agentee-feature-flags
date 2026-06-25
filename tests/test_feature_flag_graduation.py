"""Tests for feature flag graduation writes and CLI."""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from feature_flags import FeatureFlagGraduation, GraduationResult
from feature_flags.cli import _parse_bool, build_parser, main
from feature_flags.graduation import _status_count, record_feature_flag_graduation


@dataclass
class _FakeConnection:
    row: dict[str, Any] | None
    existing_row: dict[str, Any] | None = None
    metadata_exists: bool = True
    statements: list[tuple[str, tuple[Any, ...]]] | None = None

    def __post_init__(self) -> None:
        self.statements = []
        self.fetchrow = AsyncMock(side_effect=self._fetchrow)
        self.fetchval = AsyncMock(side_effect=self._fetchval)
        self.execute = AsyncMock(side_effect=self._execute)

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield

    async def _fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.statements.append((sql, args))  # type: ignore[union-attr]
        if "INSERT INTO feature_flag_graduations" in sql:
            return self.row
        return self.existing_row

    async def _fetchval(self, sql: str, *args: Any) -> bool:
        self.statements.append((sql, args))  # type: ignore[union-attr]
        return self.metadata_exists

    async def _execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))  # type: ignore[union-attr]
        if sql.strip().startswith("DELETE"):
            return "DELETE 2"
        return "UPDATE 1"


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self.conn


def _graduation(**overrides: Any) -> FeatureFlagGraduation:
    values = {
        "flag_name": "oi_tiered_interpreter",
        "owning_service": "email-planner-agent",
        "owner": "Alea Platform",
        "introduced_issue": "ISSUE-845",
        "flag_default": False,
        "rollout_state": "graduated",
        "graduation_evidence": "QA links",
        "delete_by": date(2026, 7, 31),
        "cleanup_ticket": "ISSUE-1314",
        "seed_locations": ["src/app/infrastructure/database.py:_FF_SEED_CANONICAL"],
        "runtime_call_sites": ["src/app/services/plan_operation_interpreter.py"],
        "ship_engine_allowlist": ["ship-engine/scenarios"],
        "db_cleanup_required": True,
        "rollback_note": "Reintroduce flag if rollback is needed.",
        "recorded_by": "pytest",
    }
    values.update(overrides)
    return FeatureFlagGraduation(**values)


@pytest.mark.asyncio
async def test_record_feature_flag_graduation_inserts_deletes_and_updates_metadata() -> None:
    """Graduation writes the ledger row and removes live flag rows in one transaction."""
    row = {
        "id": 7,
        "flag_name": "oi_tiered_interpreter",
        "rollout_state": "graduated",
        "dedupe_key": "custom-key",
    }
    conn = _FakeConnection(row=row)

    result = await record_feature_flag_graduation(
        _FakePool(conn),
        _graduation(dedupe_key="custom-key"),
    )

    assert result == GraduationResult(
        row=row,
        inserted=True,
        deleted_feature_flag_rows=2,
        updated_lifecycle_rows=1,
    )
    insert_args = conn.statements[0][1]  # type: ignore[index]
    assert insert_args[0] == "oi_tiered_interpreter"
    assert insert_args[10] == '["src/app/infrastructure/database.py:_FF_SEED_CANONICAL"]'
    assert any("DELETE FROM feature_flags" in sql for sql, _ in conn.statements or [])
    assert any("UPDATE feature_flag_lifecycle_metadata" in sql for sql, _ in conn.statements or [])


@pytest.mark.asyncio
async def test_record_feature_flag_graduation_is_idempotent_by_dedupe_key() -> None:
    """A duplicate dedupe key reuses the existing ledger row and still cleans DB rows."""
    existing = {
        "id": 8,
        "flag_name": "qa_only_flag",
        "rollout_state": "removed",
        "dedupe_key": "qa_only_flag:removed:ISSUE-1316",
    }
    conn = _FakeConnection(row=None, existing_row=existing, metadata_exists=False)

    result = await record_feature_flag_graduation(
        _FakePool(conn),
        _graduation(
            flag_name=" qa_only_flag ",
            rollout_state="removed",
            cleanup_ticket=None,
            introduced_issue="ISSUE-1316",
        ),
    )

    assert result.inserted is False
    assert result.row == existing
    assert result.updated_lifecycle_rows == 0
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_record_feature_flag_graduation_rejects_cross_flag_dedupe_conflict() -> None:
    """A dedupe-key collision for a different flag must not delete the requested flag."""

    existing = {
        "id": 8,
        "flag_name": "other_flag",
        "rollout_state": "removed",
        "dedupe_key": "shared-key",
    }
    conn = _FakeConnection(row=None, existing_row=existing)

    with pytest.raises(ValueError, match="different feature flag"):
        await record_feature_flag_graduation(
            _FakePool(conn),
            _graduation(flag_name="qa_only_flag", dedupe_key="shared-key"),
        )

    assert not any("DELETE FROM feature_flags" in sql for sql, _ in conn.statements or [])


@pytest.mark.asyncio
async def test_record_feature_flag_graduation_raises_when_conflict_row_is_missing() -> None:
    """A broken DB response is surfaced instead of pretending graduation succeeded."""
    with pytest.raises(RuntimeError):
        await record_feature_flag_graduation(_FakePool(_FakeConnection(row=None)), _graduation())


@pytest.mark.asyncio
async def test_feature_flag_graduation_validation_rejects_bad_inputs() -> None:
    """Required fields and tombstone rollout states are enforced."""
    with pytest.raises(ValueError, match="missing required"):
        await record_feature_flag_graduation(
            _FakePool(_FakeConnection(row={})),
            _graduation(flag_name=" "),
        )
    with pytest.raises(ValueError, match="rollout_state"):
        await record_feature_flag_graduation(
            _FakePool(_FakeConnection(row={})),
            _graduation(rollout_state="testing"),
        )


def test_cli_parser_builds_graduate_namespace() -> None:
    """The CLI accepts the operator-facing graduation arguments."""
    args = build_parser().parse_args(
        [
            "flags",
            "graduate",
            "flag_x",
            "--database-url",
            "postgresql://example",
            "--owning-service",
            "email-planner-agent",
            "--owner",
            "Alea Platform",
            "--flag-default",
            "false",
            "--graduation-evidence",
            "links",
            "--delete-by",
            "2026-07-31",
            "--seed-location",
            "seed.py",
            "--runtime-call-site",
            "runtime.py",
            "--ship-engine-allowlist",
            "allowlist.json",
            "--db-cleanup-required",
            "--yes",
        ]
    )

    assert args.command == "flags"
    assert args.flags_command == "graduate"
    assert args.flag_default is False
    assert args.db_cleanup_required is True
    assert args.delete_by == date(2026, 7, 31)
    assert args.seed_location == ["seed.py"]


def test_parse_bool_accepts_known_values_and_rejects_unknown() -> None:
    """Boolean parser keeps flag-default explicit."""
    assert _parse_bool("yes") is True
    assert _parse_bool("0") is False
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_bool("maybe")


def test_status_count_returns_zero_for_unparseable_status() -> None:
    """Unexpected asyncpg command tags are treated as zero affected rows."""
    assert _status_count("DELETE not-a-number") == 0


def test_main_runs_graduate_command(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """The console entry point connects, records, closes, and prints JSON."""
    row = {"id": 9, "flag_name": "flag_x", "rollout_state": "graduated", "dedupe_key": "d"}
    conn = _FakeConnection(row=row)
    pool = _FakePool(conn)
    pool.close = AsyncMock()  # type: ignore[attr-defined]

    async def _create_pool(database_url: str, *, min_size: int, max_size: int) -> _FakePool:
        assert database_url == "postgresql://example"
        assert min_size == 1
        assert max_size == 1
        return pool

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=_create_pool))

    main(
        [
            "flags",
            "graduate",
            "flag_x",
            "--database-url",
            "postgresql://example",
            "--owning-service",
            "email-planner-agent",
            "--owner",
            "Alea Platform",
            "--graduation-evidence",
            "links",
            "--yes",
        ]
    )

    assert '"deleted_feature_flag_rows": 2' in capsys.readouterr().out
    pool.close.assert_awaited_once()  # type: ignore[attr-defined]


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators get a clear error if no database URL is configured."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="database-url"):
        main(
            [
                "flags",
                "graduate",
                "flag_x",
                "--owning-service",
                "email-planner-agent",
                "--owner",
                "Alea Platform",
                "--graduation-evidence",
                "links",
                "--yes",
            ]
        )
