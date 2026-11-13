"""Independent breadth-first source-key graph check; shared input parser.

Both calculations inspect the same already-known sample and cannot establish
identities, actual event times, source rights, availability, or independence.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
from pathlib import Path

from elo_split_graph_audit import _cutoff, _read_rows


def _reference(rows, cutoff, *, league_scoped):
    adjacency = {}
    indexed = []
    for day, league, a, b in rows:
        first = (league, a) if league_scoped else a
        second = (league, b) if league_scoped else b
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
        indexed.append((day, first, second))
    early = [edge for edge in indexed if edge[0] < cutoff]
    later = [edge for edge in indexed if edge[0] >= cutoff]
    early_nodes = set().union(*(set((a, b)) for _, a, b in early)) if early else set()
    later_nodes = set().union(*(set((a, b)) for _, a, b in later)) if later else set()
    early_pairs = {frozenset((a, b)) for _, a, b in early}
    known = Counter(sum(key in early_nodes for key in (a, b)) for _, a, b in later)
    pairs = sum(frozenset((a, b)) in early_pairs for _, a, b in later)
    unseen = set(adjacency)
    components = []
    while unseen:
        start = next(iter(unseen))
        queue, visited = deque([start]), {start}
        unseen.remove(start)
        while queue:
            for other in adjacency[queue.popleft()]:
                if other in unseen:
                    unseen.remove(other)
                    visited.add(other)
                    queue.append(other)
        a = sum(first in visited for _, first, _ in early)
        b = sum(first in visited for _, first, _ in later)
        components.append((len(visited), a, b))
    largest = max(components, key=lambda part: (part[1] + part[2], part[0]))
    crossing = [part for part in components if part[1] and part[2]]
    return {"early_distinct_source_keys": len(early_nodes), "later_distinct_source_keys": len(later_nodes),
            "source_keys_in_both_partitions": len(early_nodes & later_nodes),
            "later_rows_with_early_keys": {"zero": known[0], "one": known[1], "two": known[2]},
            "later_rows_with_unordered_pair_in_early": pairs, "components": len(components),
            "largest_component_source_rows": largest[1] + largest[2],
            "largest_component_source_keys": largest[0],
            "components_crossing_boundary": len(crossing),
            "early_rows_in_crossing_components": sum(part[1] for part in crossing),
            "later_rows_in_crossing_components": sum(part[2] for part in crossing)}


def audit(raw, *, expected_sha256, cutoff_date):
    cutoff = _cutoff(cutoff_date)
    summary, rows = _read_rows(raw, expected_sha256)
    return {"schema": "research.tt_elo_split_graph_reference.v1",
            "source_sha256": summary["source_sha256"], "source_rows": len(rows),
            "date_label_cutoff": cutoff.isoformat(),
            "early_rows_before_cutoff": sum(day < cutoff for day, _, _, _ in rows),
            "later_rows_on_or_after_cutoff": sum(day >= cutoff for day, _, _, _ in rows),
            "global_exact_id": _reference(rows, cutoff, league_scoped=False),
            "league_scoped_exact_id": _reference(rows, cutoff, league_scoped=True),
            "shared_input_parser_and_source": True, "independent_source_verified": False,
            "identity_verified": False, "availability_verified": False,
            "independent_holdout": False, "model_training_authorized": False,
            "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--cutoff-date", required=True)
    args = parser.parse_args()
    result = audit(args.sample.read_bytes(), expected_sha256=args.expected_sha256,
                   cutoff_date=args.cutoff_date)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
