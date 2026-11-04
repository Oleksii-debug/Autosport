import hashlib
import json

import pytest

from elo_day_support_audit import audit
from test_elo_sample_inventory import raw_rows, row


def inspect(rows):
    raw = raw_rows(rows)
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_same_day_pair_is_not_prior_under_batch_rule():
    rows = [row(match_id="aaaaaaaaaaaa"),
            row(match_id="bbbbbbbbbbbb", player_a="222222222222", player_b="111111111111")]
    report = inspect(rows)
    assert report["strict_earlier_date_source_row_support"] == {
        "any_player": 0, "both_players": 0, "h2h_pair": 0}
    assert report["same_day_prefix_in_file_order"] == {
        "player_support_includes_same_day": 1, "any_player_only_from_same_day": 1,
        "both_players_only_from_same_day": 1, "h2h_support_includes_same_day": 1,
        "h2h_only_from_same_day": 1}
    assert report["model_training_authorized"] is False
    assert report["result_available_before_decision_verified"] is False


def test_earlier_date_support_and_same_day_inflation_are_distinct():
    rows = [row(), row("bbbbbbbbbbbb", date="2026-06-26"),
            row("cccccccccccc", date="2026-06-26", player_a="222222222222", player_b="111111111111"),
            row("dddddddddddd", date="2026-06-26", player_b="333333333333")]
    report = inspect(rows)
    assert report["strict_earlier_date_source_row_support"] == {
        "any_player": 3, "both_players": 2, "h2h_pair": 2}
    assert report["strict_earlier_date_h2h_count_distribution"] == {"0": 2, "1": 2, "2-4": 0, "5+": 0}
    assert report["same_day_prefix_in_file_order"]["player_support_includes_same_day"] == 2
    assert report["same_day_prefix_in_file_order"]["h2h_support_includes_same_day"] == 1
    assert report["same_day_prefix_in_file_order"]["h2h_only_from_same_day"] == 0


def test_within_day_reordering_preserves_batched_support():
    before = row(player_b="444444444444")
    ab = row("bbbbbbbbbbbb", date="2026-06-26")
    bc = row("cccccccccccc", date="2026-06-26", player_a="222222222222", player_b="333333333333")
    first, second = inspect([before, ab, bc]), inspect([before, bc, ab])
    assert first["strict_earlier_date_source_row_support"] == second["strict_earlier_date_source_row_support"]
    assert first["strict_earlier_date_h2h_count_distribution"] == second["strict_earlier_date_h2h_count_distribution"]
    assert first["same_day_prefix_in_file_order"] != second["same_day_prefix_in_file_order"]


def test_league_scope_is_exact_and_reversed_pair_stays_same():
    rows = [row(), row("bbbbbbbbbbbb", date="2026-06-26", league="Elsewhere",
                       player_a="222222222222", player_b="111111111111"),
            row("cccccccccccc", date="2026-06-26",
                player_a="222222222222", player_b="111111111111")]
    report = inspect(rows)
    assert report["strict_earlier_date_source_row_support"] == {
        "any_player": 1, "both_players": 1, "h2h_pair": 1}
    assert report["same_day_prefix_in_file_order"]["h2h_support_includes_same_day"] == 0


def test_outcomes_and_vendor_model_values_do_not_affect_support():
    original = [row(), row("bbbbbbbbbbbb", date="2026-06-26")]
    changed = [dict(item, winner="B", sets_a="0", sets_b="3",
                    p_model="0.99", elo_a_pre="2100") for item in original]
    a, b = inspect(original), inspect(changed)
    for key in ("strict_earlier_date_source_row_support", "strict_earlier_date_h2h_count_distribution",
                "same_day_prefix_in_file_order"):
        assert a[key] == b[key]


@pytest.mark.parametrize("bad", [
    {"date": "2026-06-25T01:00:00Z"}, {"match_id": ""}, {"player_a": ""},
    {"player_b": "111111111111"}, {"league": ""}, {"player_a": " 111111111111"},
])
def test_ambiguous_inputs_fail_closed(bad):
    with pytest.raises(ValueError):
        inspect([row(**bad)])


def test_duplicate_match_ids_and_wrong_digest_fail_closed():
    raw = raw_rows([row(), row(date="2026-06-26")])
    with pytest.raises(ValueError, match="ambiguous"):
        audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0" * 64)


def test_result_is_aggregate_only_and_cannot_authorize_learning():
    result = inspect([row(match_id="abcdef012345")])
    text = json.dumps(result)
    assert "abcdef012345" not in text and "111111111111" not in text
    for key in ("physical_people_verified", "physical_match_deduplicated",
                "event_date_verified", "result_available_before_decision_verified",
                "source_rights_verified", "independent_holdout",
                "model_training_authorized", "bookmaker_execution", "profitability_claim"):
        assert result[key] is False
