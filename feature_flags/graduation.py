"""Feature flag graduation ledger write path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

TOMBSTONE_STATES = {"graduated", "always-on", "removed"}


@dataclass(frozen=True)
class FeatureFlagGraduation:
    """Inputs required to append a feature flag graduation row."""

    flag_name: str
    owning_service: str
    owner: str
    rollout_state: str
    introduced_issue: str | None = None
    flag_default: bool | None = None
    graduation_evidence: str | None = None
    delete_by: date | None = None
    cleanup_ticket: str | None = None
    seed_locations: list[str] | None = None
    runtime_call_sites: list[str] | None = None
    ship_engine_allowlist: list[str] | None = None
    db_cleanup_required: bool = True
    rollback_note: str | None = None
    recorded_by: str = "agentee-cli"
    tombstone_reason: str | None = None
    dedupe_key: str | None = None

    def normalized(self) -> FeatureFlagGraduation:
        """Return a sanitized copy with defaults filled."""
        flag_name = self.flag_name.strip()
        rollout_state = self.rollout_state.strip()
        cleanup_or_issue = self.cleanup_ticket or self.introduced_issue or "manual"
        dedupe_key = self.dedupe_key or f"{flag_name}:{rollout_state}:{cleanup_or_issue}"
        tombstone_reason = (
            self.tombstone_reason
            or f"{flag_name} retired via agentee flags graduate ({rollout_state})"
        )
        return FeatureFlagGraduation(
            flag_name=flag_name,
            owning_service=self.owning_service.strip(),
            owner=self.owner.strip(),
            rollout_state=rollout_state,
            introduced_issue=_blank_to_none(self.introduced_issue),
            flag_default=self.flag_default,
            graduation_evidence=_blank_to_none(self.graduation_evidence),
            delete_by=self.delete_by,
            cleanup_ticket=_blank_to_none(self.cleanup_ticket),
            seed_locations=list(self.seed_locations or []),
            runtime_call_sites=list(self.runtime_call_sites or []),
            ship_engine_allowlist=list(self.ship_engine_allowlist or []),
            db_cleanup_required=self.db_cleanup_required,
            rollback_note=_blank_to_none(self.rollback_note),
            recorded_by=self.recorded_by.strip() or "agentee-cli",
            tombstone_reason=tombstone_reason,
            dedupe_key=dedupe_key,
        )


@dataclass(frozen=True)
class GraduationResult:
    """Result returned by a graduation transaction."""

    row: dict[str, Any]
    inserted: bool
    deleted_feature_flag_rows: int
    updated_lifecycle_rows: int

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable result shape."""
        return {
            "row": self.row,
            "inserted": self.inserted,
            "deleted_feature_flag_rows": self.deleted_feature_flag_rows,
            "updated_lifecycle_rows": self.updated_lifecycle_rows,
        }


async def record_feature_flag_graduation(
    pool: Any,
    graduation: FeatureFlagGraduation,
) -> GraduationResult:
    """Append the graduation row and remove live flag rows transactionally."""
    item = graduation.normalized()
    _validate(item)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO feature_flag_graduations (
                    flag_name,
                    owning_service,
                    owner,
                    introduced_issue,
                    flag_default,
                    rollout_state,
                    tombstone_reason,
                    graduation_evidence,
                    delete_by,
                    cleanup_ticket,
                    seed_locations,
                    runtime_call_sites,
                    ship_engine_allowlist,
                    db_cleanup_required,
                    rollback_note,
                    recorded_by,
                    dedupe_key
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12::jsonb, $13::jsonb, $14, $15, $16, $17
                )
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id, flag_name, rollout_state, recorded_at, tombstoned_at, dedupe_key
                """,
                item.flag_name,
                item.owning_service,
                item.owner,
                item.introduced_issue,
                item.flag_default,
                item.rollout_state,
                item.tombstone_reason,
                item.graduation_evidence,
                item.delete_by,
                item.cleanup_ticket,
                json.dumps(item.seed_locations),
                json.dumps(item.runtime_call_sites),
                json.dumps(item.ship_engine_allowlist),
                item.db_cleanup_required,
                item.rollback_note,
                item.recorded_by,
                item.dedupe_key,
            )
            inserted = row is not None
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT id, flag_name, rollout_state, recorded_at, tombstoned_at, dedupe_key
                    FROM feature_flag_graduations
                    WHERE dedupe_key = $1
                    """,
                    item.dedupe_key,
                )
            deleted_status = await conn.execute(
                "DELETE FROM feature_flags WHERE flag_name = $1",
                item.flag_name,
            )
            updated_lifecycle_rows = 0
            if await _lifecycle_metadata_exists(conn):
                update_status = await conn.execute(
                    """
                    UPDATE feature_flag_lifecycle_metadata
                    SET lifecycle_stage = 'graduated', updated_at = now()
                    WHERE flag_name = $1
                    """,
                    item.flag_name,
                )
                updated_lifecycle_rows = _status_count(update_status)

    if row is None:
        raise RuntimeError("graduation insert returned no row and no existing dedupe row")
    return GraduationResult(
        row=dict(row),
        inserted=inserted,
        deleted_feature_flag_rows=_status_count(deleted_status),
        updated_lifecycle_rows=updated_lifecycle_rows,
    )


def _validate(item: FeatureFlagGraduation) -> None:
    """Validate normalized graduation inputs."""
    required = {
        "flag_name": item.flag_name,
        "owning_service": item.owning_service,
        "owner": item.owner,
        "rollout_state": item.rollout_state,
        "recorded_by": item.recorded_by,
        "dedupe_key": item.dedupe_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"missing required graduation fields: {', '.join(missing)}")
    if item.rollout_state not in TOMBSTONE_STATES:
        allowed = ", ".join(sorted(TOMBSTONE_STATES))
        raise ValueError(f"rollout_state must be one of: {allowed}")


async def _lifecycle_metadata_exists(conn: Any) -> bool:
    """Return whether the lifecycle metadata table is present."""
    return bool(
        await conn.fetchval("SELECT to_regclass('feature_flag_lifecycle_metadata') IS NOT NULL")
    )


def _status_count(status: str) -> int:
    """Parse asyncpg command status strings like ``DELETE 2``."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


def _blank_to_none(value: str | None) -> str | None:
    """Normalize empty strings to None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
