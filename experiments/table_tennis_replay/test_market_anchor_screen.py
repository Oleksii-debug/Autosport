from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pytest

from h2h_exploratory import instant
from market_anchor_screen import (action, closing_prices, economics, evaluate,
    history_state, probabilities, selected_population)


def row(ident='1', home='A', away='B', at='2026-10-22T12:00:00Z', winner=0):
    score = '11-7' if winner == 0 else '7-11'
    return {'fixture_id': ident, 'sport': 'table_tennis', 'league': 'TT Cup',
        'home': home, 'away': away, 'status': 'finished', 'finished_at': at,
        'updated_at': at, 'final_score': ','.join([score] * 3),
        'periods': [{'name': 'Set ' + str(i + 1), 'score': score} for i in range(3)]}


def close(r):
    sels = []
    for i, side in enumerate((r['home'], r['away'])):
        s = {'group_id': '920000', 'market_id': '920000', 'sel_id': str(10 + i),
             'group_name': 'Match Lines', 'line': 'To Win', 'side': side}
        s['pre_close'] = {**s, 'ts': '2026-10-23T10:00:00Z', 'suspended': False,
                          'odds_dec': '2.1' if i == 0 else '1.8'}
        sels.append(s)
    return {'fixture_id': r['fixture_id'], 'fixture_ids': [r['fixture_id']],
        'prematch_id': r['fixture_id'], 'closed': True, 'kickoff_known': True,
        'kickoff_estimated': False, 'pre_close_basis': 'kickoff',
        'kickoff_utc': '2026-10-23T11:00:00Z', 'selections': sels}


def test_identifier_sampling_ignores_outcomes_and_status():
    a = {'results': [row(str(i), at='2026-10-23T12:00:00Z') for i in range(50)]}
    b = deepcopy(a)
    for r in b['results']:
        r['status'] = 'unknown'
        r['periods'] = []
        r['final_score'] = '0-0'
    assert selected_population(a) == selected_population(b)
    assert len(selected_population(a)['2026-10-23']) == 15


def test_cutoff_excludes_current_day_delayed_updates_and_current_id():
    old = row()
    current = row('2', at='2026-10-23T12:00:00Z')
    revised = row('3')
    revised['updated_at'] = '2026-10-23T00:00:00Z'
    explicit_current = row('4')
    cutoff = instant('2026-10-23T00:00:00Z')
    state = history_state([(old, 0), (current, 0), (revised, 0), (explicit_current, 0)], cutoff, exclude_id='4')
    assert state[1][('TT Cup', 'A')] == 1
    assert state[0][('TT Cup', 'A')] == 1512


def test_equal_timestamp_batch_has_no_serial_order_effect():
    a, b = row('1'), row('2', away='C')
    cutoff = instant('2026-10-23T00:00:00Z')
    x = history_state([(a, 0), (b, 0)], cutoff, exclude_id=None)
    a['fixture_id'], b['fixture_id'] = '2', '1'
    y = history_state([(b, 0), (a, 0)], cutoff, exclude_id=None)
    assert x == y
    assert x[0][('TT Cup', 'A')] == 1524


def test_h2h_orientation_support_and_fallback():
    valid = [(row(str(i)), 0) for i in range(3)]
    state = history_state(valid, instant('2026-10-23T00:00:00Z'), exclude_id=None)
    p = probabilities(row(), .5, state)
    reverse = probabilities(row(home='B', away='A'), .5, state)
    assert p['h2h_anchor'] == pytest.approx(13 / 23)
    assert reverse['h2h_anchor'] == pytest.approx(10 / 23)
    assert p['elo_anchor'] == .5  # only 3 matches / 1 opponent
    before = deepcopy(state)
    probabilities(row(home='New', away='Unknown'), .6, state)
    assert state == before
    other = row()
    other['league'] = 'Setka Cup'
    assert probabilities(other, .6, state)['h2h_anchor'] == .6


def test_elo_corrects_for_opposition_without_target_result():
    valid = []
    for i in range(10):
        for name, winner in [('A', 0), ('B', 1)]:
            r = row(str(len(valid) + 1), home=name, away='C' if i % 2 else 'D',
                    at=f'2026-10-{10+i:02d}T12:00:00Z', winner=winner)
            valid.append((r, winner))
    state = history_state(valid, instant('2026-10-23T00:00:00Z'), exclude_id=None)
    target = row('50', at='2026-10-23T12:00:00Z')
    p = probabilities(target, .5, state)
    assert p['elo_supported']
    assert .5 < p['elo_anchor'] < .6
    target['periods'] = []
    target['final_score'] = 'changed target outcome'
    assert probabilities(target, .5, state) == p


