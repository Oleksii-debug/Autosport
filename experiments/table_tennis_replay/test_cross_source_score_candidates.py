import hashlib
import json

import pytest

from cross_source_score_candidates import audit
from cross_source_score_reference import count_pairs


def row(ref, a, b, score="11-3,11-4,11-6", winner="1", date="2026-01-01", clock="10:00"):
    return {"row_id": ref, "date": date, "clock": clock, "a": a, "b": b,
            "score": score, "winner": winner}


def document():
    return {"left": [row("left-one", "K1", "K2"),
                     row("left-two", "K1", "K2", "4-11,6-11,5-11", "2", clock="11:00")],
            "right": [row("right-one", "other B", "other A", "3-11,4-11,6-11", "2", clock="09:59"),
                      row("right-two", "other A", "other B", "4-11,6-11,5-11", "2", clock="11:00")],
            "right_to_left": {"other A": "K1", "other B": "K2"}}


def invoke(doc):
    raw = json.dumps(doc, ensure_ascii=False).encode()
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_two_hypotheses_reversed_sides_and_clock_labels():
    report = invoke(document())
    assert report["source_rows"] == {"left": 2, "right": 2}
    assert report["unmapped_rows"] == {"left": 0, "right": 0}
    counts = report["counts"]
    assert counts["unique_signature_candidates"] == 2
    assert counts["opposite_side_order_candidates"] == 1
    assert counts["clock_label_equal_candidates"] == 1
    assert counts["clock_label_different_candidates"] == 1
    assert counts["clock_label_missing_candidates"] == 0
    assert all(report[key] is False for key in (
        "crosswalk_verified", "physical_identity_verified", "physical_match_verified",
        "source_independence_verified", "timezone_verified", "result_availability_verified",
        "rights_verified", "training_authorized", "bookmaker_execution", "profitability_claim"))
    text = json.dumps(report)
    for sensitive in ("left-one", "right-one", "other A", "other B", "11-3,11-4,11-6"):
        assert sensitive not in text


def test_order_and_reversed_source_orientation_do_not_change_candidates():
    source = document()
    baseline = invoke(source)
    source["left"].reverse()
    source["right"].reverse()
    actual = invoke(source)
    assert baseline["counts"] == actual["counts"]
    assert baseline["transcription_sha256"] != actual["transcription_sha256"]


def test_independent_pairwise_check_and_duplicate_counting():
    source = document()
    reference = count_pairs(source)
    result = invoke(source)["counts"]
    assert reference == {"raw_pairwise_score_matches": 2, "opposite_order": 1,
                         "same_clock_label": 1, "different_clock_label": 1}
    assert reference["raw_pairwise_score_matches"] == result["unique_signature_candidates"]
    duplicate = source["left"][0].copy()
    duplicate["row_id"] = "another-row"
    source["left"].append(duplicate)
    assert count_pairs(source)["raw_pairwise_score_matches"] == 3
    assert invoke(source)["counts"]["ambiguous_signature_groups"] == 1


@pytest.mark.parametrize("side", ["left", "right", "both"])
def test_duplicate_score_signature_is_ambiguous_not_deduplicated(side):
    source = document()
    for which in ("left", "right") if side == "both" else (side,):
        duplicate = source[which][0].copy()
        duplicate["row_id"] += "-distinct-local-row"
        source[which].append(duplicate)
    result = invoke(source)["counts"]
    assert result["unique_signature_candidates"] == 1
    assert result["ambiguous_signature_groups"] == 1
    assert result["left_rows_in_ambiguous_groups"] == (2 if side in ("left", "both") else 1)
    assert result["right_rows_in_ambiguous_groups"] == (2 if side in ("right", "both") else 1)


@pytest.mark.parametrize("mutation", ["date", "score"])
def test_other_day_or_different_score_does_not_match(mutation):
    source = document()
    if mutation == "date":
        source["right"][0]["date"] = "2026-01-02"
    else:
        source["right"][0]["score"] = "3-11,4-11,7-11"
    result = invoke(source)["counts"]
    assert result["unique_signature_candidates"] == 1
    assert result["unmatched_left_rows"] == result["unmatched_right_rows"] == 1


def test_unknown_crosswalk_never_implies_identity():
    source = document()
    source["right_to_left"].pop("other B")
    result = invoke(source)
    assert result["unmapped_rows"] == {"left": 2, "right": 2}
    assert result["counts"]["unique_signature_candidates"] == 0


def test_missing_clock_is_not_equal_or_different():
    source = document()
    source["right"][0]["clock"] = None
    result = invoke(source)["counts"]
    assert result["clock_label_missing_candidates"] == 1
    assert result["clock_label_equal_candidates"] == 1
    assert result["clock_label_different_candidates"] == 0


@pytest.mark.parametrize("problem", ["duplicate_row", "noninjective", "bad_score", "bad_clock", "bad_date"])
def test_invalid_inputs_fail_closed(problem):
    source = document()
    if problem == "duplicate_row":
        source["right"][1]["row_id"] = source["right"][0]["row_id"]
    elif problem == "noninjective":
        source["right_to_left"]["other B"] = "K1"
    elif problem == "bad_score":
        source["left"][0]["score"] = "11-3,11-4,11-6,11-7"
    elif problem == "bad_clock":
        source["left"][0]["clock"] = "24:00"
    else:
        source["left"][0]["date"] = "2026-01-01T10:00Z"
    with pytest.raises(ValueError):
        invoke(source)


def test_hash_and_duplicate_json_keys_are_rejected():
    raw = json.dumps(document()).encode()
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw + b" ", expected_sha256=hashlib.sha256(raw).hexdigest())
    duplicate = b'{"left":[],"left":[],"right":[],"right_to_left":{}}'
    with pytest.raises(ValueError, match="JSON"):
        audit(duplicate, expected_sha256=hashlib.sha256(duplicate).hexdigest())
