from copy import deepcopy
import csv
import hashlib
import io
import json
import random

import pytest

from setka_collision_audit import _signature, audit, collision_groups
from setka_collision_reference import _reference_signature, summarize
from setka_source_time_audit import FIELDS


def row(index, a="Alpha_Private", b="Beta_Private", *, games=((11, 3), (11, 5), (11, 8)),
        winner="1", totals=(3, 0), day="6/10/2022", time="1:05:00"):
    result = {name: "NA" for name in FIELDS}
    result.update({"X": str(index), "Date": day, "Time": time, "Player1": a, "Player2": b,
                   "HomeWinner": winner, "Sets_P1": str(totals[0]), "Sets_P2": str(totals[1])})
    for i, (left, right) in enumerate(games, 1):
        result[f"P1_G{i}"], result[f"P2_G{i}"] = str(left), str(right)
    return result


def sample(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def inspect(raw):
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_reversed_sides_and_scores_compare_equal_without_exposing_labels():
    a = row(1)
    b = row(2, "Beta_Private", "Alpha_Private", games=((3, 11), (5, 11), (8, 11)),
            winner="0", totals=(0, 3), time="01:05:00")
    raw = sample([a, b])
    report = inspect(raw)
    assert report["collision_groups"] == 1 and report["collision_rows"] == 2
    assert report["groups_with_reversed_source_side_order"] == 1
    assert report["posthoc_groups_equal_on_raw_fields_except_row_label"] == 0
    assert report["structurally_qualified_collision_rows"] == 2
    assert report["group_score_status"]["all_qualified_equal"] == 1
    assert "Alpha_Private" not in json.dumps(report) and "Beta_Private" not in json.dumps(report)
    assert report["identity_independently_verified"] is False
    _, groups = collision_groups(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    reference = summarize(groups)
    for key, value in reference.items():
        assert report[key] == value


def test_different_qualified_scores_are_not_silently_deduplicated():
    raw = sample([row(1), row(2, games=((11, 3), (11, 5), (11, 9)))])
    result = inspect(raw)
    assert result["group_score_status"]["all_qualified_different"] == 1
    assert result["physical_match_deduplication_verified"] is False


def test_exact_raw_row_equality_ignores_only_source_row_number():
    result = inspect(sample([row(1), row(2)]))
    assert result["posthoc_groups_equal_on_raw_fields_except_row_label"] == 1
    assert result["group_score_status"]["all_qualified_equal"] == 1
    different_raw_time = inspect(sample([row(1), row(2, time="01:05:00")]))
    assert different_raw_time["posthoc_groups_equal_on_raw_fields_except_row_label"] == 0
    assert different_raw_time["group_score_status"]["all_qualified_equal"] == 1


@pytest.mark.parametrize("change", ["bad_winner", "bad_total", "unfinished", "score_gap",
                                    "half_score_missing", "extra_set", "invalid_points"])
def test_unqualified_scores_cannot_be_classified_as_equal(change):
    good = row(1)
    bad = row(2)
    if change == "bad_winner": bad["HomeWinner"] = "0"
    elif change == "bad_total": bad["Sets_P2"] = "1"
    elif change == "unfinished": bad["P1_G3"] = bad["P2_G3"] = "NA"
    elif change == "score_gap": bad["P1_G2"] = bad["P2_G2"] = "NA"
    elif change == "half_score_missing": bad["P2_G2"] = "NA"
    elif change == "extra_set": bad["P1_G4"], bad["P2_G4"] = "11", "7"
    else: bad["P1_G1"], bad["P2_G1"] = "11", "10"
    report = inspect(sample([good, bad]))
    assert report["group_score_status"]["not_fully_qualified"] == 1
    assert report["structurally_qualified_collision_rows"] == 1
    assert _signature(bad) is _reference_signature(bad) is None


def test_only_same_raw_date_and_same_time_pair_enter_collision_groups():
    raw = sample([row(1), row(2, day="06/10/2022"), row(3, time="1:06:00"),
                  row(4, a="Other_Private", b="Beta_Private")])
    result = inspect(raw)
    assert result["collision_rows"] == result["collision_groups"] == 0


def test_duplicate_source_row_label_or_unpinned_bytes_are_rejected():
    raw = sample([row(1), row(1)])
    with pytest.raises(ValueError, match="row labels"):
        inspect(raw)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audit(raw, expected_sha256="f" * 64)


def test_independent_score_validator_agrees_across_generated_scores():
    rng = random.Random(123)
    values = ("0", "2", "9", "10", "11", "12", "13", "14", "NA", "", "xxx")
    for index in range(400):
        value = row(index)
        for field in ("HomeWinner", "Sets_P1", "Sets_P2", "P1_G1", "P2_G1", "P1_G2",
                      "P2_G2", "P1_G3", "P2_G3", "P1_G4", "P2_G4", "P1_G5", "P2_G5"):
            if rng.random() < 0.4:
                value[field] = rng.choice(values)
        primary = _signature(value)
        reference = _reference_signature(value)
        assert (primary is None) == (reference is None)
        if primary is not None:
            assert primary[0] == ("1" if reference[0] == 0 else "2")
            assert primary[1:] == reference[1:]


def test_equal_source_labels_on_both_sides_are_not_merged():
    value = row(1, a="Alpha_Private", b="Alpha_Private")
    assert _signature(value) is _reference_signature(value) is None
    assert inspect(sample([value, deepcopy(value) | {"X": "2"}]))["collision_groups"] == 0
