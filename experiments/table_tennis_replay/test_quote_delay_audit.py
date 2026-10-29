from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from prospective_capture import collect
from quote_delay_audit import audit

BASE = datetime(2026, 10, 29, tzinfo=timezone.utc)


def stamp(s):
    return (BASE + timedelta(seconds=s)).isoformat()


def match(identity, seconds, home_price=1.5, away_price=2.5):
    q = lambda side, selection, price: {'fixture_id': identity, 'group_id': '240',
        'market_id': '920054', 'group_name': 'Match', 'phase': 'inplay', 'side': side, 'sel_id': selection,
        'odds_dec': price, 'ts': stamp(seconds)}
    return {'id': identity, 'sport': 'table_tennis', 'in_play': True,
            'home': identity+'A', 'away': identity+'B', 'league': 'L',
            'group_status': {'240': {'polled_at': stamp(seconds)}},
            'quotes': [q(identity+'A', identity+'-1', home_price), q(identity+'B', identity+'-2', away_price)]}


def build_session(path, frames, interval=40):
    ticks = iter([BASE, BASE+timedelta(seconds=1), BASE+timedelta(seconds=2), BASE+timedelta(seconds=3),
                  BASE+timedelta(seconds=interval), BASE+timedelta(seconds=interval+1),
                  BASE+timedelta(seconds=interval+2), BASE+timedelta(seconds=interval+3)])
    frames = iter(frames)
    def fetch(url):
        if '/odds?' in url:
            index, ms = next(frames)
            return json.dumps({'server_time': stamp(index), 'matches': ms}).encode()
        return b'{"sport":"table_tennis","results":[]}'
    collect(path, cycles=2, fetch=fetch, clock=lambda: next(ticks), sleep=lambda _: None)


def test_adjacent_same_selection_and_pair_exposure(tmp_path):
    a, b = match('a',0), match('b',0,2.4,1.6)
    a2, b2 = match('a',40,1.4,2.5), match('b',40,2.4,1.8)
    build_session(tmp_path, [(0,[a,b]),(40,[a2,b2])])
    total = audit(tmp_path)['totals']
    assert [total[k] for k in ('selected','lower','higher','missing_or_invalid')] == [2,1,1,0]
    assert total['initial_fixture_pairs'] == total['both_legs_comparable_pairs'] == 1
    assert total['comparable_pairs_at_least_one_lower'] == 1


def test_missing_suspended_stale_or_changed_id_is_not_a_loss(tmp_path):
    a, b, c, d = (match(i,0) for i in 'abcd')
    b2,c2,d2 = (match(i,40) for i in 'bcd')
    b2['quotes'][0]['suspended']=True
    c2['group_status']['240']['stale']=True
    d2['quotes'][0]['sel_id']='new-selection'
    build_session(tmp_path, [(0,[a,b,c,d]),(40,[b2,c2,d2])])
    total = audit(tmp_path)['totals']
    assert total['selected'] == total['missing_or_invalid'] == 4
    assert total['lower'] == total['higher'] == total['both_legs_comparable_pairs'] == 0
    assert total['initial_fixture_pairs'] == 6


def test_future_change_cannot_change_initial_candidate_count(tmp_path):
    a,b=match('a',0),match('b',0)
    a2,b2=match('a',40,1.1,9),match('b',40,4,1.01)
    build_session(tmp_path,[(0,[a,b]),(40,[a2,b2])])
    total=audit(tmp_path)['totals']
    assert total['selected']==2 and total['lower']==1 and total['higher']==1


def test_duplicate_ids_fail_closed(tmp_path):
    a=match('a',0)
    build_session(tmp_path,[(0,[a,deepcopy(a)]),(40,[match('a',40)])])
    with pytest.raises(ValueError,match='duplicate fixture'):
        audit(tmp_path)


def test_unqualified_delay_not_interpolated(tmp_path):
    build_session(tmp_path,[(0,[match('a',0)]),(61,[match('a',61)])],interval=61)
    report=audit(tmp_path)
    assert report['totals']['intervals_outside_30_60_seconds']==1
    assert report['totals']['selected']==0


def test_wrong_market_fixture_phase_or_duplicate_selection_rejected(tmp_path):
    examples=[]
    for mutation in ('market','fixture','phase','duplicate','label','period','group_name'):
        a=match('a',0)
        if mutation=='market':a['quotes'][0]['market_id']='other'
        if mutation=='fixture':a['quotes'][0]['fixture_id']='other'
        if mutation=='phase':a['quotes'][0]['phase']='prematch'
        if mutation=='duplicate':a['quotes'].append(deepcopy(a['quotes'][0]))
        if mutation=='label':a['quotes'][0]['label']='inconsistent'
        if mutation=='period':a['quotes'][0]['period']='2'
        if mutation=='group_name':a['quotes'][0]['group_name']='Set Winner'
        examples.append(a)
    for i,a in enumerate(examples):
        sub=tmp_path/str(i)
        build_session(sub,[(0,[a]),(40,[match('a',40)])])
        assert audit(sub)['totals']['selected']==0
