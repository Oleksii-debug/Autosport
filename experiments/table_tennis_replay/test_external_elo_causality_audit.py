import csv
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from external_elo_causality_audit import (
    CSV_FIELDS, _definitions, _source, prediction_inventory, synthetic_equal_timestamp_demo,
)


def make_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def row(a, b, winner, target, category="MT"):
    value = {key: "" for key in CSV_FIELDS}
    value.update(player_a_id=a, player_b_id=b, winner_id=winner, target=target, category=category)
    return value


def inspect(raw):
    return prediction_inventory(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_inventory_counts_exact_keys_without_leaking_them():
    r1 = row("1234501", "1234502", "1234501", "1")
    r2 = row("1234502", "1234501", "1234501", "0")
    r3 = row("1234503", "1234504", "1234503", "1", category="WT")
    report = inspect(make_csv([r1, r2, r3, r3]))
    assert report["rows"] == 4
    assert report["distinct_exact_source_player_keys"] == 4
    assert report["source_player_keys_with_multiple_rows"] == 4
    assert report["distinct_unordered_source_key_pairs"] == 2
    assert report["unordered_source_pairs_with_multiple_rows"] == 2
    assert report["rows_in_repeated_source_pairs"] == 4
    assert (report["exact_duplicate_value_groups"], report["rows_in_exact_duplicate_value_groups"]) == (1, 2)
    assert (report["global_source_key_components"], report["largest_component_rows"]) == (2, 2)
    assert all(report[k] is False for k in ("row_match_id_present", "row_match_time_present",
        "source_result_availability_present", "market_prices_present", "input_history_or_training_verified",
        "physical_identity_verified", "physical_event_deduplication_verified", "source_rights_verified",
        "independent_holdout_verified", "real_money_execution"))
    for identity in ("1234501", "1234502", "1234503", "1234504"):
        assert identity not in json.dumps(report)


@pytest.mark.parametrize("changes", [
    {"player_a_id": "x"}, {"player_b_id": "1234501"}, {"winner_id": "1234599"},
    {"target": "0"}, {"category": "MS"}, {"winner_id": "-1"},
])
def test_invalid_source_identity_or_target_fails_closed(changes):
    value = row("1234501", "1234502", "1234501", "1")
    value.update(changes)
    with pytest.raises(ValueError):
        inspect(make_csv([value]))


def test_csv_schema_hash_and_empty_fail_closed():
    raw = make_csv([row("11", "12", "11", "1")])
    with pytest.raises(ValueError, match="hash"):
        prediction_inventory(raw + b" ", expected_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="columns"):
        inspect(raw.replace(b"player_a_id", b"player_z_id", 1))
    with pytest.raises(ValueError, match="empty"):
        inspect(make_csv([]))


def test_source_mismatch_is_rejected_before_loading(tmp_path):
    p = tmp_path / "ratings" / "elo.py"
    p.parent.mkdir()
    p.write_text("raise AssertionError('must not execute')\n")
    with pytest.raises(ValueError, match="hash"):
        _source(tmp_path, "ratings/elo.py")


def test_ast_loader_does_not_execute_external_top_level():
    namespace = {}
    _definitions("raise RuntimeError('not run')\ndef sample():\n return 7\n", ("sample",), namespace)
    assert namespace["sample"]() == 7
    with pytest.raises(ValueError, match="definitions"):
        _definitions("def wrong():\n return 1\n", ("sample",), {})


def test_exact_pinned_upstream_functions_on_synthetic_equal_times():
    path = os.environ.get("TT_TOPSPIN_SOURCE_DIR")
    if not path:
        pytest.skip("Set TT_TOPSPIN_SOURCE_DIR to the three pinned upstream files")
    report = synthetic_equal_timestamp_demo(Path(path))
    assert report["synthetic_only"] and report["same_timestamp_on_both_input_rows"]
    assert report["equal_timestamp_outcome_changes_target_elo"]
    assert report["first_row_a_wins"]["elo_a_before"] == 1516
    assert report["first_row_a_loses"]["elo_a_before"] == 1484
    assert report["first_row_a_wins"]["form_last_5_a"] == 1
    assert report["first_row_a_loses"]["form_last_5_a"] == 0
    assert report["target_row_first_in_input"]["elo_a_before"] == 1500
    assert report["target_row_first_in_input"]["form_last_5_a"] is None
    assert report["real_model_reproduced"] is False and report["real_money_execution"] is False
