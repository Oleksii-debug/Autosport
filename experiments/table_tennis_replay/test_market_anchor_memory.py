from copy import deepcopy
from decimal import Decimal
import pytest

from autosport.shadow_strategy_memory import ShadowStrategyMemory
from market_anchor_memory import replay
from test_market_anchor_screen import row


def population():
    source, forecasts = [], []
    for d in (23, 24):
        for n in range(40):
            ident = str(d * 100 + n)
            day = f'2026-10-{d}'
            original = row(ident, at=day+'T12:00:00Z')
            source.append(original)
            forecasts.append({'fixture_id': ident, 'day': day, 'league': 'TT Cup',
                'home': 'A', 'away': 'B', 'outcome': 1, 'kickoff': day+'T11:00:00Z',
                'odds': ['2', '2'], 'market': .5, 'elo_anchor': .7,
                'h2h_anchor': .5, 'combined_anchor': .5})
    report = {'source_sha256': 'a'*64, 'rows': forecasts,
              'coverage_by_day': {'2026-10-23': {}, '2026-10-24': {}}}
    return report, {'sport': 'table_tennis', 'results': source}


def test_memory_selects_only_after_earlier_feedback_and_persists(tmp_path):
    r, s = population()
    out, memory = replay(r, s, 'a'*64)
    assert [d['policy'] for d in out['days']] == ['WAIT', 'elo_anchor']
    assert out['feedback_events_in_checkpoint'] == 40
    assert out['economics']['bets'] == 40
    assert Decimal(out['economics']['pnl']) == Decimal(38)
    p = tmp_path/'research-memory.json'
    memory.save(p)
    assert ShadowStrategyMemory.load(p).payload() == memory.payload()
    assert not memory.payload()['execution_authorized']


def test_same_day_losses_do_not_rewrite_earlier_choice():
    r, s = population()
    for v in s['results'][40:]:
        v['periods'] = [{'name': 'Set '+str(i+1), 'score': '7-11'} for i in range(3)]
        v['final_score'] = '7-11,7-11,7-11'
    for v in r['rows'][40:]:
        v['outcome'] = 0
    out, _ = replay(r, s, 'a'*64)
    assert [d['policy'] for d in out['days']] == ['WAIT', 'elo_anchor']
    assert Decimal(out['economics']['pnl']) == Decimal('-40.40')


def test_delayed_source_updates_are_not_revealed_early():
    r, s = population()
    for v in s['results'][:40]:
        v['updated_at'] = '2026-10-24T00:00:00Z'
    out, _ = replay(r, s, 'a'*64)
    assert out['choices'] == {'WAIT': 2}
    assert out['feedback_events_in_checkpoint'] == 0


def test_negative_memory_prefers_zero_action():
    r, s = population()
    for v in r['rows']:
        v['elo_anchor'] = .3  # backing the losing away player
    out, _ = replay(r, s, 'a'*64)
    assert out['choices'] == {'WAIT': 2}
    assert out['economics']['bets'] == 0


def test_cannot_supply_forecast_outcome_in_conflict_with_source():
    r, s = population()
    r['rows'][0]['outcome'] = 0
    with pytest.raises(ValueError, match='result mismatch'):
        replay(r, s, 'a'*64)
