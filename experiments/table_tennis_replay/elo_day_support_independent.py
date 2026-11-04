"""Slow pairwise cross-check for the fixed, previously inventoried CSV."""
import csv
import hashlib
import json
from pathlib import Path
import sys

sample = Path(sys.argv[1])
if hashlib.sha256(sample.read_bytes()).hexdigest() != "bbb2ab531f269a1eafff89e240086a4d17999114b582825ba4338986c6770fde":
    raise ValueError("this cross-check is only defined for the pinned sample")
with sample.open(encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
strict = {"any_player": 0, "both_players": 0, "h2h_pair": 0}
prefix = {"player_support_includes_same_day": 0, "any_player_only_from_same_day": 0,
          "both_players_only_from_same_day": 0, "h2h_support_includes_same_day": 0,
          "h2h_only_from_same_day": 0}
distribution = {"0": 0, "1": 0, "2-4": 0, "5+": 0}
for i, row in enumerate(rows):
    a, b, league, day = row["player_a"], row["player_b"], row["league"], row["date"]
    earlier = [r for r in rows if r["league"] == league and r["date"] < day]
    same_day_before = [r for r in rows[:i] if r["league"] == league and r["date"] == day]
    def has(player, records):
        return any(player in (record["player_a"], record["player_b"]) for record in records)
    def pair_count(records):
        return sum({a, b} == {record["player_a"], record["player_b"]} for record in records)
    pa, pb, ph = has(a, earlier), has(b, earlier), pair_count(earlier)
    sa, sb, sh = has(a, same_day_before), has(b, same_day_before), pair_count(same_day_before)
    strict["any_player"] += bool(pa or pb)
    strict["both_players"] += bool(pa and pb)
    strict["h2h_pair"] += bool(ph)
    distribution["0" if ph == 0 else "1" if ph == 1 else "2-4" if ph < 5 else "5+"] += 1
    prefix["player_support_includes_same_day"] += bool(sa or sb)
    prefix["any_player_only_from_same_day"] += not (pa or pb) and bool(sa or sb)
    prefix["both_players_only_from_same_day"] += not (pa and pb) and (pa or sa) and (pb or sb)
    prefix["h2h_support_includes_same_day"] += bool(sh)
    prefix["h2h_only_from_same_day"] += not ph and bool(sh)
print(json.dumps({"strict": strict, "distribution": distribution, "prefix": prefix},
                 sort_keys=True, indent=2))
