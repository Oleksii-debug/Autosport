"""Research replay on separate Football-Data league seasons.

Source files are supplied by the caller; this module grants no source rights.
Only pre-closing PSH/PSD/PSA prices are read. Actual quote availability and
result publication times remain unverified. The shared team evaluator supplies
the original causal checks, strategy, cost and WAIT handling.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from benchmark_recorded_match_returns import summarize
from evaluate_recorded_team_agent import digest, evaluate_rows, validate_config


def assumed_london_kickoff(raw_date: str, raw_time: str) -> datetime:
    """Interpret UK local time for 2000–2099; reject ambiguous clock changes."""
    local = datetime.strptime(raw_date + " " + raw_time, "%d/%m/%Y %H:%M")
    if not 2000 <= local.year <= 2099:
        raise ValueError("unsupported UK timezone-rule year")
    def last_sunday(month):
        end = date(local.year, month, 31)
        return end - timedelta(days=(end.weekday() + 1) % 7)
    spring, autumn, day = last_sunday(3), last_sunday(10), local.date()
    if day in (spring, autumn) and local.hour == 1:
        raise ValueError("ambiguous or nonexistent UK clock-change time")
    summer = (spring < day < autumn or (day == spring and local.hour >= 2)
              or (day == autumn and local.hour < 1))
    return (local - timedelta(hours=int(summer))).replace(tzinfo=timezone.utc)


def load_seasons(files: dict[str, Path]):
    if type(files) is not dict or not files:
        raise ValueError("season files must be a non-empty mapping")
    rows, matches, exclusions, source_hashes = [], set(), Counter(), {}
    for season, path in sorted(files.items()):
        if type(season) is not str or len(season) != 4 or not season.isascii() or not season.isdigit():
            raise ValueError("season must be four ASCII digits")
        raw = Path(path).read_bytes()
        source_hashes[season] = hashlib.sha256(raw).hexdigest()
        for item in csv.DictReader(raw.decode("utf-8-sig").splitlines()):
            try:
                kickoff = assumed_london_kickoff(item["Date"], item["Time"])
                home, away = item["HomeTeam"].strip(), item["AwayTeam"].strip()
                if not home or not away or home == away or item["Div"] != "E0":
                    raise ValueError("invalid teams or league")
                odds = {key: Decimal(item[column]) for key, column in (("1", "PSH"), ("X", "PSD"), ("2", "PSA"))}
                if any(not p.is_finite() or p <= 1 for p in odds.values()):
                    raise ValueError("invalid prices")
                result = {"H": "1", "D": "X", "A": "2"}[item["FTR"]]
            except (KeyError, ValueError, InvalidOperation):
                exclusions[season] += 1
                continue
            match_key = (kickoff.isoformat(), home, away)
            if match_key in matches:
                raise ValueError("duplicate match across source files")
            matches.add(match_key)
            identity = season + ":" + ":".join(match_key)
            rows.append({"id": identity, "kickoff": kickoff, "odds": odds, "result": result,
                         "team_context": {"country": "England", "home": home, "away": away}})
    rows.sort(key=lambda row: (row["kickoff"], row["id"]))
    return rows, {"source_sha256": digest(source_hashes), "file_sha256": source_hashes,
                  "exclusions": dict(exclusions), "eligible_rows": len(rows),
                  "attribution": "Football-Data.co.uk, https://www.football-data.co.uk/downloadm.php"}


def evaluate(files: dict[str, Path], config: dict, evaluation_seasons: tuple[str, ...]):
    validate_config(config)
    if type(evaluation_seasons) is not tuple or not evaluation_seasons or len(set(evaluation_seasons)) != len(evaluation_seasons):
        raise ValueError("distinct evaluation seasons are required")
    rows, source = load_seasons(files)
    by_id = {row["id"]: row for row in rows}
    report = evaluate_rows(rows, source, config)
    seasons = {}
    for season in evaluation_seasons:
        decisions = [d for d in report["decisions"] if d["id"].startswith(season + ":")]
        if not decisions:
            raise ValueError("evaluation season has no eligible validation matches")
        variants = {}
        for variant in ("market", "frozen_team", "online_team"):
            selected, brier, logloss = [], Decimal(0), Decimal(0)
            for decision in decisions:
                row = by_id[decision["id"]]
                chosen = decision["variants"][variant]["selection"]
                for side, value in zip(("1", "X", "2"), decision["variants"][variant]["probabilities"], strict=True):
                    probability = Decimal(value)
                    brier += (probability - int(side == row["result"])) ** 2
                    if side == row["result"]:
                        logloss -= probability.ln()
                if chosen != "WAIT":
                    reward = row["odds"][chosen] - 1 if chosen == row["result"] else Decimal(-1)
                    selected.append({"kickoff": row["kickoff"], "rewards": {variant: reward}})
            variants[variant] = {**summarize(selected, variant, config["cost_bps_per_stake"]),
                                 "wait": len(decisions) - len(selected),
                                 "brier_mean": str(brier / (3 * len(decisions))),
                                 "logloss_mean": str(logloss / len(decisions))}
        seasons[season] = {"matches": len(decisions), "variants": variants}
    return {"schema": "autosport.independent_team_agent_research", "schema_version": 1,
            "config": config, "config_sha256": digest(config), "source": source,
            "training_matches": report["training_matches"], "evaluation": seasons,
            "decision_journal_sha256": digest(report["decisions"]),
            "forward_evidence_eligible": False, "real_money_execution": False,
            "promotion_authorized": False, "profitability_claim": False,
            "limits": ["No independently verified quote availability, fills or result publication times.",
                       "The 48-hour result lag and Europe/London kickoff timezone are assumptions.",
                       "Online evaluation consumes earlier results; later seasons are prequential, not frozen holdout.",
                       "Retrospective prices and small samples cannot establish prospective profitability."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", nargs=2, action="append", required=True, metavar=("ID", "CSV"))
    parser.add_argument("--evaluation-season", action="append", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files = {key: Path(value) for key, value in args.season}
    if len(files) != len(args.season):
        parser.error("duplicate season ID")
    with localcontext(Context(prec=28)):
        report = evaluate(files, strict_json_loads(args.config.read_text("utf-8")), tuple(args.evaluation_season))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"config_sha256": report["config_sha256"], "evaluation": report["evaluation"]}))
