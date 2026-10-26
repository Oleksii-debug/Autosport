"""Run one bounded, restart-safe Account C sandbox PAPER campaign segment."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan,
    BlindPaperSandboxCampaign,
    SandboxCampaignError,
    run_network_endurance,
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a no-auth synthetic table-tennis PAPER segment. "
            "The resulting evidence is never forward-evidence eligible."
        )
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--segment-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--cutoff-seconds", type=int, default=7800)
    parser.add_argument("--seed", type=int, default=23)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--expect-new", action="store_true")
    mode.add_argument("--expect-resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    return parser


def _new_campaign(args: argparse.Namespace) -> BlindPaperSandboxCampaign:
    if args.cutoff_seconds <= args.segment_seconds:
        raise SandboxCampaignError("cutoff_seconds must exceed segment_seconds")
    started = datetime.now(timezone.utc)
    campaign_id = "account-c-sandbox-" + started.strftime("%Y%m%dT%H%M%SZ")
    return BlindPaperSandboxCampaign(
        BlindPaperCampaignPlan(
            campaign_id=campaign_id,
            started_at=_timestamp(started),
            cutoff_at=_timestamp(started + timedelta(seconds=args.cutoff_seconds)),
        ),
        seed=args.seed,
    )


def _validate_boundary(summary: dict[str, object]) -> None:
    if (
        summary["truth"] != "simulated"
        or summary["paper_only"] is not True
        or summary["real_money_execution"] is not False
        or summary["forward_evidence_eligible"] is not False
        or summary["promotion_authorized"] is not False
        or summary["profitability_claim"] is not False
    ):
        raise SandboxCampaignError("sandbox evidence boundary was violated")


def main() -> int:
    args = _parser().parse_args()
    state_path = args.workspace / "campaign_state.json"
    if args.expect_new:
        if state_path.exists():
            raise SandboxCampaignError("expect-new refuses to overwrite campaign state")
        campaign = _new_campaign(args)
    else:
        if not state_path.is_file():
            raise SandboxCampaignError(
                "expect-resume/verify-only requires campaign_state.json"
            )
        campaign = BlindPaperSandboxCampaign.load(args.workspace)

    if args.verify_only:
        summary = campaign.summary()
        counters = summary["counters"]
        if (
            type(counters) is not dict
            or summary["policy_generation"] <= 0
            or counters.get("NETWORK_SNAPSHOTS", 0) < 2
        ):
            raise SandboxCampaignError(
                "verified campaign lacks completed network learning evidence"
            )
        _validate_boundary(summary)
        summary["independent_restart_verification"] = "PASS"
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    summary = run_network_endurance(
        campaign,
        workspace=args.workspace,
        duration_seconds=args.segment_seconds,
        poll_interval_seconds=args.poll_seconds,
        progress=lambda event: print(
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            flush=True,
        ),
    )
    if summary["network_snapshots_segment"] < 2:
        raise SandboxCampaignError("segment requires at least two source snapshots")
    _validate_boundary(summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
