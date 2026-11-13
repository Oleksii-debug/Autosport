"""Synthetic graph and chronology-label checks; no model or real identity."""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from elo_split_graph_audit import audit
from elo_split_graph_reference import audit as reference
from test_elo_sample_inventory import raw_rows, row

CUTOFF = "2026-06-26"


def run(items, cutoff=CUTOFF):
    raw = raw_rows(items)
    sha = hashlib.sha256(raw).hexdigest()
    return audit(raw, expected_sha256=sha, cutoff_date=cutoff)


def test_future_bridge_keeps_all_connected_source_rows_in_one_component():
    rows = [row("aaaaaaaaaaaa", player_a="111111111111", player_b="222222222222"),
            row("bbbbbbbbbbbb", player_a="333333333333", player_b="444444444444"),
            row("cccccccccccc", date=CUTOFF, player_a="222222222222", player_b="333333333333"),
            row("dddddddddddd", date="2026-06-27", player_a="222222222222", player_b="111111111111")]
    result = run(rows)
    assert (result["early_rows_before_cutoff"], result["later_rows_on_or_after_cutoff"]) == (2, 2)
    group = result["global_exact_id"]
    assert group["source_keys_in_both_partitions"] == 3
    assert group["later_rows_with_early_keys"] == {"zero": 0, "one": 0, "two": 2}
    assert group["later_rows_with_unordered_pair_in_early"] == 1
    assert (group["components"], group["largest_component_source_keys"],
            group["largest_component_source_rows"]) == (1, 4, 4)
    assert (group["components_crossing_boundary"], group["early_rows_in_crossing_components"],
            group["later_rows_in_crossing_components"]) == (1, 2, 2)
    assert result["model_training_authorized"] is result["independent_holdout"] is False


def test_cross_league_exact_key_matches_depend_on_declared_namespace():
    rows = [row(league="First"),
            row("bbbbbbbbbbbb", date=CUTOFF, league="Second",
                player_a="111111111111", player_b="333333333333")]
    result = run(rows)
    global_scope, local_scope = result["global_exact_id"], result["league_scoped_exact_id"]
    assert global_scope["source_keys_in_both_partitions"] == 1
    assert global_scope["later_rows_with_early_keys"] == {"zero": 0, "one": 1, "two": 0}
    assert global_scope["components_crossing_boundary"] == 1
    assert local_scope["source_keys_in_both_partitions"] == 0
    assert local_scope["later_rows_with_early_keys"] == {"zero": 1, "one": 0, "two": 0}
    assert local_scope["components_crossing_boundary"] == 0
    assert local_scope["components"] == 2


def test_pair_overlap_does_not_follow_just_individually_known_players():
    rows = [row(), row("bbbbbbbbbbbb", player_a="333333333333", player_b="444444444444"),
            row("cccccccccccc", date=CUTOFF, player_a="111111111111", player_b="333333333333")]
    part = run(rows)["league_scoped_exact_id"]
    assert part["later_rows_with_early_keys"]["two"] == 1
    assert part["later_rows_with_unordered_pair_in_early"] == 0


def test_equal_cutoff_is_later_and_same_day_order_does_not_change_counts():
    first = row("aaaaaaaaaaaa", date=CUTOFF)
    second = row("bbbbbbbbbbbb", date=CUTOFF, player_a="222222222222", player_b="111111111111")
    x, y = run([first, second]), run([second, first])
    assert x["early_rows_before_cutoff"] == 0 and x["later_rows_on_or_after_cutoff"] == 2
    assert x["global_exact_id"] == y["global_exact_id"]
    assert x["league_scoped_exact_id"] == y["league_scoped_exact_id"]
    assert x["global_exact_id"]["later_rows_with_early_keys"]["zero"] == 2


def test_empty_later_partition_has_zero_overlap():
    result = run([row()], cutoff="2026-07-01")
    assert result["later_rows_on_or_after_cutoff"] == 0
    assert result["global_exact_id"]["source_keys_in_both_partitions"] == 0
    assert result["global_exact_id"]["components_crossing_boundary"] == 0


