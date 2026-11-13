import pytest

from intraday_history_screen import predict_pair, evaluate
from test_market_anchor_screen import row


def fixture():
    target = {'fixture_id': '100', 'league': 'TT Cup', 'home': 'A', 'away': 'B',
              'day': '2026-10-24', 'kickoff': '2026-10-24T12:00:00Z', 'market': .5}
    valid = [(row(str(i), at=f'2026-10-2{i}T12:00:00Z'), 0) for i in (1, 2, 3)]
    valid.append((row('4', at='2026-10-24T10:00:00Z', winner=1), 1))
    return valid, target


def test_same_day_information_changes_history_without_changing_model_weights():
    valid, target = fixture()
    result = predict_pair(valid, target)
    assert result['prior_meetings'] == result['home_history'] == 3
    assert result['intraday_prior_meetings'] == result['intraday_home_history'] == 4
    assert result['intraday_prior_home_wins'] == result['prior_home_wins'] == 3
    assert result['intraday_h2h_anchor'] < result['h2h_anchor']


def test_delayed_update_future_match_and_target_result_cannot_leak():
    valid, target = fixture()
    before = predict_pair(valid, target)
    late = row('5', at='2026-10-24T11:00:00Z')
    late['updated_at'] = '2026-10-24T12:00:00Z'
    valid.extend([(late, 0), (row('6', at='2026-10-24T13:00:00Z'), 0),
                  (row('100', at='2026-10-24T09:00:00Z'), 0)])
    assert predict_pair(valid, target) == before


def test_both_h2h_windows_use_prediction_time_not_different_day_boundaries():
    valid, target = fixture()
    valid.append((row('7', at='2026-09-24T08:00:00Z'), 0))
    result = predict_pair(valid, target)
    assert result['prior_meetings'] == 3
    assert result['intraday_prior_meetings'] == 4
    assert result['home_history'] == 4  # Elo still uses all earlier history.


def test_forged_outcome_is_not_a_new_evaluation_sample():
    original = row('100', at='2026-10-24T13:00:00Z')
    source = {'sport': 'table_tennis', 'results': [original]}
    screen = {'source_sha256': 'a' * 64, 'rows': [{
        'fixture_id': '100', 'league': 'TT Cup', 'home': 'A', 'away': 'B',
        'day': '2026-10-24', 'kickoff': '2026-10-24T12:00:00Z',
        'market': .5, 'odds': ['2', '2'], 'outcome': 0}]}
    with pytest.raises(ValueError, match='source result'):
        evaluate(screen, source, 'a' * 64)
