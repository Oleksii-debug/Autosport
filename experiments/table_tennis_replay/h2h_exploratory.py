"""Descriptive, research-only first-live H2H comparison; no execution authority.

Only use results observed strictly before the decision. Names are unverified
source strings, not canonical identities. This is an exploratory comparison,
not a training pipeline or a claim that observed quotes were executable.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


def instant(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc


def result(match):
    raw = match.get("result") or {}
    periods = raw.get("periods") or []
    wins = [0, 0]
    try:
        for period in periods:
            if 3 in wins:
                return None
            a, b = (int(v) for v in period["score"].split("-"))
            # A set ends at 11 unless deuce requires exactly a two-point lead.
            hi, lo = max(a, b), min(a, b)
            if lo < 0 or not ((hi == 11 and lo <= 9) or (hi > 11 and hi - lo == 2)):
                return None
            wins[int(b > a)] += 1
        if max(wins) != 3 or min(wins) > 2 or len(periods) != sum(wins):
            return None
        return wins.index(3), instant(raw.get("observed_at"))
    except (ValueError, TypeError, KeyError):
        return None


def price(value):
    if not isinstance(value, dict) or value.get("suspended") or value.get("stale"):
        return None
    p = value.get("odds")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 1:
        return None
    return float(p)


def analyze(data):
    matches = data["matches"]
    ids = [str(m["id"]) for m in matches]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate fixture IDs")
    rows = []
    for match in matches:
        home, away, league = match.get("home"), match.get("away"), match.get("league")
        if not all(isinstance(x, str) and x.strip() for x in (home, away, league)) or home == away:
            continue
        final = result(match)
        try:
            decision = instant(match.get("first_live_at"))
        except ValueError:
            continue
        if final is None or final[1] <= decision:
            continue
        frames = [f for f in match.get("frames", []) if f.get("kind") == "live" and f.get("offset_seconds") == 0 and not f.get("source_stale")]
        if len(frames) != 1:
            continue
        try:
            if instant(frames[0].get("observed_at")) != decision:
                continue
        except ValueError:
            continue
        quote = frames[0].get("odds") or {}
        hp, ap = price(quote.get("home")), price(quote.get("away"))
        if hp is None or ap is None:
            continue
        prior = []
        for previous in matches:
            if previous is match or previous.get("league") != league:
                continue
            ph, pa = previous.get("home"), previous.get("away")
            if {ph, pa} != {home, away}:
                continue
            resolved = result(previous)
            try:
                previous_start = instant(previous.get("first_live_at"))
            except ValueError:
                continue
            if resolved and previous_start < resolved[1] < decision:
                prior.append((ph if resolved[0] == 0 else pa) == home)
        market = (1 / hp) / (1 / hp + 1 / ap)
        # Fixed illustrative shrinkage: five pseudo-observations anchored to market.
        candidate = (sum(prior) + 5 * market) / (len(prior) + 5)
        outcome = int(final[0] == 0)
        rows.append({"fixture_id": str(match["id"]), "home": home, "away": away,
                     "league": league, "decision_at": decision.isoformat(),
                     "prior_meetings": len(prior), "prior_home_wins": sum(prior),
                     "market_probability": market, "h2h_probability": candidate,
                     "home_won": outcome})
    paired = [r for r in rows if r["prior_meetings"]]
    def brier(rs, key):
        return sum((r[key] - r["home_won"]) ** 2 for r in rs) / len(rs) if rs else None
    return {"scope": "exploratory; exact source names are unverified identity proxies",
            "fixtures": len(matches), "scored": len(rows), "with_prior_meetings": len(paired),
            "market_brier_on_same_paired_cohort": brier(paired, "market_probability"),
            "h2h_brier_on_same_paired_cohort": brier(paired, "h2h_probability"),
            "real_money_authority": False, "independent_holdout": False, "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    report = analyze(json.loads(raw))
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
