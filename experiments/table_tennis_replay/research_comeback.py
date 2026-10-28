"""Evaluate one pre-specified paper-only hypothesis on observed live snapshots.

For each complete match, take the first observed snapshot on which the pre-match
favourite trails 1:2 in sets and has an active quoted match-winner price. Results
are hypothetical: a displayed quote is not evidence that a bet was accepted.
"""
import argparse
from decimal import Decimal
import json
import math
import re
from pathlib import Path

def completed_sets(scores):
    if not isinstance(scores,str): return None
    sets=[0,0]
    for part in scores.split(','):
        match=re.fullmatch(r'\s*(\d+)\s*[-:]\s*(\d+)\s*',part)
        if not match:return None
        a,b=map(int,match.groups())
        if max(a,b)>=11 and abs(a-b)>=2: sets[0 if a>b else 1]+=1
    return sets

def active_price(item):
    item=item or {};value=item.get('odds')
    if item.get('suspended') or item.get('stale') or isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<=1:return None
    return Decimal(str(value))

def analyze(data):
    eligible=[]; excluded={}
    def skip(reason):excluded[reason]=excluded.get(reason,0)+1
    for m in data['matches']:
        result=m.get('result') or {}; periods=result.get('periods') or []
        if not result or not periods:skip('no_complete_result');continue
        wins=completed_sets(','.join(str(x.get('score','')) for x in periods))
        if wins is None or max(wins)!=3 or min(wins)>2:skip('not_verified_best_of_five');continue
        p=(m.get('prematch') or {}).get('odds') or {};home=active_price(p.get('home'));away=active_price(p.get('away'))
        if home is None or away is None or home==away:skip('no_unique_prematch_favourite');continue
        side=0 if home<away else 1;chosen=None
        for f in sorted(m.get('frames',[]),key=lambda x:x['offset_seconds']):
            if f.get('kind')!='live' or f.get('source_stale'):continue
            sets=completed_sets(f.get('score'));o=active_price((f.get('odds') or {}).get('home' if side==0 else 'away'))
            if sets is not None and sets[side]==1 and sets[1-side]==2 and o is not None:
                chosen=(f,o);break
        if not chosen:skip('not_observed_1_2_with_quote');continue
        f,o=chosen;won=wins[side]==3;net=o-1 if won else Decimal(-1)
        eligible.append({'fixture_id':m['id'],'home':m['home'],'away':m['away'],
                         'favourite':m['home'] if side==0 else m['away'],
                         'offset_seconds':f['offset_seconds'],'observed_at':f.get('observed_at'),
                         'live_score':f.get('score'),'odds':str(o),'won':won,'hypothetical_net':str(net)})
    return {'matches':len(data['matches']),'eligible':len(eligible),'wins':sum(x['won'] for x in eligible),
            'hypothetical_net_units':str(sum((Decimal(x['hypothetical_net']) for x in eligible),Decimal(0))),
            'excluded':excluded,'records':eligible,'accepted_wagers':False,
            'limitations':'First observed 1:2, not necessarily its first occurrence. No execution, stake costs or acceptance evidence.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('recording',type=Path);parser.add_argument('output',type=Path);args=parser.parse_args()
    report=analyze(json.loads(args.recording.read_text('utf-8')))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
