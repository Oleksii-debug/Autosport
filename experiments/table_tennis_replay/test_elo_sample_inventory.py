import csv
import hashlib
import io
import json

import pytest

from elo_sample_inventory import FIELDS, MAX_BYTES, audit


def row(match_id="aaaaaaaaaaaa", date="2026-06-25", player_a="111111111111",
        player_b="222222222222", **changes):
    value = dict(match_id=match_id, date=date, league="Test League",
                 player_a=player_a, player_b=player_b, elo_a_pre="1500",
                 elo_b_pre="1501", sol_a_pre="0.5", sol_b_pre="0.7",
                 p_model="0.48", winner="A", sets_a="3", sets_b="1")
    value.update(changes)
    return value


def raw_rows(rows, fields=FIELDS):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def inspect(raw):
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_exact_source_ids_and_same_day_appearances_are_counted_not_merged():
    raw = raw_rows([row(), row("bbbbbbbbbbbb", player_a="222222222222", player_b="111111111111"),
                    row("bbbbbbbbbbbb", date="2026-06-26", player_b="333333333333")])
    report = inspect(raw)
    assert report["rows"] == 3 and report["exact_player_id_strings"] == 3
    assert report["duplicate_match_id_values"] == 1 and report["rows_with_duplicate_match_id"] == 2
    assert report["date_player_groups_with_multiple_rows"] == 2
    assert report["rows_with_repeated_player_on_date"] == 2
    assert report["date_pair_groups_with_multiple_rows"] == 1 and report["rows_with_repeated_pair_on_date"] == 2
    assert report["date_decreases_in_file_order"] == 0


def test_invalid_and_equal_ids_do_not_create_time_groups():
    report = inspect(raw_rows([row(date="2026-06-25T10:00:00Z"), row("bbbbbbbbbbbb", player_b="111111111111"),
                              row("cccccccccccc", date="not-a-date", player_b="")]))
    assert report["invalid_date_label"] == 2
    assert report["same_id_both_sides_rows"] == 1 and report["both_player_ids_present_rows"] == 2
    assert report["date_player_groups_with_multiple_rows"] == 0


def test_source_score_shape_does_not_assert_competition_format():
    rows = [row(), row("b", winner="B", sets_a="0", sets_b="2"),
            row("c", winner="B", sets_a="3", sets_b="1"), row("d", sets_a="2", sets_b="2"),
            row("e", winner="", sets_a="3"), row("f", sets_a="-1")]
    assert inspect(raw_rows(rows))["score_shape_counts"] == {
        "other_shape_format_unverified": 1, "terminal_shape_format_unverified": 1,
        "winner_disagrees_with_set_totals": 1, "tied_set_totals": 1,
        "winner_missing_or_invalid": 1, "sets_missing_or_invalid": 1}


def test_numeric_domains_reject_nonfinite_and_out_of_range():
    report = inspect(raw_rows([row(elo_a_pre="NaN", elo_b_pre="Infinity", sol_a_pre="-0.1",
                                   sol_b_pre="1.1", p_model="1.5")]))
    assert all(value == 1 for value in report["invalid_numeric_counts"].values())


def test_grouping_is_source_local_not_a_person_or_cross_provider_claim():
    raw = raw_rows([row(), row("bbbbbbbbbbbb", date="2026-06-24", league="Other League")])
    report = inspect(raw)
    assert report["date_decreases_in_file_order"] == 1
    assert all(report[k] is False for k in (
        "source_ids_verified_as_people", "cross_provider_identity_verified", "upstream_rights_verified",
        "exact_match_time_verified", "pre_match_or_outcome_availability_verified",
        "source_fit_provenance_verified", "independent_holdout", "model_training_authorized",
        "bookmaker_prices_accepted", "real_money_execution"))
    text = json.dumps(report)
    assert "aaaaaaaaaaaa" not in text and "111111111111" not in text


def test_hash_size_schema_and_rows_fail_closed():
    raw = raw_rows([row()])
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="empty or oversized"):
        inspect(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="header"):
        inspect(raw_rows([row()], FIELDS + ("date",)))
    with pytest.raises(ValueError, match="header"):
        inspect(b"date,player_a\n2026-06-25,a\n")
    with pytest.raises(ValueError, match="row shape"):
        inspect(raw + b"short,row\n")
    with pytest.raises(ValueError, match="encoding"):
        inspect(b"\xff")


def test_empty_file_rows_and_duplicate_or_missing_ids_are_explicit():
    with pytest.raises(ValueError, match="empty sample CSV"):
        inspect(raw_rows([]))
    report = inspect(raw_rows([row(match_id=""), row(match_id="id"), row(match_id="id")]))
    assert report["missing_match_id"] == 1
    assert report["duplicate_match_id_values"] == 1 and report["rows_with_duplicate_match_id"] == 2
    assert report["opaque_hex12_rows"]["match"] == 0