def test_close_accepts_unchanged_price_not_called_executable():
    r = row(at='2026-10-23T12:00:00Z')
    c = close(r)
    price, why = closing_prices(c, r, '2026-10-23')
    assert why is None
    assert price['odds'] == (Decimal('2.1'), Decimal('1.8'))
    c['fixture_ids'] += ['99']
    c['prematch_id'] = '99'
    assert closing_prices(c, r, '2026-10-23')[1] is None
    c['fixture_ids'] += ['98']
    assert closing_prices(c, r, '2026-10-23')[1] == 'ambiguous_fixture_link'


def test_claimed_alias_with_separate_result_cannot_settle_another_meeting():
    r = row(at='2026-10-23T12:00:00Z')
    c = close(r)
    c['fixture_ids'] = ['1', '99']
    c['prematch_id'] = '99'
    assert closing_prices(c, r, '2026-10-23', result_ids=frozenset(('1','99')))[1] == 'alias_has_separate_result'


@pytest.mark.parametrize('mutation,reason', [
    (lambda c: c.update(kickoff_estimated=True), 'kickoff_unconfirmed'),
    (lambda c: c.update(kickoff_utc='2026-10-22T11:00:00Z'), 'kickoff_date_or_order'),
    (lambda c: c['selections'].append(deepcopy(c['selections'][0])), 'ambiguous_selection'),
    (lambda c: c['selections'][0]['pre_close'].update(suspended=True), 'missing_or_suspended_close'),
    (lambda c: c['selections'][0]['pre_close'].update(sel_id='wrong'), 'conflicting_selection_identity'),
    (lambda c: c['selections'][0]['pre_close'].update(ts='2026-10-23T11:00:01Z'), 'price_or_time_outside_protocol'),
    (lambda c: c['selections'][0]['pre_close'].update(odds_dec='NaN'), 'price_or_time_outside_protocol'),
])
def test_closing_rejects_conflicting_or_future_data(mutation, reason):
    r = row(at='2026-10-23T12:00:00Z')
    c = close(r)
    mutation(c)
    assert closing_prices(c, r, '2026-10-23')[1] == reason


def test_decimal_pnl_stress_same_actions_and_away_orientation():
    rs = []
    for i, outcome in enumerate((1, 0, 0)):
        rs.append({'fixture_id': str(i), 'kickoff': str(i), 'day': '2026-10-23',
                   'market': .5, 'elo_anchor': .7 if i < 2 else .3,
                   'odds': ['2', '2'], 'outcome': outcome})
    e = economics(rs, 'elo_anchor')
    assert (e['bets'], e['wins']) == (3, 2)
    assert Decimal(e['pnl']) == Decimal('.89')
    assert Decimal(e['stress_pnl']) == Decimal('.77')
    assert Decimal(e['max_drawdown_in_kickoff_order']) == Decimal('1.01')
    assert Decimal(e['market_expected_pnl_same_actions']) == Decimal('-.09')
    assert action(.5, (Decimal('1.9'), Decimal('1.9'))) is None


def test_full_replay_includes_missing_unknown_zero_days_and_checks_receipts(tmp_path):
    r = row(at='2026-10-23T12:00:00Z')
    unknown = row('2', at='2026-10-24T12:00:00Z')
    unknown['status'] = 'unknown'
    missing = row('3', at='2026-10-24T13:00:00Z')
    archive = {'sport': 'table_tennis', 'results': [r, unknown, missing]}
    sel = {'days': selected_population(archive)}
    raw = json.dumps(close(r)).encode()
    (tmp_path/'1.body.json').write_bytes(raw)
    receipt = {'sha256': hashlib.sha256(raw).hexdigest(), 'url': 'https://moviodds.com/closing?fi=1',
        'requested_at': '2026-11-09T00:00:00Z', 'received_at': '2026-11-09T00:00:01Z'}
    (tmp_path/'1.receipt.json').write_text(json.dumps(receipt))
    report = evaluate(archive, sel, tmp_path)
    assert report['coverage'] == {'included': 1, 'invalid_or_unknown_result': 1, 'not_captured': 1}
    assert report['all']['market']['economics']['daily']['2026-10-24']['bets'] == 0
    assert not report['profitability_proven'] and not report['production_promotion']
    (tmp_path/'1.body.json').write_bytes(raw+b' ')
    with pytest.raises(ValueError, match='digest'):
        evaluate(archive, sel, tmp_path)
