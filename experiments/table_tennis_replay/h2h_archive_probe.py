"""Exploratory retrospective comparison using an assumed archive capture time.

This deliberately does not assert prior availability or verified identities.
No trading, model training, promotion, or financial authority.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

from h2h_exploratory import analyze, instant, result


def compare(recording, archive, assumed_capture):
    capture = instant(assumed_capture)
    if archive.get("sport") != "table_tennis" or not isinstance(archive.get("results"), list):
        raise ValueError("unexpected archive format")
    ids = set()
    histories = []
    recording_ids = {str(m["id"]) for m in recording["matches"]}
    for item in archive["results"]:
        ident = str(item.get("fixture_id") or "")
        if not ident or ident in ids or ident in recording_ids:
            raise ValueError("duplicate or overlapping fixture ID")
        ids.add(ident)
        home, away, league = item.get("home"), item.get("away"), item.get("league")
        if item.get("status") != "finished" or not all(isinstance(x, str) and x.strip() for x in (home, away, league)) or home == away:
            continue
        try:
            finish = instant(item["finished_at"])
            updated = instant(item["updated_at"])
        except (KeyError, ValueError):
            continue
        parsed = result({"result": {"observed_at": assumed_capture, "periods": item.get("periods")}})
        if parsed is None or not finish <= updated <= capture:
            continue
        histories.append({"home": home, "away": away, "league": league,
                          "winner": home if parsed[0] == 0 else away})
    eligible = analyze(recording)["rows"]
    pairs = []
    for row in eligible:
        decision = instant(row["decision_at"])
        if capture >= decision:
            continue
        relevant = [x for x in histories if x["league"] == row["league"] and
                    {x["home"], x["away"]} == {row["home"], row["away"]}]
        if not relevant:
            continue
        n = len(relevant)
        wins = sum(x["winner"] == row["home"] for x in relevant)
        m = row["market_probability"]
        p = (wins + 5 * m) / (n + 5)
        pairs.append({**row, "archive_prior_meetings": n, "archive_prior_home_wins": wins,
                      "archive_h2h_probability": p})
    def metrics(key):
        if not pairs:
            return None
        brier = sum((r[key] - r["home_won"]) ** 2 for r in pairs) / len(pairs)
        logloss = -sum(math.log(r[key] if r["home_won"] else 1-r[key]) for r in pairs) / len(pairs)
        return {"brier": brier, "log_loss": logloss}
    return {"archive_records": len(archive["results"]), "valid_archive_records": len(histories),
            "assumed_archive_capture": assumed_capture, "available_at_verified": False,
            "eligible_live_records": len(eligible), "matched_records": len(pairs),
            "market": metrics("market_probability"), "h2h": metrics("archive_h2h_probability"),
            "holdout": False, "execution_authority": False,
            "identity": "exact name and tournament strings are unverified proxies", "rows": pairs}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("recording", type=Path)
    p.add_argument("archive", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--assumed-capture", required=True)
    args = p.parse_args()
    raw_recording, raw_archive = args.recording.read_bytes(), args.archive.read_bytes()
    report = compare(json.loads(raw_recording), json.loads(raw_archive), args.assumed_capture)
    report["recording_sha256"] = hashlib.sha256(raw_recording).hexdigest()
    report["archive_sha256"] = hashlib.sha256(raw_archive).hexdigest()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
