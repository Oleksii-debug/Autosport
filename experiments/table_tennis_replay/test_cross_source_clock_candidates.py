"""All fixtures below are invented and do not establish participant identities."""
from copy import deepcopy
import hashlib
import json

import pytest

from cross_source_clock_candidates import audit
from cross_source_clock_reference import audit as reference


def document():
    return {
        "left": [
            {"row_id": "l1", "date": "2026-01-01", "clock": "06:00", "a": "private-alpha", "b": "private-beta"},
            {"row_id": "l2", "date": "2026-01-01", "clock": "07:30", "a": "private-alpha", "b": "private-beta"},
            {"row_id": "l3", "date": "2026-01-02", "clock": "08:00", "a": "private-alpha", "b": "private-beta"},
            {"row_id": "l4", "date": "2026-01-02", "clock": "09:00", "a": "private-alpha", "b": "private-beta"},
        ],
        "right": [
            {"row_id": "r1", "date": "2026-01-01", "clock": "09:00", "elapsed": "00:10:00", "a": "x", "b": "y"},
            {"row_id": "r2", "date": "2026-01-01", "clock": "10:30", "elapsed": "01:40:08", "a": "y", "b": "x"},
            {"row_id": "r3", "date": "2026-01-02", "clock": "11:00", "elapsed": "00:20:00", "a": "x", "b": "y"},
            {"row_id": "r4", "date": "2026-01-02", "clock": "12:00", "elapsed": "01:19:48", "a": "y", "b": "x"},
        ],
        "right_to_left": {"x": "private-alpha", "y": "private-beta"},
    }


def run(data, fn=audit):
    raw = json.dumps(data, sort_keys=True).encode()
    return fn(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_full_pairing_and_independent_offset_first_enumeration():
    data = document()
    result, crosscheck = run(data), run(data, reference)
    assert result["pairing_space"] == 4 and result["feasible_full_pairings"] == 1
    assert result["feasible_offset_minutes"] == [179, 180, 181]
    assert result["feasible_pairing_offset_combinations"] == 3
    assert result["possible_row_pairs"] == result["forced_row_pairs_within_hypothesis"] == 4
    assert result["reversed_label_order_range"] == [2, 2]
    assert result["chapter_gap_minus_label_gap_seconds"] == [-12, 8]
    for key in ("feasible_full_pairings", "feasible_offset_minutes", "feasible_pairing_offset_combinations",
                "possible_row_pairs", "forced_row_pairs_within_hypothesis"):
        assert result[key] == crosscheck[key]
    assert result["physical_people_or_events_verified"] is result["crosswalk_verified"] is False
    assert "private-alpha" not in json.dumps(result) and "private-beta" not in json.dumps(result)


def test_permutation_of_input_rows_does_not_change_candidate_counts():
    original = document()
    changed = deepcopy(original)
    changed["left"].reverse()
    changed["right"] = [changed["right"][2], changed["right"][0], changed["right"][3], changed["right"][1]]
    first, second = run(original), run(changed)
    first.pop("transcription_sha256"); second.pop("transcription_sha256")
    assert first == second


def test_same_clock_rows_remain_ambiguous_and_not_merged():
    data = document()
    data["left"] = data["left"][:2]
    data["right"] = data["right"][:2]
    data["left"][1]["clock"] = data["left"][0]["clock"]
    data["right"][1]["clock"] = data["right"][0]["clock"]
    result = run(data)
    assert result["feasible_full_pairings"] == 2
    assert result["possible_row_pairs"] == 4 and result["forced_row_pairs_within_hypothesis"] == 0
    assert result["feasible_pairing_offset_combinations"] == 6
    assert run(data, reference)["feasible_full_pairings"] == 2


def test_tolerance_boundary_and_no_full_pairing():
    data = document()
    data["right"][1]["clock"] = "10:32"  # deltas 180 and 182 share only offset 181
    result = run(data)
    assert result["feasible_offset_minutes"] == [181]
    data["right"][1]["clock"] = "10:33"
    result = run(data)
    assert result["status"] == "no_full_pairing_under_hypotheses"
    assert result["feasible_full_pairings"] == 0 and result["forced_row_pairs_within_hypothesis"] == 0


def test_missing_group_prevents_partial_match_and_empty_is_explicit():
    data = document()
    data["right"].pop()
    result = run(data)
    assert result["status"] == "no_full_comparison_incomplete_groups"
    assert result["mismatched_group_counts"] == 1 and result["feasible_full_pairings"] is None
    data["left"] = []; data["right"] = []
    assert run(data)["status"] == "no_full_comparison_incomplete_groups"


def test_midnight_wrap_is_not_inferred():
    data = document()
    data["left"] = [data["left"][0]]
    data["right"] = [data["right"][0]]
    data["left"][0]["clock"] = "23:55"
    data["right"][0]["clock"] = "00:05"
    result = run(data)
    assert result["midnight_wrap_modelled"] is False
    assert result["status"] == "no_full_pairing_under_hypotheses"


def test_factorial_resource_cap_prevents_enumeration():
    data = document()
    data["left"] = [dict(data["left"][0], row_id="l" + str(i)) for i in range(8)]
    data["right"] = [dict(data["right"][0], row_id="r" + str(i), elapsed=f"00:00:{i:02d}") for i in range(8)]
    result = run(data)
    assert result["pairing_space"] == 40320
    assert result["status"] == "not_computed_pairing_cap" and result["feasible_full_pairings"] is None


@pytest.mark.parametrize("kind, message", [
    ("duplicate", "duplicate local row"), ("date", "canonical date"),
    ("clock", "HH:MM"), ("elapsed", "HH:MM:SS"),
    ("crosswalk", "injective"), ("unmapped", "unmapped"),
    ("same_person", "participant labels"), ("extra", "row fields"),
    ("chapter_duplicate", "duplicate chapter position"),
])
def test_invalid_inputs_fail_closed(kind, message):
    data = document()
    if kind == "duplicate": data["left"][1]["row_id"] = data["left"][0]["row_id"]
    elif kind == "date": data["left"][0]["date"] = "2026-2-1"
    elif kind == "clock": data["left"][0]["clock"] = "25:00"
    elif kind == "elapsed": data["right"][0]["elapsed"] = "00:60:01"
    elif kind == "crosswalk": data["right_to_left"]["y"] = "private-alpha"
    elif kind == "unmapped": data["right"][0]["a"] = "unknown"
    elif kind == "same_person": data["left"][0]["b"] = data["left"][0]["a"]
    elif kind == "extra": data["left"][0]["score"] = "invented"
    else: data["right"][1]["elapsed"] = data["right"][0]["elapsed"]
    with pytest.raises(ValueError, match=message):
        run(data)


def test_digest_duplicate_json_and_missing_bytes_are_rejected():
    raw = json.dumps(document()).encode()
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="f" * 64)
    raw = b'{"left":[],"left":[],"right":[],"right_to_left":{}}'
    with pytest.raises(ValueError, match="invalid input JSON"):
        audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="empty or oversized"):
        audit(b"", expected_sha256=hashlib.sha256(b"").hexdigest())
