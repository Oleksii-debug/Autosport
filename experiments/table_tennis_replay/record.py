"""Record a fixed group of real table-tennis matches for later 3-minute replay."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://moviodds.com"


def instant(text):
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def fetch(path, params):
    url = BASE + path + "?" + urlencode(params)
    with urlopen(Request(url, headers={"User-Agent": "Autosport-table-tennis-study/1.0"}), timeout=25) as response:
        body = response.read(30 * 1024 * 1024 + 1)
    if len(body) > 30 * 1024 * 1024:
        raise ValueError("source response too large")
    return json.loads(body), hashlib.sha256(body).hexdigest()


def market(match):
    players = (match.get("home"), match.get("away"))
    found = {}
    for q in match.get("quotes", []):
        group = str(q.get("group_id", ""))
        if not ((group == "920000" and q.get("line") == "To Win") or
                (group == "240" and not q.get("line") and not q.get("handicap"))):
            continue
        name = q.get("side") or q.get("label")
        odds = q.get("odds_dec")
        if name not in players or not isinstance(odds, (float, int)) or isinstance(odds, bool):
            continue
        if name in found:
            found[name] = {"ambiguous": True, "odds": None}
            continue
        active = odds > 1 and not q.get("suspended")
        status = match.get("group_status", {}).get(group, {})
        found[name] = {"odds": odds if active else None, "suspended": not active,
                       "source_price_changed_at": q.get("ts"), "market_checked_at": status.get("polled_at"),
                       "stale": bool(status.get("stale") or match.get("stale"))}
    return {"home": found.get(players[0]), "away": found.get(players[1])}


def initial_state(source):
    data = json.loads(source.read_text("utf-8"))
    matches = []
    for item in data["matches"]:
        matches.append({"id": str(item["id"]), "home": item["home"], "away": item["away"],
                        "league": item["league"], "scheduled_at": item["kickoff_utc"],
                        "prematch": {"captured_at": data["captured_at"], "odds": market(item)},
                        "first_live_at": None, "live_id": None, "status": "Очікується",
                        "frames": [], "result": None})
    if len(matches) != 20 or len({x["id"] for x in matches}) != 20:
        raise ValueError("the fixed selection must have exactly 20 distinct matches")
    return {"schema": "autosport.table_tennis_recording.v1", "source": BASE,
            "source_documentation": BASE + "/doc", "selection_captured_at": data["captured_at"],
            "poll_seconds": 20, "frame_seconds": 180, "matches": matches,
            "errors": [], "recording_complete": False}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
        temp = f.name
    os.replace(temp, path)


def same_names(match, item):
    return (match["home"], match["away"], match["league"]) == (item.get("home"), item.get("away"), item.get("league"))


def related_ids(live_id):
    data, _ = fetch("/closing", {"fi": live_id})
    return {str(x) for x in data.get("fixture_ids", [])}


def frame(item, received, offset, source_digest):
    return {"offset_seconds": offset, "observed_at": received.isoformat(), "score": item.get("score"),
            "odds": market(item), "source_id": str(item["id"]), "source_snapshot_sha256": source_digest,
            "source_polled_at": item.get("polled_at"), "source_last_live_seen": item.get("last_live_seen"),
            "source_stale": bool(item.get("stale")), "kind": "live"}


def cycle(state, board, outcomes, *, now, board_sha):
    candidates = [x for x in board.get("matches", []) if x.get("in_play")]
    unmatched = []
    for target in state["matches"]:
        if target["result"]:
            continue
        for item in candidates:
            live_id = str(item["id"])
            if same_names(target, item) and live_id not in {target["id"], target.get("live_id")}:
                unmatched.append(live_id)
    links = {}
    def resolve(live_id):
        try:
            return live_id, related_ids(live_id)
        except (OSError, ValueError, KeyError):
            return live_id, set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for live_id, aliases in pool.map(resolve, sorted(set(unmatched))):
            links[live_id] = aliases
    for target in state["matches"]:
        if target["result"]:
            continue
        result_matches = [row for row in outcomes.get("results", []) if same_names(target, row) and
                          str(row.get("fixture_id")) in {target["id"], target.get("live_id")}]
        finished = len(result_matches) == 1 and result_matches[0].get("status") == "finished"
        linked = [item for item in candidates if same_names(target, item) and
                  (str(item["id"]) in {target["id"], target.get("live_id")} or target["id"] in links.get(str(item["id"]), set()))]
        if len(linked) == 1 and not finished:
            item = linked[0]
            last_seen = item.get("last_live_seen")
            try:
                fresh_live = last_seen and 0 <= (now - instant(last_seen)).total_seconds() <= 120
            except ValueError:
                fresh_live = False
            if fresh_live:
                target["live_id"] = str(item["id"])
                if target["first_live_at"] is None:
                    target["first_live_at"] = now.isoformat()
                    target["status"] = "Наживо"
                    target["frames"].append(frame(item, now, 0, board_sha))
                else:
                    start = instant(target["first_live_at"])
                    next_offset = 180 * (1 + max((f["offset_seconds"] // 180 for f in target["frames"] if f["kind"] != "final"), default=-1))
                    elapsed = (now - start).total_seconds()
                    while elapsed >= next_offset:
                        if elapsed - next_offset > 45:
                            target["frames"].append({"offset_seconds": next_offset, "observed_at": None,
                                                     "kind": "missing", "reason": "Немає запису в цей момент"})
                        else:
                            target["frames"].append(frame(item, now, next_offset, board_sha))
                        next_offset += 180
        if finished:
            row = result_matches[0]
            target["status"] = "Завершено" if target["first_live_at"] else "Завершено; початок не записано"
            target["result"] = {"observed_at": now.isoformat(), "source_finished_at": row.get("finished_at"),
                                "final_score": row.get("final_score"), "periods": row.get("periods"),
                                "source_fixture_id": str(row.get("fixture_id"))}
            if target["first_live_at"]:
                offset = max(0, int((now - instant(target["first_live_at"])).total_seconds()))
                next_offset = 180 * (1 + max((f["offset_seconds"] // 180 for f in target["frames"] if f["kind"] != "final"), default=-1))
                while next_offset < offset:
                    target["frames"].append({"offset_seconds": next_offset, "observed_at": None,
                                             "kind": "missing", "reason": "Немає запису в цей момент"})
                    next_offset += 180
                target["frames"].append({"offset_seconds": offset, "observed_at": now.isoformat(),
                                         "score": row.get("final_score"), "periods": row.get("periods"),
                                         "odds": None, "kind": "final"})
    state["last_checked_at"] = now.isoformat()
    state["last_source_generated_at"] = board.get("generated_at")
    state["recording_complete"] = all(item["result"] for item in state["matches"])
    state["coverage_complete"] = state["recording_complete"] and all(
        item["first_live_at"] and not any(frame["kind"] == "missing" for frame in item["frames"])
        for item in state["matches"])
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=16200)
    parser.add_argument("--one-cycle", action="store_true")
    args = parser.parse_args()
    if args.poll_seconds < 20 or not 1 <= args.max_seconds <= 16200:
        parser.error("invalid bounded recording parameters")
    state = json.loads(args.output.read_text("utf-8")) if args.output.exists() else initial_state(args.selection)
    save(args.output, state)
    start = time.monotonic()
    while True:
        try:
            board, board_sha = fetch("/odds", {"sport": "table_tennis"})
            outcomes, _ = fetch("/results", {"sport": "table_tennis"})
            now = datetime.now(timezone.utc)
            cycle(state, board, outcomes, now=now, board_sha=board_sha)
            print(json.dumps({"at": now.isoformat(), "live": sum(x["status"] == "Наживо" for x in state["matches"]),
                              "finished": sum(x["result"] is not None for x in state["matches"]),
                              "frames": sum(len(x["frames"]) for x in state["matches"])}), flush=True)
        except (OSError, ValueError, KeyError) as exc:
            state["errors"].append({"observed_at": datetime.now(timezone.utc).isoformat(), "type": type(exc).__name__})
            print(json.dumps({"source_error": type(exc).__name__}), flush=True)
        save(args.output, state)
        if args.one_cycle or state["recording_complete"] or time.monotonic() - start >= args.max_seconds:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
