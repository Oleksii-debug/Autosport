"""Research comparison of match-point totals against the live winner market.

Uses an independent-point model to cross-price two markets from the same
provider. Archived ticks are not contemporaneous observations or actual fills.
The format and market interpretation are explicit research assumptions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from autosport.json_integrity import strict_json_loads
from early_set_economics import (
    _action, _market_probability, _report, _score, _winner, decide, match_probability,
)
from prospective_capture import _write_exclusive
from record import instant

MAX_TICK_AGE = 15
POLICIES = ("market", "winner_implied_points")


class AmbiguousPriceHistory(ValueError):
    pass


def game_probability(p, home, away):
    """First to 11 points with a two-point margin; deuce tail is exact."""
    deuce = p * p / (p * p + (1 - p) ** 2)

    @lru_cache(maxsize=None)
    def win(h, a):
        if max(h, a) >= 11 and abs(h - a) >= 2:
            return float(h > a)
        if h >= 10 and a >= 10:
            return {0: deuce, 1: p + (1 - p) * deuce, -1: p * deuce}[h - a]
        return p * win(h + 1, a) + (1 - p) * win(h, a + 1)

    return win(home, away)


def implied_point_skill(market_home, sets, points):
    """Find the per-point skill consistent with the current live winner price."""
    low, high = 0.0, 1.0
    for _ in range(48):
        p = (low + high) / 2
        q = min(max(game_probability(p, 0, 0), 1e-15), 1 - 1e-15)
        current = game_probability(p, *points)
        h, a = sets
        value = (current * match_probability(q, h + 1, a)
                 + (1 - current) * match_probability(q, h, a + 1))
        if value < market_home:
            low = p
        else:
            high = p
    return (low + high) / 2


def over_probability(p, sets, points, need):
    """Probability of at least need further points before the match ends."""
    if not 0 < p < 1 or type(need) is not int or need > 141:
        raise ValueError("unsupported point-probability inputs")

    @lru_cache(maxsize=None)
    def over(h, a, hp, ap, left):
        if left <= 0:
            return 1.0
        if max(h, a) == 3:
            return 0.0
        if max(hp, ap) >= 11 and abs(hp - ap) >= 2:
            return over(h + int(hp > ap), a + int(ap > hp), 0, 0, left)
        return p * over(h, a, hp + 1, ap, left - 1) + (1 - p) * over(h, a, hp, ap + 1, left - 1)

    return min(1.0, max(0.0, over(*sets, *points, need)))


def _admissible_tape(directory, fixture):
    if not isinstance(fixture, str) or not fixture.isascii() or not fixture.isdecimal():
        raise ValueError("expected a numeric source fixture")
    raw_path = directory / f"{fixture}.body.json"
    receipt_path = directory / f"{fixture}.receipt.json"
    if not raw_path.is_file() and not receipt_path.is_file():
        return None
    raw = raw_path.read_bytes()
    receipt = strict_json_loads(receipt_path.read_text("utf-8"))
    url = urlsplit(receipt["url"])
    query = parse_qs(url.query, strict_parsing=True)
    if ((url.scheme, url.netloc, url.path, url.fragment) != ("https", "moviodds.com", "/history", "")
            or query != {"fi": [fixture], "group": ["240"], "limit": ["2500"]}
            or instant(receipt["received_at"]) < instant(receipt["requested_at"])
            or receipt["bytes"] != len(raw) or len(raw) > 4 * 1024 * 1024
            or receipt["sha256"] != hashlib.sha256(raw).hexdigest()):
        raise ValueError("history receipt does not bind the expected source")
    tape = strict_json_loads(raw.decode("utf-8"))
    if (tape.get("fixture_id") != fixture or tape.get("fixture_ids") != [fixture]
            or tape.get("truncated") is not False or not isinstance(tape.get("points"), list)
            or tape.get("count") != len(tape["points"]) or str(tape.get("group")) != "240"):
        raise ValueError("unexpected, aliased or truncated price history")
    return tape["points"]


def _total_at(tape, cutoff):
    """Use last state per side and require a fresh, same-tick exact pair."""
    latest, signatures = {}, {}
    for row in tape:
        if (str(row.get("group_id")) != "240" or row.get("group_name") != "Match"
                or row.get("side") not in ("Over", "Under") or row.get("line") != row.get("handicap")):
            continue
        try:
            timestamp = instant(row["ts"])
            line = Decimal(str(row["line"]))
        except (InvalidOperation, KeyError, TypeError, ValueError, AttributeError):
            continue
        if not line.is_finite() or not 40 < line < 141 or line % 1 != Decimal("0.5") or timestamp > cutoff:
            continue
        market_id, selection_id = row.get("market_id"), row.get("sel_id")
        if not isinstance(market_id, str) or not market_id or not isinstance(selection_id, str) or not selection_id:
            continue
        key = market_id, line, row["side"]
        signature = selection_id, row.get("odds_dec"), row.get("suspended")
        event = (*key, timestamp)
        if signatures.setdefault(event, signature) != signature:
            raise AmbiguousPriceHistory("conflicting price rows at one timestamp")
        old = latest.get(key)
        if old is None or timestamp > old[0]:
            latest[key] = timestamp, row
    choices = []
    for (market_id, line, side), (tick, over) in latest.items():
        if side != "Over" or not 0 <= (cutoff - tick).total_seconds() <= MAX_TICK_AGE:
            continue
        partner = latest.get((market_id, line, "Under"))
        if partner is None or partner[0] != tick:
            continue
        under = partner[1]
        if (over.get("suspended") is not False or under.get("suspended") is not False
                or over["sel_id"] == under["sel_id"]):
            continue
        try:
            odds = tuple(Decimal(str(r["odds_dec"])) for r in (over, under))
        except (InvalidOperation, KeyError):
            continue
        if not all(o.is_finite() and 1 < o <= 1000 for o in odds) or sum(1 / o for o in odds) < 1:
            continue
        choices.append((tick, line, odds))
    if not choices:
        return None
    newest = max(tick for tick, _, _ in choices)
    # Same-time lines: the most balanced displayed price, then the lower line.
    freshest = [(line, odds) for tick, line, odds in choices if tick == newest]
    return min(freshest, key=lambda item: (abs(_market_probability(item[1]) - .5), item))


def _scored_points(validated_score):
    return sum(int(point) for game in validated_score.split(",") for point in game.split("-"))


def analyze(raw, directory, *, expected_sha256, assumed_best_of_five_leagues):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("recording SHA-256 mismatch")
    data = strict_json_loads(raw.decode("utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != "autosport.table_tennis_recording.v1"
            or data.get("source") != "https://moviodds.com" or not isinstance(data.get("matches"), list)):
        raise ValueError("unsupported table tennis recording")
    leagues = frozenset(assumed_best_of_five_leagues)
    if not leagues:
        raise ValueError("declare the assumed format scope before analysis")
    counts, records, ids = Counter(), [], set()
    for match in data["matches"]:
        source_ids = {match.get("id"), match.get("live_id")} - {None}
        if not match.get("id") or ids.intersection(source_ids):
            raise ValueError("duplicate or missing recorded source identity")
        ids.update(source_ids)
        if match.get("league") not in leagues:
            counts["format_out_of_scope"] += 1
            continue
        category, anchor = decide(match.get("prematch") or {}, match["frames"], match.get("live_id"),
                                  first_live_at=match.get("first_live_at"))
        if anchor is None:
            counts[category] += 1
            continue
        tape = _admissible_tape(Path(directory), match["live_id"])
        if tape is None:
            counts["missing_exact_tape"] += 1
            continue
        try:
            total = _total_at(tape, anchor["time"])
        except AmbiguousPriceHistory:
            counts["conflicting_source_prices"] += 1
            continue
        if total is None:
            counts["no_fresh_synchronized_total"] += 1
            continue
        line, prices = total
        sets, points = _score(anchor["score"])
        points = points or (0, 0)
        p = implied_point_skill(anchor["probabilities"]["market"], sets, points)
        model = over_probability(p, sets, points, int(line) + 1 - _scored_points(anchor["score"]))
        if not 0 < model < 1:
            counts["already_known_or_degenerate_total"] += 1
            continue
        probs = {"market": _market_probability(prices), "winner_implied_points": model}
        decision = {"prices": prices, "probabilities": probs,
                    "actions": {policy: _action(value, prices) for policy, value in probs.items()}}
        winner = None
        if _winner(match, anchor["time"], anchor["score"]) is not None:
            winner = int(_scored_points(match["result"]["final_score"]) < line)
        records.append((decision, winner))
        counts["selected_frame"] += 1
    return {"schema": "research.tt_live_point_total_economics.v1", "recording_sha256": digest,
            "assumed_best_of_five_leagues": sorted(leagues), "recorded_matches": len(data["matches"]),
            "max_same_tick_age_seconds": MAX_TICK_AGE, "counts": dict(sorted(counts.items())),
            "policies": _report(records, policies=POLICIES), "independent_holdout": False,
            "market_semantics_independently_verified": False, "execution_verified": False,
            "model_training_authorized": False, "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("tape_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--assumed-best-of-five-league", action="append", required=True)
    args = parser.parse_args()
    report = analyze(args.recording.read_bytes(), args.tape_directory,
                     expected_sha256=args.expected_sha256,
                     assumed_best_of_five_leagues=args.assumed_best_of_five_league)
    _write_exclusive(args.output, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
