#!/usr/bin/env python3
"""Deterministically fold Autosport swarm ownership events.

The resolver is deliberately network-free. Feed it a JSON export of GitHub issue
comments in server order. It never grants ownership from malformed or ambiguous
records.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


OWNERSHIP_HEADERS = {
    "CLAIM_V1": "claim",
    "CLAIM_RENEW_V1": "renew",
    "CLAIM_HEARTBEAT_V1": "renew",
    "LEASE_RENEW_V1": "renew",
    "CLAIM_RELEASE_V1": "release",
}
ANY_VERSIONED_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9_ -]*_V\d+$")
FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?:=|:)\s*(.*)$")
CLAIM_IDENTITY_FIELDS = (
    "TASK_ID",
    "SEMANTIC_KEY",
    "ACCOUNT_ID",
    "CLAIM_MODE",
    "INTENDED_SLICE",
)
RENEW_IMMUTABLE_FIELDS = {
    "TASK_ID": "task_id",
    "SEMANTIC_KEY": "semantic_key",
    "ACCOUNT_ID": "account_id",
    "CLAIM_MODE": "claim_mode",
    "INTENDED_SLICE": "intended_slice",
}


def parse_instant(value: str) -> datetime:
    """Parse an offset-aware ISO-8601 instant and normalize it to UTC."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return instant.astimezone(timezone.utc)


def _format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_events(
    body: str,
    *,
    comment_index: int,
    comment_id: Any,
) -> list[dict[str, Any]]:
    """Extract ownership blocks while ignoring unrelated versioned blocks."""

    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line_index, raw_line in enumerate(body.splitlines()):
        line = raw_line.strip()

        if line in OWNERSHIP_HEADERS:
            current = {
                "type": OWNERSHIP_HEADERS[line],
                "header": line,
                "fields": {},
                "comment_index": comment_index,
                "comment_id": comment_id,
                "line_index": line_index,
            }
            events.append(current)
            continue

        # TASK_RESULT_V1, AUDITOR_*_V1, etc. end the preceding ownership
        # block. A later ownership header in the same comment starts a new one.
        if ANY_VERSIONED_HEADER_RE.match(line):
            current = None
            continue

        if current is None:
            continue

        match = FIELD_RE.match(line)
        if match:
            key, value = match.groups()
            current["fields"][key] = value.strip()

    return events


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if not key.startswith("_")}


def _problem(
    kind: str,
    message: str,
    *,
    event: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    comment_index: int | None = None,
    comment_id: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "message": message}
    if run_id:
        result["run_id"] = run_id
    if event is not None:
        result.update(
            {
                "header": event["header"],
                "comment_index": event["comment_index"],
                "comment_id": event["comment_id"],
                "line_index": event["line_index"],
            }
        )
    else:
        if comment_index is not None:
            result["comment_index"] = comment_index
        if comment_id is not None:
            result["comment_id"] = comment_id
    return result


def _missing_claim_identity_fields(fields: Mapping[str, Any]) -> list[str]:
    return [
        field
        for field in CLAIM_IDENTITY_FIELDS
        if not str(fields.get(field, "")).strip()
    ]


def _mark_ambiguous(
    state: dict[str, Any],
    *,
    comment_index: int,
    comment_id: Any,
) -> None:
    state["ambiguous"] = True
    state["latest_event"] = "ambiguous"
    state["latest_comment_index"] = comment_index
    state["latest_comment_id"] = comment_id


