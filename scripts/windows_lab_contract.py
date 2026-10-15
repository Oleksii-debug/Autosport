"""Manual, nonexecuting controller for the immutable Windows lab handoff.

This tool never starts a program, clicks GUI controls, reads local user files,
runs shell commands, or treats PASS reported by an agent as independent evidence.
"""
from __future__ import annotations

import argparse
import sys

from autosport.windows_lab_contract import (
    WindowsLabCampaign,
    WindowsLabContractError,
    WindowsLabTicket,
    source_level_lab_ticket,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autosport source-bound Windows lab contract")
    commands = parser.add_subparsers(dest="operation", required=True)
    issue = commands.add_parser("issue", help="Produce a nonexecuting lab ticket on stdout")
    issue.add_argument("--source-sha", required=True)
    issue.add_argument("--package-sha256", required=True)
    commands.add_parser("verify-ticket", help="Read <=16 KiB JSON ticket from stdin")
    commands.add_parser("verify-campaign", help="Read <=16 KiB JSON campaign from stdin")
    args = parser.parse_args(argv)
    try:
        if args.operation == "issue":
            print(source_level_lab_ticket(args.source_sha, args.package_sha256).to_json())
        elif args.operation == "verify-ticket":
            raw = sys.stdin.read(16_385)
            ticket = WindowsLabTicket.from_json(raw)
            print("TICKET_CONTRACT_OK ticket_id=" + ticket.ticket_id)
        else:
            raw = sys.stdin.read(16_385)
            campaign = WindowsLabCampaign.from_json(raw)
            print(
                "CAMPAIGN_UNVERIFIED ticket_id="
                + campaign.ticket.ticket_id
                + " observations=" + str(len(campaign.observations))
                + " source_bound_sha256=" + campaign.source_bound_sha256
            )
    except (WindowsLabContractError, UnicodeError):
        print("LAB_CONTRACT_REJECTED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
