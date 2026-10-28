"""Retrospective, hypothetical outcome-and-odds diagnostic; not production evidence."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REQUIRED = {"match_id", "date", "joueur1_id", "joueur2_id", "vainqueur_id",
            "score", "cote1_ouverture", "cote2_ouverture"}
SOURCE = "https://www.valuebetennis.com/en/donnees.htm"
FEE = Decimal("0.02")
STAKE = Decimal("1")
EDGE = Decimal("0.05")
SET_PATTERN = re.compile(r"\d{1,2}-\d{1,2}(?:\(\d{1,2}\))?")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    # The publisher documents UTC. A naive CSV timestamp is interpreted in UTC,
    # not treated as independent proof of actual result or price availability.
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc))


def parse_odds(value: str) -> Decimal:
    try:
        odds = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("missing or malformed opening odds") from exc
    if (not odds.is_finite() or odds <= 1 or odds > 1000
            or odds.as_tuple().exponent < -6):
        raise ValueError("opening odds outside bounded Decimal support")
    return odds


def valid_score(score: str) -> bool:
    pieces = score.strip().split()
    return len(pieces) >= 2 and all(SET_PATTERN.fullmatch(piece) for piece in pieces)


def load_data(inputs: list[tuple[str, bytes]]) -> tuple[list[dict], list[dict]]:
    rows, provenance, seen = [], [], set()
    for name, raw in inputs:
        provenance.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest()})
        text = raw.decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error as exc:
            raise ValueError("unable to identify CSV delimiter") from exc
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
                or not REQUIRED <= set(reader.fieldnames)):
            raise ValueError("missing or duplicate CSV columns")
        for record in reader:
            if None in record or any(record.get(k) is None for k in REQUIRED):
                raise ValueError("malformed CSV row")
            identity = record["match_id"].strip()
            if not identity or identity in seen:
                raise ValueError("empty or duplicate match identity")
            seen.add(identity)
            at = parse_time(record["date"])
            if at.year < 2021:
                raise ValueError("date precedes supported source coverage")
            player_a, player_b = record["joueur1_id"].strip(), record["joueur2_id"].strip()
            winner = record["vainqueur_id"].strip()
            if not player_a or not player_b or player_a == player_b or winner not in (player_a, player_b):
                raise ValueError("ambiguous player or winner identity")
            score_ok = valid_score(record["score"])
            try:
                odds_a, odds_b = parse_odds(record["cote1_ouverture"]), parse_odds(record["cote2_ouverture"])
            except ValueError:
                odds_a, odds_b = None, None
            rows.append({"id": identity, "at": at, "a": player_a, "b": player_b,
                         "winner": winner, "score_ok": score_ok,
                         "odds_a": odds_a, "odds_b": odds_b})
    if not rows:
        raise ValueError("input is empty")
    return sorted(rows, key=lambda r: (r["at"], r["id"])), provenance


def fresh_arm() -> dict:
    return {"bets": 0, "no_bets": 0, "wins": 0, "losses": 0,
            "turnover": Decimal(0), "gross_profit": Decimal(0),
            "fees": Decimal(0), "net_profit": Decimal(0),
            "peak": Decimal(0), "equity": Decimal(0), "max_drawdown": Decimal(0)}


def record(arm: dict, selected: str | None, row: dict) -> None:
    if selected is None:
        arm["no_bets"] += 1
        return
    odds = row["odds_a"] if selected == row["a"] else row["odds_b"]
    won = selected == row["winner"]
    gross = STAKE * (odds - 1) if won else -STAKE
    fee = gross * FEE if won else Decimal(0)
    net = gross - fee
    arm["bets"] += 1
    arm["wins" if won else "losses"] += 1
    arm["turnover"] += STAKE
    arm["gross_profit"] += gross
    arm["fees"] += fee
    arm["net_profit"] += net
    arm["equity"] += net
    arm["peak"] = max(arm["peak"], arm["equity"])
    arm["max_drawdown"] = max(arm["max_drawdown"], arm["peak"] - arm["equity"])


def audit(inputs: list[tuple[str, bytes]], *, warmup_start: date = date(2021, 1, 1),
          evaluation_start: date = date(2022, 1, 1),
          evaluation_end: date = date(2023, 1, 1), source: str = SOURCE,
          attribution: str = "Valuebetennis, Tennis results and odds since 2021",
          price_description: str = "publisher_reported_opening_odds") -> dict:
    if not warmup_start < evaluation_start < evaluation_end:
        raise ValueError("warmup and evaluation dates must be strictly ordered")
    rows, provenance = load_data(inputs)
    grouped = defaultdict(list)
    for row in rows:
        if warmup_start <= row["at"].date() < evaluation_end:
            grouped[row["at"].date()].append(row)
    ratings = defaultdict(lambda: 1500.0)
    previous_games = defaultdict(int)
    arms = {name: fresh_arm() for name in ("no_bet", "market_favorite", "elo_threshold", "elo_history_guarded")}
    excluded = Counter()
    history_counts = Counter()
    eval_count, warmup_count = 0, 0
    for day, batch in sorted(grouped.items()):
        changes = defaultdict(list)
        for row in batch:
            p = 1.0 / (1.0 + 10.0 ** ((ratings[row["b"]] - ratings[row["a"]]) / 400.0))
            if day >= evaluation_start:
                if not row["score_ok"]:
                    excluded["incomplete_or_ambiguous_score"] += 1
                elif row["odds_a"] is None:
                    excluded["missing_or_invalid_opening_odds"] += 1
                else:
                    eval_count += 1
                    record(arms["no_bet"], None, row)
                    favorite = row["a"] if row["odds_a"] <= row["odds_b"] else row["b"]
                    record(arms["market_favorite"], favorite, row)
                    probability = Decimal(str(p))
                    edge_a = probability - Decimal(1) / row["odds_a"]
                    edge_b = (Decimal(1)-probability) - Decimal(1) / row["odds_b"]
                    selected = (row["a"] if edge_a >= EDGE and edge_a >= edge_b else
                                row["b"] if edge_b >= EDGE else None)
                    record(arms["elo_threshold"], selected, row)
                    history_a, history_b = previous_games[row["a"]], previous_games[row["b"]]
                    history_counts["both_seen" if history_a and history_b else
                                   "one_seen" if history_a or history_b else "none_seen"] += 1
                    record(arms["elo_history_guarded"], selected if history_a and history_b else None, row)
            elif row["score_ok"]:
                warmup_count += 1
            if row["score_ok"]:
                delta = 16.0 * ((1.0 if row["winner"] == row["a"] else 0.0) - p)
                changes[row["a"]].append(delta)
                changes[row["b"]].append(-delta)
        for player, deltas in sorted(changes.items()):
            ratings[player] += math.fsum(deltas)
            previous_games[player] += len(deltas)
    if not warmup_count or not eval_count:
        raise ValueError("need usable warmup and evaluation events")
    output_arms = {}
    for name, state in arms.items():
        output_arms[name] = {k: str(v) if isinstance(v, Decimal) else v
                             for k, v in state.items() if k not in ("peak", "equity")}
        output_arms[name]["roi"] = (str(state["net_profit"] / state["turnover"])
                                     if state["turnover"] else None)
    return {"schema": "autosport.account_c.historical_odds_research.v1",
            "source": {"attribution": attribution,
                       "url": source, "license": "CC BY 4.0", "inputs": provenance,
                       "source_and_rights_independently_verified": False},
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "protocol": {"warmup_start": warmup_start.isoformat(),
                         "evaluation_start": evaluation_start.isoformat(),
                         "evaluation_end_exclusive": evaluation_end.isoformat(),
                         "price_description": price_description,
                         "elo_k": 16, "initial_elo": 1500, "edge_threshold": str(EDGE),
                         "stake_units": str(STAKE), "winning_profit_fee": str(FEE),
                         "same_day_outcomes_available": False,
                         "drawdown_order_assumption": "match_start_time_then_match_id"},
            "truth": {"upstream_point_in_time_verified": False,
                      "actual_price_fills_verified": False, "untouched_holdout": False,
                      "forward_evidence_eligible": False, "promotion_authorized": False,
                      "profitability_claim": False, "real_money_execution": False},
            "counts": {"all_input": len(rows), "warmup_usable": warmup_count,
                       "evaluation_eligible": eval_count, "evaluation_exclusions": dict(excluded),
                       "prior_player_history": dict(history_counts)},
            "arms": output_arms}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {p.resolve() for p in args.input}:
        parser.error("output cannot overwrite input")
    result = audit([(p.name, p.read_bytes()) for p in args.input])
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "arms": result["arms"], "truth": result["truth"]}))


if __name__ == "__main__":
    main()
