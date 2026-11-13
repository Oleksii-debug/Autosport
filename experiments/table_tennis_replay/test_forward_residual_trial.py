"""Fail-closed checks at the new prospective observation boundary."""
from datetime import timedelta
import json
from pathlib import Path

import pytest

import forward_residual_trial as trial
from h2h_exploratory import instant
from verify_forward_residual import winner


def market(*, stale=False):
    return {'id': '42', 'home': 'A', 'away': 'B', 'group_status': {
        '920000': {'polled_at': '2026-11-13T10:00:00Z', 'stale': stale}},
        'quotes': [{'fixture_id': '42', 'group_id': '920000', 'line': 'To Win',
                    'market_id': 'm', 'sel_id': str(i), 'side': side,
                    'odds_dec': '1.95', 'ts': '2026-11-13T08:00:00Z'}
                   for i, side in enumerate(('A', 'B'))]}


def test_quote_freshness_uses_confirmed_market_poll_not_old_stable_price():
    server = instant('2026-11-13T10:01:59Z')
    assert trial.quote_pair(market(), server) is not None
    assert trial.quote_pair(market(), server + timedelta(seconds=2)) is None
    assert trial.quote_pair(market(stale=True), server) is None


@pytest.mark.parametrize('field,value', [('fixture_id', '43'), ('side', 'A'),
                                        ('suspended', True), ('market_id', 'another'),
                                        ('ts', '2026-11-13T10:00:01Z')])
def test_ambiguous_or_unavailable_opposing_quote_invalidates_pair(field, value):
    row = market()
    row['quotes'][1][field] = value
    assert trial.quote_pair(row, instant('2026-11-13T10:00:01Z')) is None


def test_outcome_cannot_be_requested_before_declared_cooling_period(tmp_path, monkeypatch):
    f = {'decision_at': '2026-11-13T10:00:00Z', 'rows': [{'kickoff': '2026-11-13T10:10:00Z'}]}
    raw = json.dumps(f).encode()
    (tmp_path / 'forecasts.json').write_bytes(raw)
    monkeypatch.setattr(trial, 'now', lambda: instant('2026-11-13T10:29:59Z'))
    monkeypatch.setattr(trial, 'capture', lambda *_: pytest.fail('early network request'))
    monkeypatch.setattr('sys.argv', ['trial', str(tmp_path), 'collect', '--hash', trial.sha(raw), '--followup', '1'])
    with pytest.raises(ValueError, match='too early'):
        trial.main()


def test_independent_score_reader_rejects_game_after_match_already_won():
    row = {'league': 'TT Cup', 'home': 'A', 'away': 'B', 'status': 'finished',
           'finished_at': '2026-11-13T10:15:00Z', 'updated_at': '2026-11-13T10:15:01Z'}
    games = [{'score': '11-3'}, {'score': '13-11'}, {'score': '11-8'}]
    assert winner({**row, 'periods': games}) == 0
    assert winner({**row, 'periods': [*games, {'score': '9-11'}]}) is None


def test_changed_forecast_cannot_trigger_outcome_request(tmp_path, monkeypatch):
    (tmp_path / 'forecasts.json').write_bytes(b'{"changed": true}')
    monkeypatch.setattr(trial, 'capture', lambda *_: pytest.fail('changed forecast triggered request'))
    monkeypatch.setattr('sys.argv', ['trial', str(tmp_path), 'collect', '--hash', '0'*64, '--followup', '1'])
    with pytest.raises(ValueError, match='hash mismatch'):
        trial.main()
