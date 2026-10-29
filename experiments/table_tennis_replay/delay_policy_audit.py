"""Causal, observational delayed-quote sensitivity; never an execution simulator.

Select from the first received snapshot only. A later observed quote is not a
fill, a parlay offer, or a settlement. Use the existing receipt and market
checks; do not join participants across sources or invent missing prices.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, localcontext
import hashlib
from itertools import combinations
import json
from pathlib import Path

from prospective_capture import verify_session
from quote_delay_audit import _snapshot
from record import instant


CATEGORIES = ("missing_or_invalid", "below_98_percent", "from_98_to_below_100_percent", "at_least_100_percent")


def _classify(first, second):
    """Compare exact product values with a fixed 98% boundary, without division."""
    if first is None or second is None:
        return "missing_or_invalid"
    old_a, new_a = first
    old_b, new_b = second
    with localcontext() as ctx:
        ctx.prec = sum(len(x.as_tuple().digits) for x in (old_a, new_a, old_b, new_b)) + 8
        old = old_a * old_b
        new = new_a * new_b
        if new < Decimal("0.98") * old:
            return "below_98_percent"
        if new < old:
            return "from_98_to_below_100_percent"
        return "at_least_100_percent"


def _choices(snapshot):
    """Freeze IDs and sides without looking at a subsequent observation."""
    choices = {}
    for fixture, state in snapshot.items():
        if state is None:
            continue
        home, away, _league, selections = state
        side = home if selections[home][1] <= selections[away][1] else away
        choices[fixture] = (state, side, selections[side][1])
    return choices


def _exact_snapshot(directory, seq, receipt):
    """Reuse eligibility checks but recover the original decimal JSON numbers.

    The canonical strict reader validates the entire raw JSON first, then the
    identical bytes are decoded with Decimal to avoid binary-float rounding.
    """
    eligible = _snapshot(directory, seq, receipt)
    raw = (Path(directory) / f"{seq:04d}.odds.body.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError("snapshot changed after verification")
    board = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    matches = {row["id"]: row for row in board["matches"]}
    exact = {}
    for fixture, state in eligible.items():
        if state is None:
            exact[fixture] = None
            continue
        home, away, league, selections = state
        prices = {}
        for side in (home, away):
            selection = selections[side][0]
            source = [quote for quote in matches[fixture]["quotes"]
                      if str(quote.get("group_id")) == "240"
                      and str(quote.get("market_id")) == "920054"
                      and quote.get("line") in (None, "")
                      and quote.get("handicap") in (None, "")
                      and (quote.get("side") or quote.get("label")) == side
                      and quote.get("sel_id") == selection]
            if len(source) != 1 or type(source[0].get("odds_dec")) not in (int, Decimal):
                raise ValueError("cannot recover unique exact source price")
            price = Decimal(source[0]["odds_dec"])
            if not price.is_finite() or price <= 1:
                raise ValueError("invalid exact source price")
            prices[side] = (selection, price)
        exact[fixture] = (home, away, league, prices)
    return exact


def _later_price(original, later):
    state, side, old_price = original
    if later is None or later[:3] != state[:3]:
        return None
    if any(later[3][name][0] != state[3][name][0] for name in state[:2]):
        return None
    return old_price, later[3][side][1]


def _counts_template():
    return {"initial_pairs": 0, **{key: 0 for key in CATEGORIES}}


def audit(directory):
    verified = verify_session(directory)
    sequences = sorted(seq for seq, kind in verified if kind == "odds")
    if len(sequences) < 2:
        raise ValueError("at least two odds snapshots required")
    all_counts, chosen_counts = Counter(), Counter()
    intervals_outside_window = 0
    transitions = []
    for left, right in zip(sequences, sequences[1:]):
        old_receipt, new_receipt = verified[(left, "odds")], verified[(right, "odds")]
        delay = (instant(new_receipt["received_at"]) - instant(old_receipt["received_at"])).total_seconds()
        if not 30 <= delay <= 60:
            intervals_outside_window += 1
            transitions.append({"from_sequence": left, "to_sequence": right, "delay_seconds": delay,
                                "included": False})
            continue
        initial = _choices(_exact_snapshot(directory, left, old_receipt))
        # Fixed before the next snapshot is inspected. IDs are only used inside
        # the private calculation; the aggregate output does not expose names.
        first_two = sorted(initial)[:2]
        pairs = combinations(sorted(initial), 2)
        later = _exact_snapshot(directory, right, new_receipt)
        comparison = {ident: _later_price(choice, later.get(ident)) for ident, choice in initial.items()}
        totals, chosen = Counter(), Counter()
        for a, b in pairs:
            category = _classify(comparison[a], comparison[b])
            totals["initial_pairs"] += 1
            totals[category] += 1
            if [a, b] == first_two:
                chosen["initial_pairs"] += 1
                chosen[category] += 1
        all_counts.update(totals)
        chosen_counts.update(chosen)
        transitions.append({"from_sequence": left, "to_sequence": right, "delay_seconds": delay,
                            "included": True, "eligible_initial_fixtures": len(initial),
                            "all_pairs": {k: totals[k] for k in _counts_template()},
                            "first_pair": {k: chosen[k] for k in _counts_template()}})
    return {"schema": "research.tt_delayed_quote_policy.v1", "transitions": transitions,
            "intervals_outside_30_60_seconds": intervals_outside_window,
            "all_pairs": {k: all_counts[k] for k in _counts_template()},
            "first_pair_per_transition": {k: chosen_counts[k] for k in _counts_template()},
            "threshold": "0.98", "displayed_prices_are_fills": False,
            "bookmaker_parlay_price_verified": False, "real_money_execution": False,
            "identity_verified": False, "rights_verified": False,
            "note": "Observational products; dependent matches and pairs; no interpolation, outcome or profit."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = audit(args.session)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
