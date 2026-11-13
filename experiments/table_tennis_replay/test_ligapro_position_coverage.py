"""Invented labels and naive times only; no real models or source rows."""
from datetime import datetime
import hashlib
import json

import pytest
from sklearn.preprocessing import OneHotEncoder

from ligapro_notebook_inventory import audit as prior_audit
from ligapro_position_coverage import _indexed, _reference, audit
from test_ligapro_notebook_inventory import raw_csv, row


def t(hour):
    return datetime(2026, 1, 1, hour)


def test_synthetic_onehot_encodes_opposite_only_categories_as_unknown():
    encoder = OneHotEncoder(handle_unknown="ignore")
    encoder.fit([["A", "B"]])  # Invented labels, not the source dataset.
    assert encoder.transform([["B", "A"]]).toarray().tolist() == [[0.0, 0.0]]
    assert encoder.transform([["A", "B"]]).toarray().tolist() == [[1.0, 1.0]]


def test_slot_categories_and_orientation_are_not_interchangeable():
    train = [("A", "B", t(3)), ("C", "D", t(5))]
    test = [("B", "A", t(1)), ("A", "D", t(1)), ("Z", "Y", t(4))]
    result = _indexed(train, test)
    assert result == _reference(train, test)
    for slot in ("player1", "player2"):
        assert result[slot] == {"same_position": 1, "other_position_only": 1, "unseen_anywhere": 1}
    assert result["test_rows_any_position_unknown"] == 2
    assert result["test_rows_both_positions_unknown"] == 2
    assert result["test_rows_reverse_only_pair_seen"] == 1
    assert result["test_rows_same_oriented_pair_seen"] == 0
    assert result["test_rows_later_same_position_player1"] == 1
    assert result["test_rows_later_same_position_player2"] == 1
    assert result["test_rows_later_both_same_position"] == 1
    assert result["test_rows_later_reverse_orientation_pair"] == 1
    assert result["test_rows_later_same_orientation_pair"] == 0


def test_later_same_and_reversed_pair_counts_can_overlap():
    train = [("A", "B", t(2)), ("B", "A", t(3))]
    test = [("A", "B", t(1))]
    result = _indexed(train, test)
    assert result == _reference(train, test)
    assert result["test_rows_later_same_orientation_pair"] == 1
    assert result["test_rows_later_reverse_orientation_pair"] == 1
    assert result["test_rows_later_either_orientation_pair"] == 1


def test_equal_or_missing_time_never_counts_as_later():
    train = [("A", "B", t(2)), ("A", "B", None)]
    test = [("A", "B", t(2)), ("A", "B", None)]
    result = _indexed(train, test)
    assert result == _reference(train, test)
    assert result["test_rows_later_any_same_position"] == 0
    assert result["test_rows_later_either_orientation_pair"] == 0
    assert result["player1"]["same_position"] == result["player2"]["same_position"] == 2


def pinned(rows):
    raw = raw_csv(rows)
    digest = hashlib.sha256(raw).hexdigest()
    previous = prior_audit(raw, expected_sha256=digest)
    return raw, digest, previous


def test_pinned_cleaning_and_split_reuse_omit_source_labels():
    rows = [row(n) for n in range(20)]
    raw, digest, previous = pinned(rows)
    report = audit(raw, expected_sha256=digest, expected_previous=previous)
    assert report["previous_aggregate_unchanged"]
    assert report["split"] == previous["split"]
    assert report["train_valid_pair_rows"] == 16 and report["test_valid_pair_rows"] == 4
    assert report["reference"]["status"] == "matched_direct_pairwise"
    assert "PRIVATE-" not in json.dumps(report)
    assert report["source_names_verified_as_people"] is False
    assert report["model_fit_or_predict"] is report["real_money_execution"] is False


def test_invalid_pairs_are_counted_without_inventing_category():
    rows = [row(n) for n in range(20)]
    rows[0]["Player2"] = rows[0]["Player1"]
    raw, digest, previous = pinned(rows)
    report = audit(raw, expected_sha256=digest, expected_previous=previous)
    assert report["train_invalid_pair_rows"] + report["test_invalid_pair_rows"] == 1
    assert report["train_valid_pair_rows"] + report["test_valid_pair_rows"] == 19
    assert report["reference"]["status"] == "matched_direct_pairwise"


def test_timezone_aware_is_excluded_only_from_later_comparison():
    rows = [{**row(n), "Date": row(n)["Date"] + "+00:00"} for n in range(20)]
    raw, digest, previous = pinned(rows)
    report = audit(raw, expected_sha256=digest, expected_previous=previous)
    assert report["train_timezone_aware_excluded_from_time_comparison"] == 16
    assert report["test_timezone_aware_excluded_from_time_comparison"] == 4
    assert report["position_coverage"]["test_rows_later_any_same_position"] == 0
    assert sum(report["position_coverage"]["player1"].values()) == 4


def test_expected_previous_and_hash_must_match():
    raw, digest, previous = pinned([row(n) for n in range(20)])
    with pytest.raises(ValueError, match="previous aggregate differs"):
        audit(raw, expected_sha256=digest, expected_previous={})
    with pytest.raises(ValueError, match="pinned"):
        audit(raw, expected_sha256="f" * 64, expected_previous=previous)
    with pytest.raises(ValueError, match="previous aggregate"):
        audit(raw, expected_sha256=digest, expected_previous=None)


def test_reference_cap_is_explicit_and_invalid_cap_rejected():
    raw, digest, previous = pinned([row(n) for n in range(20)])
    report = audit(raw, expected_sha256=digest, expected_previous=previous, reference_pair_cap=0)
    assert report["reference"]["status"] == "skipped_pair_cap"
    with pytest.raises(ValueError, match="pair cap"):
        audit(raw, expected_sha256=digest, expected_previous=previous, reference_pair_cap=True)
