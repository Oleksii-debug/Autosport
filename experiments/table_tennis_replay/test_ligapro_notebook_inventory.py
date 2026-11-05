"""Completely invented source labels, timestamps and scores."""
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import io
import json

import pytest

from ligapro_notebook_inventory import FIELDS, _count, _pair, _reference, _signature, audit


def row(n=0):
    return {"Date": f"2026-01-{1 + n // 10:02d} {n % 10:02d}:00:00",
            "Player1": "PRIVATE-A", "Player2": f"PRIVATE-{n % 4}",
            "Sets_P1": "3", "Sets_P2": "0", "P1_G1": "11", "P2_G1": "5",
            "P1_G2": "11", "P2_G2": "7", "P1_G3": "11", "P2_G3": "8",
            "P1_G4": "", "P2_G4": "", "P1_G5": "", "P2_G5": "",
            "HomeWinner": str(n % 2)}


def raw_csv(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def run(rows, **kwargs):
    raw = raw_csv(rows)
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), **kwargs)


def reverse(item):
    result = deepcopy(item)
    result["Player1"], result["Player2"] = item["Player2"], item["Player1"]
    result["Sets_P1"], result["Sets_P2"] = item["Sets_P2"], item["Sets_P1"]
    for n in range(1, 6):
        result[f"P1_G{n}"], result[f"P2_G{n}"] = item[f"P2_G{n}"], item[f"P1_G{n}"]
    result["HomeWinner"] = str(1 - int(item["HomeWinner"]))
    return result


def test_side_reversal_changes_raw_record_but_not_oriented_signature():
    first = row()
    second = reverse(first)
    assert first != second and _signature(first) == _signature(second)
    second["HomeWinner"] = first["HomeWinner"]
    assert _signature(first) != _signature(second)


def test_missing_is_not_zero_and_ambiguous_or_unknown_names_are_excluded():
    first = row()
    changed = deepcopy(first)
    changed["P1_G4"] = "0"
    assert _signature(first) != _signature(changed)
    for bad in ("", " PRIVATE-A", "PRIVATE-A ", "NA", first["Player1"]):
        changed = deepcopy(first)
        changed["Player2"] = bad
        assert _pair(changed) is _signature(changed) is None
    changed = deepcopy(first)
    changed["HomeWinner"] = "2"
    assert _signature(changed) is None


def _item(item, when):
    return {"raw": tuple(item[k] for k in FIELDS), "raw_time": item["Date"],
            "pair": _pair(item), "signature": _signature(item), "time": when}


def test_direct_reference_agrees_on_overlap_later_labels_and_reversal():
    earlier, later = row(), row(1)
    later["Date"] = "2026-01-01 03:00:00"
    later["Player2"] = earlier["Player2"]
    train = [_item(earlier, datetime(2026, 1, 1)), _item(later, datetime(2026, 1, 1, 3))]
    test = [_item(reverse(earlier), datetime(2026, 1, 1))]
    result = _count(train, test)
    assert result == _reference(train, test)
    assert result["test_rows_with_oriented_signature_in_train"] == 1
    assert result["test_rows_with_identical_raw_row_in_train"] == 0
    assert result["test_rows_with_any_later_train_name"] == 1
    assert result["test_rows_with_later_same_train_pair"] == 1


def test_same_time_is_not_a_later_label():
    first = row()
    value = _item(first, datetime(2026, 1, 1))
    result = _count([value], [value])
    assert result == _reference([value], [value])
    assert result["test_rows_with_any_later_train_name"] == 0
    assert result["test_rows_with_later_same_train_pair"] == 0


def test_cleaning_split_and_exact_hash_are_deterministic_without_publishing_names():
    source = [row(n) for n in range(20)]
    source.append({**row(20), "HomeWinner": ""})
    first = run(source)
    assert first == run(source)
    assert first["source_rows"] == 21 and first["notebook_clean_rows"] == 20
    assert first["excluded_by_notebook_cleaning"] == 1
    assert first["split"]["train_rows"] == 16 and first["split"]["test_rows"] == 4
    assert first["reference"]["status"] == "matched_direct_pairwise"
    assert first["date_labels"]["naive_rows"] == 20
    assert first["model_fit_or_predict"] is first["source_names_verified_as_people"] is False
    assert "PRIVATE-" not in json.dumps(first)


def test_reference_cap_skips_instead_of_claiming_cross_check():
    result = run([row(n) for n in range(20)], reference_pair_cap=0)
    assert result["reference"]["status"] == "skipped_pair_cap"
    with pytest.raises(ValueError, match="pair cap"):
        run([row(n) for n in range(20)], reference_pair_cap=True)


def test_timezone_aware_values_are_not_used_in_naive_time_comparison():
    rows = [{**row(n), "Date": row(n)["Date"] + "+00:00"} for n in range(20)]
    result = run(rows)
    assert result["date_labels"]["naive_rows"] == 0
    assert result["date_labels"]["timezone_aware_excluded_from_time_comparison"] == 20
    assert result["split_overlap"]["test_rows_with_any_later_train_name"] == 0


def test_invalid_shape_hash_and_nonbinary_cleaned_labels_fail_closed():
    source = [row(n) for n in range(20)]
    raw = raw_csv(source)
    with pytest.raises(ValueError, match="pinned"):
        audit(raw, expected_sha256="f" * 64)
    changed = raw.replace(b"Player1", b"Player2", 1)
    with pytest.raises(ValueError, match="header"):
        audit(changed, expected_sha256=hashlib.sha256(changed).hexdigest())
    source[0]["HomeWinner"] = "2"
    with pytest.raises(ValueError, match="binary"):
        run(source)
