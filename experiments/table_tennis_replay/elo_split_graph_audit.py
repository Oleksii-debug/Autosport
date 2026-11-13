"""Audit date-label partitions and source-key dependence, not model skill.

Opaque IDs are exact publisher strings, not verified people. All-rows graph
components are retrospective and must not become features at an earlier time.
No rights, availability, disjoint physical events, training, or betting is proven.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path

from elo_sample_inventory import audit as inventory
from wtt_archive_inventory import _day, _present


def _cutoff(value):
    result = _day(value)
    if result is None:
        raise ValueError("cutoff must be a canonical ISO date label")
    return result


def _read_rows(raw, expected_sha256):
    """Reuse the pinned sample inventory, returning only structural columns."""
    summary = inventory(raw, expected_sha256=expected_sha256)
    if (summary["invalid_date_label"] or summary["missing_match_id"] or
            summary["duplicate_match_id_values"] or summary["missing_league"] or
            summary["both_player_ids_present_rows"] != summary["rows"] or
            summary["same_id_both_sides_rows"]):
        raise ValueError("ambiguous source date, match, player or league")
    rows = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")):
        match, league, a, b = (row[k] for k in ("match_id", "league", "player_a", "player_b"))
        if any(not _present(value) or value != value.strip() for value in (match, league, a, b)):
            raise ValueError("noncanonical source strings")
        rows.append((_day(row["date"]), league, a, b))
    return summary, rows


def _component_membership(rows, *, league_scoped):
    """Return a deterministic internal component label for each source row."""
    parents, ranks = {}, {}
    def find(node):
        parents.setdefault(node, node)
        ranks.setdefault(node, 0)
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node
    def union(a, b):
        a, b = find(a), find(b)
        if a == b:
            return
        if ranks[a] < ranks[b]:
            a, b = b, a
        parents[b] = a
        if ranks[a] == ranks[b]:
            ranks[a] += 1

    def key(league, identity):
        return (league, identity) if league_scoped else identity

    edges = []
    for day, league, a, b in rows:
        u, v = key(league, a), key(league, b)
        union(u, v)
        edges.append((u, v))
    # A root selected by union-find depends on row order. A component's
    # minimum exact key does not; labels remain private internal values.
    anchors = {}
    for node in parents:
        root = find(node)
        if root not in anchors or node < anchors[root]:
            anchors[root] = node
    return [anchors[find(u)] for u, _ in edges]


def _component_metrics(rows, cutoff, *, league_scoped):
    """Union-find over exact keys; the whole-sample graph is retrospective."""
    labels = _component_membership(rows, league_scoped=league_scoped)
    def key(league, identity):
        return (league, identity) if league_scoped else identity
    edges = [(day < cutoff, key(league, a), key(league, b))
             for day, league, a, b in rows]
    early, later = [x for x in edges if x[0]], [x for x in edges if not x[0]]
    early_nodes = {node for _, a, b in early for node in (a, b)}
    later_nodes = {node for _, a, b in later for node in (a, b)}
    earlier_pairs = {tuple(sorted((a, b))) for _, a, b in early}
    known = Counter()
    for _, a, b in later:
        known[int(a in early_nodes) + int(b in early_nodes)] += 1
    same_pair = sum(tuple(sorted((a, b))) in earlier_pairs for _, a, b in later)
    components = defaultdict(lambda: {"source_keys": 0, "early_rows": 0, "later_rows": 0})
    nodes_by_component = defaultdict(set)
    for label, (is_early, a, b) in zip(labels, edges):
        nodes_by_component[label].update((a, b))
        components[label]["early_rows" if is_early else "later_rows"] += 1
    for label, nodes in nodes_by_component.items():
        components[label]["source_keys"] = len(nodes)
    groups = list(components.values())
    crossing = [x for x in groups if x["early_rows"] and x["later_rows"]]
    largest = max(groups, key=lambda x: (x["early_rows"] + x["later_rows"], x["source_keys"]))
    return {
        "early_distinct_source_keys": len(early_nodes), "later_distinct_source_keys": len(later_nodes),
        "source_keys_in_both_partitions": len(early_nodes & later_nodes),
        "later_rows_with_early_keys": {"zero": known[0], "one": known[1], "two": known[2]},
        "later_rows_with_unordered_pair_in_early": same_pair,
        "components": len(groups),
        "largest_component_source_rows": largest["early_rows"] + largest["later_rows"],
        "largest_component_source_keys": largest["source_keys"],
        "components_crossing_boundary": len(crossing),
        "early_rows_in_crossing_components": sum(x["early_rows"] for x in crossing),
        "later_rows_in_crossing_components": sum(x["later_rows"] for x in crossing),
    }


def audit(raw, *, expected_sha256, cutoff_date):
    cutoff = _cutoff(cutoff_date)
    summary, rows = _read_rows(raw, expected_sha256)
    early = sum(day < cutoff for day, _, _, _ in rows)
    return {
        "schema": "research.tt_elo_date_split_graph.v1",
        "source_sha256": summary["source_sha256"], "source_rows": len(rows),
        "date_label_cutoff": cutoff.isoformat(),
        "early_rows_before_cutoff": early, "later_rows_on_or_after_cutoff": len(rows) - early,
        "global_exact_id": _component_metrics(rows, cutoff, league_scoped=False),
        "league_scoped_exact_id": _component_metrics(rows, cutoff, league_scoped=True),
        "source_ids_verified_as_people": False, "cross_provider_identity_verified": False,
        "physical_match_deduplicated": False, "event_dates_verified": False,
        "result_availability_verified": False, "publisher_model_provenance_verified": False,
        "source_rights_verified": False, "independent_holdout": False,
        "model_training_authorized": False, "bookmaker_execution": False,
        "profitability_claim": False,
        "note": "Date labels and exact source keys only; components use both partitions "
                "retrospectively and are not causal features or verified independent groups.",
    }


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
