"""Describe adjacent observed live prices, never bookmaker fills or wagers."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from prospective_capture import verify_session
from record import instant


def _id(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("missing source identifier")
    return value


def _market(match, server_time):
    """Return source-order names and selections only when one market is sound."""
    try:
        fixture = _id(match.get("id"))
        if match.get("sport") != "table_tennis" or match.get("in_play") is not True:
            return None
        if match.get("stale", False) is not False:
            return None
        home, away, league = (_id(match.get(k)) for k in ("home", "away", "league"))
        if home == away:
            return None
        status = match.get("group_status", {}).get("240")
        if not isinstance(status, dict) or status.get("stale", False) is not False:
            return None
        checked = instant(status.get("polled_at"))
        if not 0 <= (server_time - checked).total_seconds() <= 90:
            return None
        selections = {}
        for quote in match.get("quotes", []):
            if str(quote.get("group_id")) != "240" or str(quote.get("market_id")) != "920054":
                continue
            if quote.get("line") not in (None, "") or quote.get("handicap") not in (None, ""):
                continue
            if (quote.get("group_name") != "Match" or quote.get("period") not in (None, "") or
                    quote.get("market_key") not in (None, "")):
                return None
            side = quote.get("side") or quote.get("label")
            if side not in (home, away):
                continue
            # Duplicate or invalid competing records must never be silently skipped.
            if side in selections:
                return None
            if (quote.get("fixture_id") != fixture or quote.get("phase") != "inplay" or
                    quote.get("suspended", False) is not False or
                    (quote.get("label") not in (None, "", side))):
                return None
            selection = _id(quote.get("sel_id"))
            raw = quote.get("odds_dec")
            if type(raw) not in (int, float):
                return None
            price = Decimal(str(raw))
            if not price.is_finite() or price <= 1 or instant(quote.get("ts")) > checked:
                return None
            selections[side] = (selection, price)
        if set(selections) != {home, away} or len({v[0] for v in selections.values()}) != 2:
            return None
        return (home, away, league, selections)
    except (AttributeError, TypeError, ValueError, InvalidOperation):
        return None


def _snapshot(directory, seq, receipt):
    raw = (Path(directory) / f"{seq:04d}.odds.body.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError("snapshot changed after verification")
    board = strict_json_loads(raw.decode("utf-8"))
    server = instant(board.get("server_time"))
    fixtures = {}
    for m in board["matches"]:
        ident = _id(m.get("id"))
        if ident in fixtures:
            raise ValueError("duplicate fixture ID in snapshot")
        fixtures[ident] = _market(m, server)
    return fixtures


def audit(directory):
    verified = verify_session(directory)
    sequences = sorted(seq for seq, kind in verified if kind == "odds")
    if len(sequences) < 2:
        raise ValueError("at least two odds snapshots required")
    transitions = []
    totals = Counter()
    for left, right in zip(sequences, sequences[1:]):
        a, b = verified[(left, "odds")], verified[(right, "odds")]
        elapsed = (instant(b["received_at"]) - instant(a["received_at"])).total_seconds()
        if not 30 <= elapsed <= 60:
            totals["intervals_outside_30_60_seconds"] += 1
            continue
        original = _snapshot(directory, left, a)
        later = _snapshot(directory, right, b)
        counts = Counter()
        for fixture, state in original.items():
            if state is None:
                continue
            home, away, league, selections = state
            side = home if selections[home][1] <= selections[away][1] else away
            old_sel, old_price = selections[side]
            counts["selected"] += 1
            next_state = later.get(fixture)
            if next_state is None or next_state[:3] != (home, away, league):
                counts["missing_or_invalid"] += 1
                continue
            next_selections = next_state[3]
            # Both sides must remain bound to the same two provider selections.
            if any(next_selections[s][0] != selections[s][0] for s in (home, away)):
                counts["missing_or_invalid"] += 1
                continue
            new_price = next_selections[side][1]
            counts["lower" if new_price < old_price else "higher" if new_price > old_price else "unchanged"] += 1
        n = counts["selected"]
        comparable = counts["lower"] + counts["higher"] + counts["unchanged"]
        no_lower = counts["higher"] + counts["unchanged"]
        counts["initial_fixture_pairs"] = n * (n - 1) // 2
        counts["both_legs_comparable_pairs"] = comparable * (comparable - 1) // 2
        counts["comparable_pairs_at_least_one_lower"] = counts["both_legs_comparable_pairs"] - no_lower * (no_lower - 1) // 2
        totals.update(counts)
        transitions.append({"from_sequence": left, "to_sequence": right,
                            "delay_seconds": elapsed, **{k: counts[k] for k in
                            ("selected", "lower", "unchanged", "higher", "missing_or_invalid",
                             "initial_fixture_pairs", "both_legs_comparable_pairs", "comparable_pairs_at_least_one_lower")}})
    keys = ("selected", "lower", "unchanged", "higher", "missing_or_invalid", "initial_fixture_pairs",
            "both_legs_comparable_pairs", "comparable_pairs_at_least_one_lower", "intervals_outside_30_60_seconds")
    return {"schema": "research.tt_quote_delay_audit.v1", "transitions": transitions,
            "totals": {k: totals[k] for k in keys}, "quotes_are_fills": False,
            "identity_verified": False, "rights_verified": False, "real_money_execution": False,
            "note": "Previously inspected, repeated correlated snapshots; no interpolation or accepted fills."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    report = audit(args.session)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2); stream.write("\n")


if __name__ == "__main__":
    main()
