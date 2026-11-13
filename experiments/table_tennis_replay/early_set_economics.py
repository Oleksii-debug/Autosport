"""Research-only live match-winner replay from pre-match price and early set score.

One observation per fixture, before selection or settlement. The declared
best-of-five scope is an input fixed before scoring, never inferred from the
winner. Source quotes are not fills. No training, execution or promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

from autosport.json_integrity import strict_json_loads
from causal_strength_frozen import PAPER_PROTOCOL
from prospective_capture import _write_exclusive
from record import instant


POLICIES = ("market", "fixed_skill", "adaptive_skill")
PRIOR_SETS = 8
MAX_EARLY_POINTS = 6
MAX_LIVE_AGE_SECONDS = 60
MAX_PREMATCH_PRICE_AGE_SECONDS = 1800
STRESS_PAYOUT_HAIRCUT = Decimal("0.05")


def _score(value):
    """Accept completed games followed by at most one unfinished game."""
    if not isinstance(value, str) or not value:
        return None
    pieces = value.split(",")
    if len(pieces) > 5:
        return None
    wins = [0, 0]
    unfinished = None
    for i, piece in enumerate(pieces):
        match = re.fullmatch(r"([0-9]{1,3})-([0-9]{1,3})", piece.strip())
        if match is None or max(wins) == 3:
            return None
        home, away = map(int, match.groups())
        high, low = max(home, away), min(home, away)
        complete = (high == 11 and low <= 9) or (high > 11 and high - low == 2)
        if complete:
            wins[int(away > home)] += 1
        elif i == len(pieces) - 1 and (high < 11 or high - low <= 1):
            unfinished = (home, away)
        else:
            return None
    return tuple(wins), unfinished


def match_probability(q, home=0, away=0, *, prior_sets=None):
    """First to three. Optional beta prior integrates future set uncertainty."""
    if not 0 < q < 1 or type(home) is not int or type(away) is not int or (
            min(home, away) < 0 or max(home, away) > 3 or home == away == 3):
        raise ValueError("invalid best-of-five state")
    if prior_sets is not None and (type(prior_sets) is not int or prior_sets <= 0):
        raise ValueError("invalid prior set weight")

    @lru_cache(maxsize=None)
    def win(h, a):
        if h == 3:
            return 1.0
        if a == 3:
            return 0.0
        p = q if prior_sets is None else (q * prior_sets + h) / (prior_sets + h + a)
        return p * win(h + 1, a) + (1 - p) * win(h, a + 1)

    return win(home, away)


def _set_skill(prematch_probability, *, prior_sets=None):
    # Invert the same pre-match model as used live, so both start at the
    # same market prior even when uncertainty is integrated.
    low, high = 0.0, 1.0
    for _ in range(52):
        middle = (low + high) / 2
        if match_probability(middle, prior_sets=prior_sets) < prematch_probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _price_pair(value, observed, max_age):
    if not isinstance(value, dict):
        return None
    parsed, stamps = [], []
    for side in ("home", "away"):
        row = value.get(side)
        if not isinstance(row, dict) or row.get("stale") is not False or row.get("suspended") is not False:
            return None
        price = row.get("odds")
        if isinstance(price, bool) or not isinstance(price, (int, float, str, Decimal)):
            return None
        try:
            amount = Decimal(str(price))
            stamp = instant(row["market_checked_at"])
        except (InvalidOperation, KeyError, TypeError, ValueError, AttributeError):
            return None
        if not amount.is_finite() or not 1 < amount <= 1000 or not 0 <= (observed - stamp).total_seconds() <= max_age:
            return None
        parsed.append(amount)
        stamps.append(stamp)
    if stamps[0] != stamps[1] or sum(1 / price for price in parsed) < 1:
        return None
    return tuple(parsed)


def _market_probability(prices):
    return float(prices[1] / sum(prices))


def _action(p, odds):
    cost = Decimal(PAPER_PROTOCOL["cost_per_ticket"])
    haircut = Decimal(PAPER_PROTOCOL["winning_payout_haircut"])
    floor = Decimal(PAPER_PROTOCOL["min_edge_units"])
    p = Decimal(str(p))
    edges = (p * odds[0] * (1 - haircut) - 1 - cost,
             (1 - p) * odds[1] * (1 - haircut) - 1 - cost)
    if edges[0] == edges[1] or max(edges) < floor:
        return None
    return int(edges[1] > edges[0])


def decide(prematch, frames, live_id, *, first_live_at):
    """Build a decision from the first eligible early frame, without a result."""
    try:
        pre_time = instant(prematch["captured_at"])
        first_live = instant(first_live_at)
    except (KeyError, TypeError, ValueError, AttributeError):
        return "missing_chronology", None
    if not pre_time < first_live or not isinstance(live_id, str) or not live_id:
        return "missing_chronology", None
    pre = _price_pair(prematch.get("odds"), pre_time, MAX_PREMATCH_PRICE_AGE_SECONDS)
    if pre is None:
        return "prematch_price_unavailable", None
    prior = _market_probability(pre)
    previous_time = None
    for frame in frames:
        if frame.get("kind") != "live":
            continue
        try:
            seen = instant(frame["observed_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return "missing_chronology", None
        if frame.get("source_id") != live_id or seen < first_live or (
                previous_time is not None and seen <= previous_time):
            return "live_id_or_chronology_conflict", None
        previous_time = seen
        if frame.get("source_stale") is not False:
            continue
        score = _score(frame.get("score"))
        if score is None:
            continue
        (home, away), points = score
        if home + away != 1 or points is not None and sum(points) > MAX_EARLY_POINTS:
            continue
        try:
            score_seen = instant(frame["source_last_live_seen"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not 0 <= (seen - score_seen).total_seconds() <= MAX_LIVE_AGE_SECONDS:
            continue
        prices = _price_pair(frame.get("odds"), seen, MAX_LIVE_AGE_SECONDS)
        if prices is None:
            continue
        probs = {
            "market": _market_probability(prices),
            "fixed_skill": match_probability(_set_skill(prior), home, away),
            "adaptive_skill": match_probability(_set_skill(prior, prior_sets=PRIOR_SETS),
                                                home, away, prior_sets=PRIOR_SETS),
        }
        return "selected_frame", {"time": seen, "prices": prices, "probabilities": probs,
                                  "score": frame["score"],
                                  "actions": {policy: _action(p, prices) for policy, p in probs.items()}}
    return "no_fresh_early_frame", None


def _winner(match, after, observed_score):
    result = match.get("result")
    if not isinstance(result, dict) or result.get("source_fixture_id") not in (match["id"], match["live_id"]):
        return None
    try:
        observed = instant(result["observed_at"])
        finished = instant(result["source_finished_at"])
        scores = ",".join(period["score"] for period in result["periods"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not after < finished <= observed or scores != result.get("final_score"):
        return None
    score = _score(scores)
    if score is None or score[1] is not None or max(score[0]) != 3:
        return None
    seen = _score(observed_score)
    if seen is None:
        return None
    completed, unfinished = sum(seen[0]), seen[1]
    seen_games = tuple(tuple(map(int, game.split("-"))) for game in observed_score.split(","))
    final_games = tuple(tuple(map(int, game.split("-"))) for game in scores.split(","))
    if final_games[:completed] != seen_games[:completed]:
        return None
    if unfinished is not None and (len(final_games) <= completed or any(
            final < earlier for final, earlier in zip(final_games[completed], unfinished, strict=True))):
        return None
    return int(score[0][1] == 3)


def _report(records, *, policies=POLICIES):
    cost = Decimal(PAPER_PROTOCOL["cost_per_ticket"])
    haircut = Decimal(PAPER_PROTOCOL["winning_payout_haircut"])
    report = {}
    for policy in policies:
        chosen = settled = wins = scored = 0
        net = stress = candidate_expected = market_expected = Decimal(0)
        brier = log_loss = 0.0
        for decision, winner in records:
            p = decision["probabilities"][policy]
            action = decision["actions"][policy]
            chosen += int(action is not None)
            if winner is None:
                continue
            scored += 1
            target = int(winner == 0)
            brier += (p - target) ** 2
            log_loss -= math.log(p if target else 1 - p)
            if action is None:
                continue
            settled += 1
            won = action == winner
            wins += int(won)
            odds = decision["prices"][action]
            side_p = p if action == 0 else 1 - p
            market_p = decision["probabilities"]["market"]
            market_side_p = market_p if action == 0 else 1 - market_p
            candidate_expected += Decimal(str(side_p)) * odds * (1 - haircut) - 1 - cost
            market_expected += Decimal(str(market_side_p)) * odds * (1 - haircut) - 1 - cost
            net += (odds * (1 - haircut) if won else 0) - 1 - cost
            stress += (odds * (1 - STRESS_PAYOUT_HAIRCUT) if won else 0) - 1 - cost
        report[policy] = {
            "scored_matches": scored, "brier": brier / scored if scored else None,
            "log_loss": log_loss / scored if scored else None,
            "paper_selected": chosen, "paper_settled": settled, "paper_won": wins,
            "paper_unresolved": chosen - settled, "paper_net_units": str(net),
            "paper_net_if_unresolved_lose_units": str(net - (chosen - settled) * (1 + cost)),
            "stress_net_units": str(stress),
            "candidate_expected_net_on_settled_units": str(candidate_expected),
            "market_expected_net_on_same_settled_units": str(market_expected),
        }
    return report


def analyze(raw, *, expected_sha256, assumed_best_of_five_leagues):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("recording SHA-256 mismatch")
    data = strict_json_loads(raw.decode("utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != "autosport.table_tennis_recording.v1"
            or data.get("source") != "https://moviodds.com" or not isinstance(data.get("matches"), list)):
        raise ValueError("unsupported table tennis recording")
    counts, records = Counter(), []
    ids = set()
    leagues = frozenset(assumed_best_of_five_leagues)
    if not leagues or any(not isinstance(league, str) or not league for league in leagues):
        raise ValueError("declare the assumed format scope before analysis")
    for match in data["matches"]:
        source_ids = {match.get("id"), match.get("live_id")} - {None}
        if not match.get("id") or ids.intersection(source_ids):
            raise ValueError("duplicate or missing recorded source identity")
        ids.update(source_ids)
        if match.get("league") not in leagues:
            counts["format_out_of_scope"] += 1
            continue
        category, decision = decide(match.get("prematch") or {}, match["frames"], match.get("live_id"),
                                    first_live_at=match.get("first_live_at"))
        counts[category] += 1
        if decision is not None:
            records.append((decision, _winner(match, decision["time"], decision["score"])))
    return {"schema": "research.tt_early_set_economics.v1", "recording_sha256": digest,
            "recorded_matches": len(data["matches"]), "counts": dict(sorted(counts.items())),
            "assumed_best_of_five_leagues": sorted(leagues), "prior_sets": PRIOR_SETS,
            "max_early_points": MAX_EARLY_POINTS, "max_live_age_seconds": MAX_LIVE_AGE_SECONDS,
            "paper_protocol": dict(PAPER_PROTOCOL), "stress_winning_payout_haircut": str(STRESS_PAYOUT_HAIRCUT),
            "policies": _report(records), "independent_holdout": False,
            "format_independently_verified": False, "execution_verified": False,
            "model_training_authorized": False, "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--assumed-best-of-five-league", action="append", required=True)
    args = parser.parse_args()
    report = analyze(args.recording.read_bytes(), expected_sha256=args.expected_sha256,
                     assumed_best_of_five_leagues=args.assumed_best_of_five_league)
    _write_exclusive(args.output, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