def test_outcome_and_model_changes_do_not_change_structural_counts():
    original = [row(), row("bbbbbbbbbbbb", date=CUTOFF)]
    modified = [dict(item, winner="B", sets_a="0", sets_b="3", p_model="0.99",
                     elo_a_pre="2000") for item in original]
    x, y = run(original), run(modified)
    for key in ("early_rows_before_cutoff", "later_rows_on_or_after_cutoff", "global_exact_id",
                "league_scoped_exact_id"):
        assert x[key] == y[key]


@pytest.mark.parametrize("bad", [
    {"date": "2026-06-25T00:00:00Z"}, {"match_id": ""}, {"player_a": ""},
    {"player_b": "111111111111"}, {"league": ""}, {"player_a": " 111111111111"},
])
def test_ambiguous_input_is_rejected(bad):
    with pytest.raises(ValueError):
        run([row(**bad)])


def test_wrong_hash_cutoff_and_duplicate_match_fail_closed():
    raw = raw_rows([row()])
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0" * 64, cutoff_date=CUTOFF)
    with pytest.raises(ValueError, match="cutoff"):
        run([row()], cutoff="2026-6-26")
    with pytest.raises(ValueError, match="ambiguous"):
        run([row(), row(date=CUTOFF)])


def test_output_aggregates_exclude_raw_ids_and_do_not_authorize_use():
    result = run([row("abcdef012345"), row("fedcba543210", date=CUTOFF)])
    body = json.dumps(result)
    assert "abcdef012345" not in body and "111111111111" not in body
    for key in ("source_ids_verified_as_people", "cross_provider_identity_verified",
                "physical_match_deduplicated", "event_dates_verified", "result_availability_verified",
                "publisher_model_provenance_verified", "source_rights_verified", "independent_holdout",
                "model_training_authorized", "bookmaker_execution", "profitability_claim"):
        assert result[key] is False


def test_reference_and_permutation_on_200_generated_graphs():
    rng = random.Random(20261104)
    for case in range(200):
        rows = []
        for index in range(rng.randint(1, 16)):
            a, b = rng.sample(range(1, 9), 2)
            rows.append(row(f"{index:012x}", date=rng.choice(["2026-06-25", CUTOFF, "2026-06-27"]),
                            league=rng.choice(["First", "Second"]),
                            player_a=f"{a:012x}", player_b=f"{b:012x}"))
        raw = raw_rows(rows)
        sha = hashlib.sha256(raw).hexdigest()
        actual = audit(raw, expected_sha256=sha, cutoff_date=CUTOFF)
        other = reference(raw, expected_sha256=sha, cutoff_date=CUTOFF)
        for key in ("early_rows_before_cutoff", "later_rows_on_or_after_cutoff",
                    "global_exact_id", "league_scoped_exact_id"):
            assert actual[key] == other[key], (case, key)
        reversed_sides = [dict(item, player_a=item["player_b"], player_b=item["player_a"]) for item in rows]
        rng.shuffle(reversed_sides)
        permuted = run(reversed_sides)
        assert actual["global_exact_id"] == permuted["global_exact_id"]
        assert actual["league_scoped_exact_id"] == permuted["league_scoped_exact_id"]


def test_cli_creates_new_aggregate_and_never_overwrites(tmp_path):
    raw = raw_rows([row(), row("bbbbbbbbbbbb", date=CUTOFF)])
    sample = tmp_path / "sample.csv"
    out = tmp_path / "report.json"
    sample.write_bytes(raw)
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments/table_tennis_replay/elo_split_graph_audit.py"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(repo / "src"), str(repo / "experiments/table_tennis_replay"), os.environ.get("PYTHONPATH", "")])}
    args = [sys.executable, str(script), str(sample), str(out), "--expected-sha256",
            hashlib.sha256(raw).hexdigest(), "--cutoff-date", CUTOFF]
    subprocess.run(args, env=env, capture_output=True, check=True)
    assert json.loads(out.read_text())["early_rows_before_cutoff"] == 1
    assert subprocess.run(args, env=env, capture_output=True).returncode != 0
