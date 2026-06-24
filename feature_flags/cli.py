"""Command-line interface for Agentee feature flags."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import date

from feature_flags.graduation import FeatureFlagGraduation, record_feature_flag_graduation


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ``agentee`` command."""
    args = build_parser().parse_args(argv)
    if args.command == "flags" and args.flags_command == "graduate":
        payload = asyncio.run(_run_graduate(args))
        print(json.dumps(payload, default=str, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="agentee")
    subcommands = parser.add_subparsers(dest="command", required=True)
    flags = subcommands.add_parser("flags")
    flag_commands = flags.add_subparsers(dest="flags_command", required=True)
    graduate = flag_commands.add_parser("graduate")
    graduate.add_argument("flag_name")
    graduate.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    graduate.add_argument("--owning-service", required=True)
    graduate.add_argument("--owner", required=True)
    graduate.add_argument("--introduced-issue")
    graduate.add_argument("--flag-default", type=_parse_bool)
    graduate.add_argument(
        "--rollout-state",
        default="graduated",
        choices=["graduated", "always-on", "removed"],
    )
    graduate.add_argument("--graduation-evidence", required=True)
    graduate.add_argument("--delete-by", type=date.fromisoformat)
    graduate.add_argument("--cleanup-ticket")
    graduate.add_argument("--seed-location", action="append", default=[])
    graduate.add_argument("--runtime-call-site", action="append", default=[])
    graduate.add_argument("--ship-engine-allowlist", action="append", default=[])
    graduate.add_argument("--db-cleanup-required", action="store_true")
    graduate.add_argument("--rollback-note")
    graduate.add_argument("--recorded-by", default="agentee-cli")
    graduate.add_argument("--tombstone-reason")
    graduate.add_argument("--dedupe-key")
    graduate.add_argument("--yes", action="store_true", required=True)
    return parser


async def _run_graduate(args: argparse.Namespace) -> dict[str, object]:
    """Connect to Postgres and record a graduation."""
    database_url = args.database_url
    if not database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")
    import asyncpg

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    try:
        result = await record_feature_flag_graduation(
            pool,
            FeatureFlagGraduation(
                flag_name=args.flag_name,
                owning_service=args.owning_service,
                owner=args.owner,
                introduced_issue=args.introduced_issue,
                flag_default=args.flag_default,
                rollout_state=args.rollout_state,
                graduation_evidence=args.graduation_evidence,
                delete_by=args.delete_by,
                cleanup_ticket=args.cleanup_ticket,
                seed_locations=args.seed_location,
                runtime_call_sites=args.runtime_call_site,
                ship_engine_allowlist=args.ship_engine_allowlist,
                db_cleanup_required=args.db_cleanup_required,
                rollback_note=args.rollback_note,
                recorded_by=args.recorded_by,
                tombstone_reason=args.tombstone_reason,
                dedupe_key=args.dedupe_key,
            ),
        )
    finally:
        await pool.close()
    return result.as_dict()


def _parse_bool(value: str) -> bool:
    """Parse explicit boolean CLI values."""
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected one of true/false/yes/no/1/0/on/off")
