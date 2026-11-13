"""Independent structural count for the exact published London CSV only.

This checker does not validate source claims, model chronology or identities.
It outputs aggregates, never individual player keys or row contents.
"""
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

EXPECTED = "9bcaa521b50cc573195545094c54601fa8f071b017ea8ed2e8498f076f80b983"


def count(raw):
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError("not the pinned upstream CSV")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    header, body = rows[0], rows[1:]
    ia, ib = header.index("player_a_id"), header.index("player_b_id")
    players, pairs, exact = Counter(), Counter(), Counter(tuple(row) for row in body)
    for row in body:
        players.update((row[ia], row[ib]))
        pairs[tuple(sorted((row[ia], row[ib])))] += 1
    graph = defaultdict(set)
    for a, b in pairs:
        graph[a].add(b)
        graph[b].add(a)
    seen, sizes = set(), []
    for start in graph:
        if start in seen:
            continue
        stack, group = [start], set()
        seen.add(start)
        while stack:
            current = stack.pop()
            group.add(current)
            for neighbour in graph[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        sizes.append(sum(n for (a, b), n in pairs.items() if a in group and b in group))
    return {"rows": len(body), "unique_players": len(players),
            "players_repeated": sum(n > 1 for n in players.values()),
            "pairs": len(pairs), "pairs_repeated": sum(n > 1 for n in pairs.values()),
            "repeated_pair_rows": sum(n for n in pairs.values() if n > 1),
            "exact_dup_groups": sum(n > 1 for n in exact.values()),
            "exact_dup_rows": sum(n for n in exact.values() if n > 1),
            "components": len(sizes), "largest_component_rows": max(sizes)}


if __name__ == "__main__":
    print(json.dumps(count(Path(sys.argv[1]).read_bytes()), sort_keys=True))
