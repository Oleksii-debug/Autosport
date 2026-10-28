"""Read-only four-outcome coverage experiment on near-synchronous recorded prices.

Products of single-match quotes are hypothetical parlay prices, not bookmaker
offers. This checks a static full-cover basket, not every dynamic portfolio.
"""
import argparse
from decimal import Decimal
from itertools import combinations, product
import json
from pathlib import Path

from record import instant
from research_comeback import active_price, completed_sets

def basket(prices, budget=Decimal(1)):
    if len(prices)!=2 or any(len(p)!=2 for p in prices):
        raise ValueError('two binary matches required')
    if budget<=0 or any(not p.is_finite() or p<=1 for pair in prices for p in pair):
        raise ValueError('invalid budget or price')
    odds=[a*b for a,b in product(*prices)]
    inverse_sum=sum((1/o for o in odds),Decimal(0))
    stakes=[budget/(o*inverse_sum) for o in odds]
    nets=[s*o-budget for s,o in zip(stakes,odds)]
    return {'product_odds':[str(o) for o in odds], 'stakes':[str(s) for s in stakes],
        'scenario_net':[str(n) for n in nets], 'best_worst_case_net':str(min(nets)),
        'sum_inverse_odds':str(inverse_sum)}

def analyze(data):
    events=[]
    for m in data['matches']:
        for f in m.get('frames',[]):
            if f.get('kind')!='live' or f.get('source_stale'): continue
            sets=completed_sets(f.get('score'))
            if sets is None or max(sets)>=3:continue
            when=instant(f['observed_at'])
            prices=[active_price((f.get('odds') or {}).get(s)) for s in ('home','away')]
            if None in prices:continue
            if any(not (f['odds'][s] or {}).get('market_checked_at') or
                not 0<=(when-instant(f['odds'][s]['market_checked_at'])).total_seconds()<=120 for s in ('home','away')):continue
            events.append((when,m,f,prices))
    events.sort(key=lambda x:x[0]); latest={}; seen=set();checks=[]
    for when,m,f,prices in events:
        latest[m['id']]=(when,m,f,prices)
        current=[v for v in latest.values() if (when-v[0]).total_seconds()<=45]
        for a,b in combinations(current,2):
            if {a[1]['home'],a[1]['away']} & {b[1]['home'],b[1]['away']}:continue
            key=tuple(sorted(((a[1]['id'],a[2]['observed_at']),(b[1]['id'],b[2]['observed_at']))))
            if key in seen:continue
            seen.add(key)
            checks.append({'at':when.isoformat(),'fixtures':[a[1]['id'],b[1]['id']],
                'names':[a[1]['home']+' — '+a[1]['away'],b[1]['home']+' — '+b[1]['away']],
                'observed_at':[a[2]['observed_at'],b[2]['observed_at']],
                'single_prices':[[str(p) for p in a[3]],[str(p) for p in b[3]]],
                **basket([a[3],b[3]])})
    checks.sort(key=lambda x:Decimal(x['best_worst_case_net']),reverse=True)
    return {'source_last_checked_at':data.get('last_checked_at'),'quote_pair_checks':len(checks),
        'unique_match_pairs':len({tuple(sorted(x['fixtures'])) for x in checks}),
        'positive_worst_case_baskets':sum(Decimal(x['best_worst_case_net'])>0 for x in checks),
        'best':checks[0] if checks else None,'worst':checks[-1] if checks else None,
        'checks':checks,'accepted_wagers':False,
        'limitations':'Synthetic products, no execution proof. At most 45 seconds between snapshots; prices may change within that interval. Overlapping observations are not independent trials. Static four-ticket baskets only, not all dynamic or partial portfolios.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('recording',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    a.output.write_text(json.dumps(analyze(json.loads(a.recording.read_text())),ensure_ascii=False,indent=2))
