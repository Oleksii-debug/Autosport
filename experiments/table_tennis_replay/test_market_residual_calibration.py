from decimal import Decimal

import pytest

from market_residual_calibration import fit, forecast, prediction, digest, fixed_action_economics, BOOTSTRAPS
from market_anchor_screen import action, economics


def training():
    return [{'fixture_id': str(i), 'day': f'2026-10-{1+i//6:02d}',
             'kickoff': f'2026-10-{1+i//6:02d}T10:00:00Z',
             'result_updated_at': f'2026-10-{1+i//6:02d}T11:00:00Z',
             'market': .5, 'elo_anchor': .75 if i%2 else .25,
             'h2h_anchor': .75 if i%2 else .25, 'outcome': i%2}
            for i in range(30)]


def test_learns_repeatable_signal_and_reproduces_day_bootstrap():
    rows = training()
    a = fit(rows, cutoff='2026-10-06T00:00:00Z', source_sha256='a'*64)
    b = fit(rows, cutoff='2026-10-06T00:00:00Z', source_sha256='a'*64)
    assert a['weights'] == [1., 1.]
    assert digest(a) == digest(b)
    assert len(a['bootstrap_weights']) == BOOTSTRAPS


def test_learning_abstains_from_reliably_reversed_signal():
    rows = training()
    for r in rows:
        r['outcome'] = 1-r['outcome']
    m = fit(rows, cutoff='2026-10-06T00:00:00Z', source_sha256='a'*64)
    assert m['weights'] == [0., 0.]


def test_all_candidates_identical_to_market_prefer_zero_weights():
    rows = training()
    for r in rows:
        r['elo_anchor'] = r['h2h_anchor'] = r['market']
    m = fit(rows, cutoff='2026-10-06T00:00:00Z', source_sha256='a'*64)
    assert m['weights'] == [0., 0.]


def test_temporal_leak_duplicate_and_bad_probability_are_rejected():
    for variant in ('time', 'duplicate', 'probability'):
        rows = training()
        if variant == 'time': rows[0]['result_updated_at'] = '2026-10-06T00:00:00Z'
        if variant == 'duplicate': rows[1]['fixture_id'] = rows[0]['fixture_id']
        if variant == 'probability': rows[0]['market'] = float('nan')
        with pytest.raises(ValueError):
            fit(rows, cutoff='2026-10-06T00:00:00Z', source_sha256='a'*64)


def test_predict_does_not_read_outcome_and_checks_overlap():
    model = {'training_ids':['old'], 'cutoff':'2026-10-06T00:00:00Z',
             'weights':[1.,0.], 'bootstrap_weights':[[1.,0.]]*BOOTSTRAPS}
    row = {'fixture_id':'new','kickoff':'2026-10-07T00:00:00Z',
           'market':.5,'elo_anchor':.8,'h2h_anchor':.5,'odds':['2','2']}
    before = forecast(row,model)
    assert before == forecast({**row,'outcome':'not even a result'},model)
    assert before['calibrated_action'] == before['bootstrap_guarded_action'] == 0
    with pytest.raises(ValueError): forecast({**row,'fixture_id':'old'},model)
    with pytest.raises(ValueError): forecast({**row,'kickoff':'2026-10-05T00:00:00Z'},model)


def test_guard_uses_lower_probability_of_the_selected_away_side():
    model = {'training_ids':[], 'cutoff':'2026-10-06T00:00:00Z',
             'weights':[1.,0.], 'bootstrap_weights':[[0.,0.]]*BOOTSTRAPS}
    row = {'fixture_id':'new','kickoff':'2026-10-07T00:00:00Z',
           'market':.5,'elo_anchor':.2,'h2h_anchor':.5,'odds':['2','2']}
    r = forecast(row,model)
    assert r['calibrated_action'] == 1 and r['bootstrap_guarded_action'] is None
    assert r['bootstrap_lower_selected_side_probabilities'] == [.5,.5]
    assert r['calibrated_anchor'] == pytest.approx(.2)


def test_orientation_symmetry():
    row={'market':.7,'elo_anchor':.6,'h2h_anchor':.8}
    reverse={k:1-v for k,v in row.items()}
    assert prediction(reverse,[.5,.25]) == pytest.approx(1-prediction(row,[.5,.25]))


def test_fixed_actions_keep_real_abstention_with_profitable_underround():
    row={'fixture_id':'1','kickoff':'2026-10-07T00:00:00Z','day':'2026-10-07',
         'market':.5,'odds':['3','3'],'outcome':1,'actions':{'guard':None}}
    assert action(.5,tuple(map(Decimal,row['odds']))) is not None
    assert fixed_action_economics([row],'guard')['bets']==0


def test_fixed_economics_matches_canonical_on_same_actions():
    rows=[]
    for i,(p,y,odds) in enumerate(((.8,1,['2','2']),(.2,1,['2','2']),(.5,0,['1.8','1.8']))):
        rows.append({'fixture_id':str(i),'day':'2026-10-07','kickoff':'2026-10-07T00:00:00Z',
                     'market':.5,'test':p,'outcome':y,'odds':odds,
                     'actions':{'test':action(p,tuple(map(Decimal,odds)))}})
    actual=fixed_action_economics(rows,'test');expected=economics(rows,'test')
    for k in ('bets','wins','pnl','stress_pnl','roi','max_drawdown_in_kickoff_order'):
        assert actual[k] == expected[k]
