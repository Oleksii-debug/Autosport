import csv
from datetime import date
import hashlib
import io
import random

import pytest

from setka_source_time_audit import FIELDS, _date_label, _time_label, audit, temporal_counts
from setka_source_time_reference import date_interpretation_reference, temporal_counts_reference


def sample(rows, *, fields=FIELDS):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for index, day, time, a, b in rows:
        row = {key: "0" for key in FIELDS}
        row.update({"X": index, "Date": day, "Time": time, "Player1": a, "Player2": b})
        writer.writerow(row)
    return out.getvalue().encode()


def inspect(raw):
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("label,classification,dmy,mdy", [
    ("6/10/2022", "slash_both_different", date(2022, 10, 6), date(2022, 6, 10)),
    ("13/9/2022", "slash_only_dmy", date(2022, 9, 13), None),
    ("9/13/2022", "slash_only_mdy", None, date(2022, 9, 13)),
    ("11/11/2022", "slash_both_same", date(2022, 11, 11), date(2022, 11, 11)),
    ("2022-09-01", "iso", date(2022, 9, 1), date(2022, 9, 1)),
    ("31/13/2022", "invalid", None, None),
])
def test_dates_do_not_choose_an_ambiguous_convention(label, classification, dmy, mdy):
    assert _date_label(label) == (classification, dmy, mdy)


@pytest.mark.parametrize("label,seconds", [("1:05:00", 3900), ("01:05:00", 3900),
                                           ("23:59:59", 86399), ("24:00:00", None),
                                           ("1:5:00", None), (" 1:05:00", None)])
def test_time_label_is_bounded_and_never_assigned_a_timezone(label, seconds):
    assert _time_label(label) == seconds


def test_ties_are_batched_and_prior_windows_have_inclusive_boundaries():
    records = [("6/10/2022", 0, "a", "b"), ("6/10/2022", 0, "a", "c"),
               ("6/10/2022", 1800, "a", "d"), ("6/10/2022", 3600, "b", "a"),
               ("6/10/2022", 3660, "e", "f"), ("06/10/2022", 3600, "a", "b")]
    expected = {"same_time_shared_name_rows": 2, "same_time_same_pair_rows": 0,
                "earlier_any_name_rows": 2, "earlier_both_names_rows": 1,
                "earlier_same_pair_rows": 1, "earlier_any_name_within_30m_rows": 2,
                "earlier_same_pair_within_30m_rows": 0, "earlier_any_name_within_60m_rows": 2,
                "earlier_same_pair_within_60m_rows": 1}
    assert temporal_counts(records) == expected
    assert temporal_counts_reference(records) == expected
    assert temporal_counts(list(reversed(records))) == expected


def test_same_time_reversed_pair_is_not_counted_as_an_earlier_match():
    records = [("same", 1, "a", "b"), ("same", 1, "b", "a")]
    result = temporal_counts(records)
    assert result["same_time_same_pair_rows"] == 2
    assert result["earlier_same_pair_rows"] == 0
    assert result == temporal_counts_reference(records)


def test_pairwise_reference_agrees_on_shuffled_ties_and_thresholds():
    rng = random.Random(17)
    names = list("abcdefghi")
    times = (0, 1, 1799, 1800, 1801, 3599, 3600, 3601, 7200)
    records = [(rng.choice(("6/10/2022", "06/10/2022")), rng.choice(times),
                *rng.sample(names, 2)) for _ in range(200)]
    rng.shuffle(records)
    assert temporal_counts(records) == temporal_counts_reference(records)


def test_independent_date_interpretations_preserve_ambiguity():
    raw = sample([("1", "6/10/2022", "1:05:00", "a", "b"),
                  ("2", "9/13/2022", "1:25:00", "b", "c")])
    result = date_interpretation_reference(raw)
    assert result["DMY"]["year_month_counts"] == {"2022-10": 1}
    assert result["DMY"]["invalid"] == 1
    assert result["MDY"]["year_month_counts"] == {"2022-06": 1, "2022-09": 1}


def test_bad_or_duplicate_indices_and_ambiguous_names_are_excluded():
    raw = sample([("1", "6/10/2022", "1:05:00", "a", "b"),
                  ("1", "6/10/2022", "1:15:00", "a", "c"),
                  ("3", "6/10/2022", "1:25:00", "a", "a"),
                  ("4", "6/10/2022", "1:35:00", " a", "b"),
                  ("5", "6/10/2022", "1:45:00", "a", "b")])
    result = inspect(raw)
    assert result["rows"] == 5
    assert result["duplicate_source_row_label_values"] == 1
    assert result["rows_with_duplicate_source_row_label"] == 2
    assert result["same_exact_label_both_sides_rows"] == 1
    assert result["missing_or_noncanonical_player_label_rows"] == 1
    assert result["eligible_temporal_rows"] == 1
    assert result["temporal_counts"]["earlier_any_name_rows"] == 0
    assert result["players_identified"] is result["result_availability_verified"] is False


def test_invalid_date_or_time_does_not_enter_temporal_counts():
    raw = sample([("1", "31/13/2022", "1:00:00", "a", "b"),
                  ("2", "6/10/2022", "24:00:00", "a", "b"),
                  ("3", "6/10/2022", "1:05:00", "a", "b")])
    result = inspect(raw)
    assert result["date_label_classes"]["invalid"] == 1
    assert result["invalid_time_labels"] == 1
    assert result["eligible_temporal_rows"] == 1
    assert result["excluded_temporal_rows"] == 2


def test_no_scores_or_results_enter_structural_counts():
    raw = sample([("1", "6/10/2022", "1:05:00", "a", "b"),
                  ("2", "6/10/2022", "1:25:00", "b", "c")])
    changed = raw.replace(b",0,0,0,0,0,0,0,0,0,0,0,0,0\r\n",
                          b",9,9,9,9,9,9,9,9,9,9,9,9,9\r\n")
    assert raw != changed
    left, right = inspect(raw), inspect(changed)
    left.pop("source_sha256"); right.pop("source_sha256")
    assert left == right


def test_hash_header_row_shape_and_empty_input_fail_closed():
    raw = sample([("1", "6/10/2022", "1:05:00", "a", "b")])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audit(raw, expected_sha256="0" * 64)
    bad_header = raw.replace(b"Player2,", b"Player1,", 1)
    with pytest.raises(ValueError, match="header"):
        inspect(bad_header)
    with pytest.raises(ValueError, match="row shape"):
        inspect(raw + b"extra,row\n")
    with pytest.raises(ValueError, match="empty"):
        inspect(b"")
