"""Synthetic probability and dependence tests; no actual evaluation claim."""
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

import elo_probability_diagnostic as module
from elo_probability_diagnostic import audit
from elo_split_graph_audit import _component_membership, _read_rows
from test_elo_sample_inventory import raw_rows, row

CUTOFF = "2026-06-26"


def inspect(items, cutoff=CUTOFF):
    raw = raw_rows(items)
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), cutoff_date=cutoff)


def test_paired_scores_and_component_resampling_have_known_values():
    records = [row(p_model="0.8"),
               row("bbbbbbbbbbbb", date=CUTOFF, player_a="333333333333", player_b="444444444444",
                   p_model="0.8", winner="B", sets_a="1", sets_b="3")]
    report = inspect(records)
    all_rows = report["all_eligible"]
    assert all_rows["eligible_rows"] == 2
    assert all_rows["publisher"]["brier"] == pytest.approx((.2**2 + .8**2) / 2)
    assert all_rows["neutral"]["brier"] == .25
    assert all_rows["publisher_minus_neutral_brier"] == pytest.approx(.09)
    assert all_rows["publisher"]["log_loss"] == pytest.approx(-(math.log(.8) + math.log(.2)) / 2)
    assert all_rows["decimal_crosscheck"] == "matched_1e-12"
    block = all_rows["component_resampling"]
    assert block["components"] == 2 and block["largest_component_rows"] == 1
    assert block["replicates"] == 2000 and block["seed"] == 20261104
    assert block["lower_2_5_percentile"] == pytest.approx(-.21)
    assert block["upper_97_5_percentile"] == pytest.approx(.39)
    later = report["later_date_label_eligible"]
    assert later["eligible_rows"] == 1
    assert later["component_resampling"]["status"] == "insufficient_components"
    assert later["component_resampling"]["lower_2_5_percentile"] is None


def test_winner_orientation_flipping_sides_and_probability_preserves_scores():
    original = [row(p_model="0.8"), row("bbbbbbbbbbbb", date=CUTOFF,
                player_a="333333333333", player_b="444444444444", p_model="0.7")]
    reversed_sides = [dict(x, player_a=x["player_b"], player_b=x["player_a"],
                           p_model=str(Decimal(1) - Decimal(x["p_model"])),
                           winner="B", sets_a=x["sets_b"], sets_b=x["sets_a"]) for x in original]
    left, right = inspect(original), inspect(reversed_sides)
    for name in ("all_eligible", "later_date_label_eligible"):
        assert left[name]["publisher"]["brier"] == pytest.approx(right[name]["publisher"]["brier"])
        assert left[name]["publisher"]["log_loss"] == pytest.approx(right[name]["publisher"]["log_loss"])
        assert left[name]["component_resampling"]["components"] == right[name]["component_resampling"]["components"]


def test_excluded_row_can_connect_eligible_rows_in_retrospective_graph():
    records = [row(p_model="0.8"),
               row("bbbbbbbbbbbb", player_a="222222222222", player_b="333333333333", sets_a="1", sets_b="0"),
               row("cccccccccccc", date=CUTOFF, player_a="333333333333", player_b="444444444444")]
    report = inspect(records)
    assert report["exclusions_all"]["score_or_winner_shape_unqualified"] == 1
    assert report["all_eligible"]["eligible_rows"] == 2
    assert report["all_eligible"]["component_resampling"]["components"] == 1
    assert report["all_eligible"]["component_resampling"]["status"] == "insufficient_components"


def test_exclusions_include_zero_one_nonfinite_and_float_underflow():
    records = [row(p_model="0"), row("bbbbbbbbbbbb", p_model="1"),
               row("cccccccccccc", p_model="NaN"), row("dddddddddddd", p_model="1e-400"),
               row("eeeeeeeeeeee", sets_a="0", sets_b="1", p_model="0"),
               row("ffffffffffff", winner="B", sets_a="3", sets_b="1")]
    result = inspect(records)
    assert result["exclusions_all"] == {"score_or_winner_shape_unqualified": 2,
                                        "probability_not_strictly_inside_zero_one": 4}
    assert result["all_eligible"]["eligible_rows"] == 0
    assert result["all_eligible"]["publisher"] is None
    assert result["all_eligible"]["decimal_crosscheck"] == "no_eligible_rows"
    assert result["all_eligible"]["component_resampling"]["replicates"] == 0


def test_fixed_cutoff_and_invalid_date_are_not_silently_repaired():
    records = [row(), row("bbbbbbbbbbbb", date=CUTOFF), row("cccccccccccc", date="2026-06-27")]
    result = inspect(records)
    assert result["all_eligible"]["eligible_rows"] == 3
    assert result["later_date_label_eligible"]["eligible_rows"] == 2
    with pytest.raises(ValueError, match="cutoff"):
        inspect(records, cutoff="2026-6-26")
    with pytest.raises(ValueError, match="ambiguous"):
        inspect([row(date="2026-06-26T00:00:00Z")])


def test_pinned_graph_labels_are_invariant_to_order_and_reversal():
    records = [row(), row("bbbbbbbbbbbb", player_a="222222222222", player_b="333333333333"),
               row("cccccccccccc", player_a="444444444444", player_b="555555555555")]
    raw = raw_rows(records)
    _, data = _read_rows(raw, hashlib.sha256(raw).hexdigest())
    labels = _component_membership(data, league_scoped=False)
    assert labels[0] == labels[1] and labels[0] != labels[2]
    flipped = [(day, league, b, a) for day, league, a, b in reversed(data)]
    assert _component_membership(flipped, league_scoped=False) == list(reversed(labels))


def test_bootstrap_is_deterministic_and_does_not_read_private_values():
    records = [row(), row("bbbbbbbbbbbb", date=CUTOFF, player_a="333333333333", player_b="444444444444")]
    result = inspect(records)
    assert result == inspect(records)
    text = json.dumps(result)
    assert "aaaaaaaaaaaa" not in text and "111111111111" not in text
    for key in ("source_ids_verified_as_people", "physical_results_verified", "source_rights_verified",
                "prediction_time_verified", "model_fit_provenance_verified", "independent_holdout",
                "calibration_verified", "market_odds_available", "model_training_authorized",
                "bookmaker_execution", "profitability_claim"):
        assert result[key] is False


def test_incorrect_float_scorer_fails_independent_decimal_check(monkeypatch):
    monkeypatch.setattr(module, "_scores", lambda rows, key: {"brier": 99, "log_loss": 99} if rows else None)
    with pytest.raises(ValueError, match="Decimal scoring disagree"):
        inspect([row()])


def test_pinned_hash_and_duplicate_fixture_fail_closed():
    raw = raw_rows([row()])
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0" * 64, cutoff_date=CUTOFF)
    with pytest.raises(ValueError, match="ambiguous"):
        inspect([row(), row(date=CUTOFF)])


def test_cli_writes_aggregate_once(tmp_path):
    raw = raw_rows([row()])
    sample, output = tmp_path / "sample.csv", tmp_path / "report.json"
    sample.write_bytes(raw)
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments/table_tennis_replay/elo_probability_diagnostic.py"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(repo / "src"), str(repo / "experiments/table_tennis_replay"), os.environ.get("PYTHONPATH", "")])}
    args = [sys.executable, str(script), str(sample), str(output), "--expected-sha256",
            hashlib.sha256(raw).hexdigest(), "--cutoff-date", CUTOFF]
    subprocess.run(args, env=env, capture_output=True, check=True)
    assert json.loads(output.read_text())["all_eligible"]["eligible_rows"] == 1
    assert subprocess.run(args, env=env, capture_output=True).returncode != 0
