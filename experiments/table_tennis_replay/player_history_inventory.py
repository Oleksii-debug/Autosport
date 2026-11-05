"""Descriptive source-name graph, never a verified player identity registry."""
import argparse
import hashlib
import json
from pathlib import Path
from h2h_exploratory import result, instant


def build(archive):
    if archive.get('sport') != 'table_tennis':
        raise ValueError('wrong sport')
    seen, players, pairs, excluded = set(), {}, {}, {}
    for row in archive['results']:
        ident = str(row.get('fixture_id') or '')
        if not ident or ident in seen:
            raise ValueError('missing or duplicate fixture ID')
        seen.add(ident)
        names = [row.get('home'), row.get('away')]
        league = row.get('league')
        why = None
        if not all(isinstance(s,str) and s.strip() for s in names+[league]) or names[0] == names[1]:
            why = 'ambiguous_names'
        if row.get('status') != 'finished':
            why = 'not_finished'
        r = result({'result': {'observed_at': row.get('updated_at'), 'periods': row.get('periods')}})
        try:
            chronology_ok = instant(row.get('finished_at')) <= instant(row.get('updated_at'))
        except ValueError:
            chronology_ok = False
        if r is None or not chronology_ok:
            why = 'invalid_result_or_time'
        if why:
            excluded[why] = excluded.get(why,0)+1
            continue
        winner = names[r[0]]
        for name in names:
            key = (league,name)
            p = players.setdefault(key, {'league':league,'source_name':name,'matches':0,'wins':0,'opponents':set()})
            p['matches'] += 1
            p['wins'] += name == winner
            p['opponents'].add(names[1] if name == names[0] else names[0])
        a,b = sorted(names)
        p = pairs.setdefault((league,a,b), {'league':league,'a':a,'b':b,'matches':0,'a_wins':0,'fixture_ids':[]})
        p['matches'] += 1
        p['a_wins'] += winner == a
        p['fixture_ids'].append(ident)
    for p in players.values():
        p['opponents'] = sorted(p['opponents'])
        p['descriptive_win_fraction'] = p['wins']/p['matches']
    for p in pairs.values():
        p['fixture_ids'].sort()
    return {'schema':'research.source_name_history.v1','date':archive.get('date'),
            'identity_verified':False,'causal_training_eligible':False,
            'source_rows':len(seen),'excluded':excluded,
            'players':[players[k] for k in sorted(players)],
            'pairs':[pairs[k] for k in sorted(pairs)]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('archive',type=Path)
    p.add_argument('output',type=Path)
    args = p.parse_args()
    raw=args.archive.read_bytes()
    report=build(json.loads(raw))
    report['source_sha256']=hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__':
    main()