def resolve_comments(
    comments: Sequence[Mapping[str, Any]],
    *,
    now: datetime | str,
    capacity: int | None = None,
) -> dict[str, Any]:
    """Fold server-ordered comments into deterministic ownership state.

    ``comments`` must be the complete relevant issue-comment history in GitHub
    server order. Runs with malformed ownership history are never admitted.
    """

    if isinstance(now, str):
        resolved_now = parse_instant(now)
    else:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        resolved_now = now.astimezone(timezone.utc)

    if capacity is not None and capacity < 1:
        raise ValueError("capacity must be >= 1")

    malformed: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    original_claim_order: list[str] = []
    last_numeric_comment_id: int | None = None
    server_order_invalid = False

    for comment_index, comment in enumerate(comments):
        comment_id = comment.get("id")

        if isinstance(comment_id, int):
            if (
                last_numeric_comment_id is not None
                and comment_id < last_numeric_comment_id
            ):
                server_order_invalid = True
                malformed.append(
                    _problem(
                        "comment_order",
                        "comment ids decreased; input is not in GitHub server order",
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                )
            last_numeric_comment_id = comment_id

        body = comment.get("body")
        if not isinstance(body, str):
            malformed.append(
                _problem(
                    "comment_body",
                    "comment body must be a string",
                    comment_index=comment_index,
                    comment_id=comment_id,
                )
            )
            continue

        for event in _extract_events(
            body,
            comment_index=comment_index,
            comment_id=comment_id,
        ):
            fields = event["fields"]
            run_id = fields.get("RUN_ID", "").strip()

            if not run_id:
                malformed.append(
                    _problem(
                        "missing_run_id",
                        "ownership event has no RUN_ID",
                        event=event,
                    )
                )
                continue

            state = states.get(run_id)
            event_type = event["type"]

            if event_type == "claim":
                if state is not None:
                    malformed.append(
                        _problem(
                            "duplicate_claim",
                            "RUN_ID has more than one CLAIM_V1",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    _mark_ambiguous(
                        state,
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                    continue

                ambiguous = False

                missing_identity = _missing_claim_identity_fields(fields)
                if missing_identity:
                    malformed.append(
                        _problem(
                            "missing_claim_fields",
                            "CLAIM_V1 missing required fields: "
                            + ", ".join(missing_identity),
                            event=event,
                            run_id=run_id,
                        )
                    )
                    ambiguous = True

                lease_text = fields.get("LEASE_UNTIL", "").strip()
                lease_dt: datetime | None = None
                if not lease_text:
                    malformed.append(
                        _problem(
                            "missing_lease",
                            "CLAIM_V1 has no LEASE_UNTIL",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    ambiguous = True
                else:
                    try:
                        lease_dt = parse_instant(lease_text)
                    except ValueError as exc:
                        malformed.append(
                            _problem(
                                "invalid_lease",
                                str(exc),
                                event=event,
                                run_id=run_id,
                            )
                        )
                        ambiguous = True

                claimed_at_text = fields.get("CLAIMED_AT", "").strip()
                claimed_at: datetime | None = None
                if not claimed_at_text:
                    malformed.append(
                        _problem(
                            "missing_claimed_at",
                            "CLAIM_V1 has no CLAIMED_AT",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    ambiguous = True
                else:
                    try:
                        claimed_at = parse_instant(claimed_at_text)
                    except ValueError as exc:
                        malformed.append(
                            _problem(
                                "invalid_claimed_at",
                                str(exc),
                                event=event,
                                run_id=run_id,
                            )
                        )
                        ambiguous = True

                state = {
                    "run_id": run_id,
                    "task_id": fields.get("TASK_ID", "").strip() or None,
                    "semantic_key": fields.get("SEMANTIC_KEY", "").strip() or None,
                    "account_id": fields.get("ACCOUNT_ID", "").strip() or None,
                    "claim_mode": fields.get("CLAIM_MODE", "").strip() or None,
                    "intended_slice": fields.get("INTENDED_SLICE", "").strip() or None,
                    "claim_order": len(original_claim_order),
                    "claim_comment_index": comment_index,
                    "claim_comment_id": comment_id,
                    "claimed_at": (
                        _format_instant(claimed_at) if claimed_at else None
                    ),
                    "lease_until": _format_instant(lease_dt) if lease_dt else None,
                    "_lease_dt": lease_dt,
                    "latest_event": "claim",
                    "latest_comment_index": comment_index,
                    "latest_comment_id": comment_id,
                    "ambiguous": ambiguous,
                }
                states[run_id] = state
                original_claim_order.append(run_id)
                continue

            if event_type == "renew":
                renewal_header = event["header"]
                if state is None:
                    malformed.append(
                        _problem(
                            "renew_without_claim",
                            f"{renewal_header} precedes any CLAIM_V1",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    continue

                if state["latest_event"] == "release":
                    malformed.append(
                        _problem(
                            "renew_after_release",
                            f"{renewal_header} occurs after CLAIM_RELEASE_V1",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    _mark_ambiguous(
                        state,
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                    continue

                lease_text = fields.get("LEASE_UNTIL", "").strip()
                if not lease_text:
                    malformed.append(
                        _problem(
                            "missing_lease",
                            f"{renewal_header} has no LEASE_UNTIL",
                            event=event,
                            run_id=run_id,
                        )
                    )
                    _mark_ambiguous(
                        state,
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                    continue

                try:
                    lease_dt = parse_instant(lease_text)
                except ValueError as exc:
                    malformed.append(
                        _problem(
                            "invalid_lease",
                            str(exc),
                            event=event,
                            run_id=run_id,
                        )
                    )
                    _mark_ambiguous(
                        state,
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                    continue

                identity_conflicts: list[str] = []
                for source_key, output_key in RENEW_IMMUTABLE_FIELDS.items():
                    supplied = fields.get(source_key, "").strip()
                    if supplied and supplied != (state.get(output_key) or ""):
                        identity_conflicts.append(source_key)

                if identity_conflicts:
                    malformed.append(
                        _problem(
                            "renew_identity_conflict",
                            f"{renewal_header} conflicts with immutable claim fields: "
                            + ", ".join(identity_conflicts),
                            event=event,
                            run_id=run_id,
                        )
                    )
                    _mark_ambiguous(
                        state,
                        comment_index=comment_index,
                        comment_id=comment_id,
                    )
                    continue

                state["_lease_dt"] = lease_dt
                state["lease_until"] = _format_instant(lease_dt)
                state["latest_event"] = "renew"
                state["latest_comment_index"] = comment_index
                state["latest_comment_id"] = comment_id
                # A renewal extends only the lease. It cannot rehabilitate or
                # rewrite malformed/missing identity from the original claim.
                continue

            # A later release terminates the run immediately. This is true even
            # if an earlier event was malformed; release cannot grant authority.
            if state is None:
                malformed.append(
                    _problem(
                        "release_without_claim",
                        "CLAIM_RELEASE_V1 precedes any CLAIM_V1",
                        event=event,
                        run_id=run_id,
                    )
                )
                continue

            state["latest_event"] = "release"
            state["latest_comment_index"] = comment_index
            state["latest_comment_id"] = comment_id
            state["release_reason"] = fields.get("REASON")
            state["release_evidence"] = fields.get("EVIDENCE")
            state["ambiguous"] = False

    # Numeric GitHub issue-comment ids are monotonic in server order. Once that
    # invariant is broken, event precedence cannot be trusted. Preserve the
    # diagnostic evidence but grant no ownership from the affected history.
    if server_order_invalid:
        for run_id in original_claim_order:
            state = states[run_id]
            state["ambiguous"] = True
            state["latest_event"] = "ambiguous"

    live_runs: list[dict[str, Any]] = []
    released_runs: list[dict[str, Any]] = []
    expired_runs: list[dict[str, Any]] = []
    ambiguous_runs: list[dict[str, Any]] = []

    for run_id in original_claim_order:
        state = states[run_id]

        if state["latest_event"] == "release":
            released_runs.append(_public_state(state))
            continue

        if (
            state.get("ambiguous")
            or state["latest_event"] == "ambiguous"
            or state.get("_lease_dt") is None
        ):
            ambiguous_runs.append(_public_state(state))
            continue

        if state["_lease_dt"] <= resolved_now:
            expired_runs.append(_public_state(state))
            continue

        live_runs.append(_public_state(state))

    live_runs.sort(key=lambda item: item["claim_order"])

    if capacity is None:
        admitted_runs = list(live_runs)
        collision_candidates: list[dict[str, Any]] = []
    else:
        admitted_runs = live_runs[:capacity]
        collision_candidates = live_runs[capacity:]

    return {
        "now": _format_instant(resolved_now),
        "capacity": capacity,
        "live_runs": live_runs,
        "admitted_runs": admitted_runs,
        "collision_candidates": collision_candidates,
        "released_runs": released_runs,
        "expired_runs": expired_runs,
        "ambiguous_runs": ambiguous_runs,
        "malformed_events": malformed,
    }


def _load_comments(path: str) -> Sequence[Mapping[str, Any]]:
    if path == "-":
        payload = json.load(__import__("sys").stdin)
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

    if isinstance(payload, dict) and isinstance(payload.get("comments"), list):
        payload = payload["comments"]

    if not isinstance(payload, list):
        raise ValueError("input JSON must be a comment list or {'comments': [...]}")

    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("every comment entry must be a JSON object")

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fold Autosport CLAIM_V1/CLAIM_HEARTBEAT_V1/renew/release "
            "events from server-ordered GitHub issue comments."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to issue-comment JSON, or '-' for stdin.",
    )
    parser.add_argument(
        "--now",
        required=True,
        help="Deterministic offset-aware ISO-8601 instant used for lease expiry.",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=None,
        help="Optional task capacity; admitted_runs are the first live claims.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        comments = _load_comments(args.input)
        result = resolve_comments(
            comments,
            now=args.now,
            capacity=args.capacity,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
