"""Offline check of selection, set-count features and rational PAPER arithmetic."""
import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from fractions import Fraction
import json
import math
from pathlib import Path

from verify_forward_residual import LEAGUES, capture, labels, read, require, settlement, sha, stamp, winner

DATE = ('2026-11-11', '2026-11-12')
QUARANTINE = {'204173406', '204188383'}
POLICIES = ('market', 'h2h_anchor', 'h2h_set_anchor')


def best_of_five(q):
    return q**3 * (10 - 15*q + 6*q*q)


def verify(root, source, expected, protocol_time):
    src, source_receipt = capture(source, 'history')
    raw = (root / 'selection.json').read_bytes()
    require(sha(raw) == expected, 'selection not externally committed')
    selection = json.loads(raw)
    original = {r['fixture_id']: r for r in src['results']}
    require(len(original) == len(src['results']), 'archive duplicate')
    available = stamp(source_receipt['received_at'])
    groups = defaultdict(list)
    for ident, r in original.items():
        if ident in QUARANTINE or r.get('league') not in LEAGUES:
            continue
        finished = stamp(r['finished_at'])
        if finished.date().isoformat() in DATE and 12 <= finished.hour <= 19:
            groups[finished.date().isoformat(), finished.hour].append(ident)
    reconstructed = [{'date': d, 'finish_hour_utc': h, 'population': len(ids),
                      'ids': sorted(ids, key=lambda i: sha(('autosport-set-replication-v1|' + i).encode()))[:8]}
                     for (d, h), ids in sorted(groups.items())]
    require(reconstructed == selection, 'selection population mismatch')
    ids = [i for group in selection for i in group['ids']]
    attempts = read(root, 'attempts')
    require(ids == [r['fixture_id'] for r in attempts], 'missing or unplanned source attempt')
    committed = stamp(protocol_time)
    captured = 0
    closing = {}
    for attempt in attempts:
        if attempt['status'] == 'captured':
            ident = attempt['fixture_id']
            closing[ident], receipt = capture(root, ident)
            require(receipt['url'] == 'https://moviodds.com/closing?fi=' + ident
                    and stamp(receipt['requested_at']) > committed, 'closing attempt scope or chronology')
            captured += 1
    frozen = read(root, 'forecasts')
    evaluation = read(root, 'evaluation')
    require(frozen['archive_sha256'] == source_receipt['sha256'] and frozen['selection_sha256'] == expected,
            'forecast input mismatch')
    require(evaluation['forecast_sha256'] == sha((root/'forecasts.json').read_bytes()), 'evaluation forecast mismatch')
    fixed = {r['fixture_id']: r for r in frozen['rows']}
    excluded = {r['fixture_id'] for r in frozen['exclusions']}
    require(not fixed.keys() & excluded and set(ids) == fixed.keys() | excluded, 'selected cohort not accounted')
    rows = evaluation['rows']
    require([r['fixture_id'] for r in rows] == list(fixed), 'label join altered cohort')
    counts = {d: defaultdict(Counter) for d in DATE}
    for old in original.values():
        if old['fixture_id'] in QUARANTINE or old.get('league') not in LEAGUES:
            continue
        won = winner(old)
        if won is None:
            continue
        updated = stamp(old['updated_at'])
        finished = stamp(old['finished_at'])
        league, home, away = labels(old)
        key = (league, *sorted((home, away)))
        for day in DATE:
            cutoff = stamp(day+'T00:00:00Z')
            if cutoff - timedelta(days=30) <= finished <= updated < cutoff:
                side = (home, away)[won]
                counts[day][key][side, 'matches'] += 1
                for s in old['periods']:
                    a, b = map(int, s['score'].split('-'))
                    counts[day][key][(home if a > b else away), 'sets'] += 1
    for r in rows:
        original_row = original[r['fixture_id']]
        w = winner(original_row)
        require(w in (0, 1) and stamp(original_row['updated_at']) <= available and labels(r) == labels(original_row)
                and r['outcome'] == int(w == 0) and {k:v for k,v in r.items() if k!='outcome'} == fixed[r['fixture_id']],
                'label does not bind to frozen fixture')
        league, a, b = labels(r)
        close = closing[r['fixture_id']]
        aliases = close['fixture_ids']
        prematch = close.get('prematch_id')
        require(close['fixture_id'] == r['fixture_id'] and (aliases == [r['fixture_id']]
                or (isinstance(prematch, str) and prematch not in original and len(aliases)==2
                    and set(aliases)=={r['fixture_id'],prematch})) and close['closed'] is True
                and close['kickoff_known'] is True and close['kickoff_estimated'] is False
                and close['pre_close_basis']=='kickoff' and stamp(close['kickoff_utc']) == stamp(r['kickoff'])
                and stamp(r['kickoff']) < stamp(original_row['finished_at'])
                and stamp(r['kickoff']).date().isoformat()==r['day'] and 12 <= stamp(r['kickoff']).hour < 20,
                'closing identity or clock mismatch')
        quotes = [q for q in close['selections'] if q.get('group_id')=='920000' and q.get('line')=='To Win']
        require(len(quotes)==2 and {q.get('side') for q in quotes}=={a,b}, 'missing closing sides')
        quotes.sort(key=lambda q:q['side']!=a)
        require(quotes[0]['market_id']==quotes[1]['market_id'] and quotes[0]['sel_id']!=quotes[1]['sel_id']
                and quotes[0]['group_name']==quotes[1]['group_name'] and all(
                    isinstance(q[k],str) and q[k] for q in quotes for k in ('market_id','sel_id','group_name')),
                'ambiguous closing market')
        for q, v in zip(quotes,r['odds'],strict=True):
            c = q['pre_close']
            require(c['suspended'] is False and Fraction(c['odds_dec']) == Fraction(v) > 1
                    and timedelta() <= stamp(r['kickoff']) - stamp(c['ts']) <= timedelta(days=1)
                    and all(q[k]==c[k] for k in ('group_id','market_id','sel_id','group_name','side','line') if k in c)
                    and all(x.get('fixture_id',prematch or r['fixture_id']) in aliases for x in (q,c)),
                    'closing price provenance mismatch')
        c = counts[r['day']][league, *sorted((a, b))]
        n = c[a, 'matches'] + c[b, 'matches']
        s = c[a, 'sets'] + c[b, 'sets']
        o = list(map(Fraction, r['odds']))
        m = float(o[1] / sum(o))
        lo, hi = 0., 1.
        for _ in range(52):
            x = (lo + hi) / 2
            if best_of_five(x) < m:
                lo = x
            else:
                hi = x
        p = best_of_five((80 * (lo + hi)/2 + c[a, 'sets'])/(80+s)) if n >= 3 else m
        h = (20*m + c[a, 'matches']) / (20+n) if n >= 3 else m
        require((r['prior_meetings'], r['prior_home_wins'], r['prior_home_sets'], r['prior_away_sets']) ==
                (n, c[a,'matches'], c[a,'sets'], c[b,'sets']) and abs(m-r['market']) < 1e-14
                and abs(h-r['h2h_anchor']) < 1e-14 and abs(p-r['h2h_set_anchor']) < 1e-13,
                'independent prior counts or set model mismatch')
        for key in POLICIES:
            edge = [Fraction(str(v))*price*Fraction(98,100)-Fraction(101,100)
                    for v,price in zip((r[key],1-r[key]),o,strict=True)]
            pick = int(edge[1] > edge[0])
            require(r['actions'][key] == (pick if edge[pick] >= Fraction(3,100) else None), 'action mismatch')
    scopes = [(rows, evaluation['pooled'])] + [([r for r in rows if r['day'] == d], evaluation['by_day'][d]) for d in DATE]
    for sample, report in scopes:
        require(len(sample) == report['n'], 'cohort size mismatch')
        for key in POLICIES:
            b, w, net, stress, dd = settlement(sample, key)
            e = report['economics'][key]
            require((b,w) == (e['bets'],e['wins']) and all(abs(x-Fraction(e[k])) < Fraction(1,10**23)
                    for x,k in ((net,'pnl'),(stress,'stress_pnl'),(dd,'max_drawdown_in_kickoff_order'))), 'return mismatch')
            metrics = report['scores'][key]
            if not sample:
                require(metrics is None, 'empty sample scores')
            else:
                brier = sum((r[key]-r['outcome'])**2 for r in sample)/len(sample)
                loss = -sum(math.log(r[key] if r['outcome'] else 1-r[key]) for r in sample)/len(sample)
                require(abs(brier-metrics['brier'])<1e-14 and abs(loss-metrics['log_loss'])<1e-14, 'score mismatch')
    return {'selection_sha256': expected, 'selected': len(ids), 'captured': captured, 'eligible': len(rows),
            'independent_selection_set_features_and_paper_arithmetic_verified': True,
            'executable_prices_verified': False, 'profitability_proven': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('source', type=Path)
    p.add_argument('--selection-hash', required=True)
    p.add_argument('--protocol-time', required=True)
    a = p.parse_args()
    print(json.dumps(verify(a.root,a.source,a.selection_hash,a.protocol_time),indent=2))
