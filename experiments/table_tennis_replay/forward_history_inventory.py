"""Private, source-label opponent inventory and prior archive drift audit."""
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import json
from pathlib import Path

from canonical_history_bridge import validated_rows
from forward_residual_trial import ACTIONS, MODELS, RESULTS, load_capture, sha, write
from h2h_exploratory import instant
from market_anchor_screen import LEAGUES, POLICIES, action, history_state, probabilities
from market_residual_calibration import forecast
from autosport.json_integrity import strict_json_loads


def csv_rows(path, headers, rows):
    with path.open('x', encoding='utf-8', newline='') as stream:
        out = csv.writer(stream)
        out.writerow(headers)
        # A source label is untrusted data even when opened in a spreadsheet.
        out.writerows(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v
                       for v in row] for row in rows)


def inventory(root, previous, forecast_roots):
    src, rec = load_capture(root, 'history', 'results', RESULTS)
    cutoff = instant(rec['received_at'])
    current = {r['fixture_id']: r for r in src['results']}
    if len(current) != len(src['results']):
        raise ValueError('duplicate current source IDs')
    old_raw = previous.read_bytes()
    previous_rows = strict_json_loads(old_raw.decode('utf-8'))['results']
    old = {r['fixture_id']: r for r in previous_rows}
    if len(old) != len(previous_rows):
        raise ValueError('duplicate previous source IDs')
    compared = ('finished_at', 'home', 'away', 'league', 'final_score', 'status', 'periods')
    shared = current.keys() & old.keys()
    drift = {k: sorted(i for i in shared if current[i].get(k) != old[i].get(k)) for k in compared}
    quarantine = set().union(*map(set, drift.values()))
    admitted, excluded = validated_rows(src)
    valid = [(r, w) for r, w in admitted if r['league'] in LEAGUES
             and instant(r['updated_at']) < cutoff and r['fixture_id'] not in quarantine]
    players, pairs = defaultdict(Counter), defaultdict(Counter)
    opponents, last = defaultdict(set), {}
    for r, w in valid:
        league, a, b = (r[k] for k in ('league', 'home', 'away'))
        x, y = sorted((a, b))
        pair = (league, x, y)
        who = (a, b)[w]
        pairs[pair][who] += 1
        for side, other in ((a, b), (b, a)):
            key = (league, side)
            players[key]['matches'] += 1
            players[key]['wins'] += side == who
            opponents[key].add(other)
        last[pair] = max(last.get(pair, r['finished_at']), r['finished_at'], key=instant)
    if sum(sum(c.values()) for c in pairs.values()) != len(valid):
        raise ValueError('pair accounting mismatch')
    if sum(c['matches'] for c in players.values()) != 2 * len(valid):
        raise ValueError('player accounting mismatch')
    csv_rows(root / 'players.csv', ('league', 'source_label', 'matches', 'wins', 'distinct_opponents'),
             ((*key, c['matches'], c['wins'], len(opponents[key])) for key, c in sorted(players.items())))
    csv_rows(root / 'pairs.csv', ('league', 'label_a', 'label_b', 'meetings', 'a_wins', 'b_wins', 'last_finish_utc'),
             ((*key, sum(c.values()), c[key[1]], c[key[2]], last[key]) for key, c in sorted(pairs.items())))
    by_league = {}
    for league in sorted(LEAGUES):
        n = sum(k[0] == league for k in players)
        counts = [sum(c.values()) for k, c in pairs.items() if k[0] == league]
        by_league[league] = {'labels': n, 'observed_pairs': len(counts), 'matches': sum(counts),
                             'repeated_pairs': sum(v > 1 for v in counts), 'possible_pairs': n * (n-1) // 2}

    state = history_state(valid, cutoff, exclude_id=None)
    changes = []
    for cohort in forecast_roots:
        frozen_raw = (cohort / 'forecasts.json').read_bytes()
        f = json.loads(frozen_raw)
        if f['history_sha256'] != rec['sha256'] or f['models'] != MODELS:
            raise ValueError('different history or models')
        models = {}
        for name, digest in MODELS.items():
            raw = (cohort / f'{name}-model.json').read_bytes()
            if sha(raw) != digest:
                raise ValueError('different model bytes')
            models[name] = json.loads(raw)
        changed = Counter()
        deltas = Counter()
        for row in f['rows']:
            p = probabilities(row, row['market'], state)
            new = {k: action(p[k], tuple(Decimal(v) for v in row['odds'])) for k in POLICIES}
            for name, model in models.items():
                alt = forecast({**row, **p}, model)
                new[f'{name}_mean'], new[f'{name}_guard'] = alt['calibrated_action'], alt['bootstrap_guarded_action']
            for key in ACTIONS:
                changed[key] += new[key] != row['actions'][key]
            for key in POLICIES:
                deltas[key] = max(deltas[key], abs(p[key] - row[key]))
        changes.append({'forecast_sha256': sha(frozen_raw), 'changed_actions': dict(changed),
                        'max_anchor_probability_difference': dict(deltas)})
    result = {'schema': 'research.forward_history_inventory.v1', 'source_sha256': rec['sha256'],
              'source_available_at': rec['received_at'], 'previous_archive_sha256': sha(old_raw),
              'current_raw_rows': len(current), 'shared_ids': len(shared), 'new_ids': len(current.keys()-old.keys()),
              'changed_ids_by_field': drift, 'quarantined_ids': sorted(quarantine),
              'valid_target_league_matches_after_quarantine': len(valid), 'result_exclusions': excluded,
              'labels': len(players), 'pairs': len(pairs), 'repeated_pairs': sum(sum(c.values()) > 1 for c in pairs.values()),
              'leagues': by_league, 'post_freeze_quarantine_sensitivity': changes,
              'frozen_forecasts_changed': False, 'identity_or_club_verified': False}
    write(root, 'history-inventory.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('previous', type=Path)
    p.add_argument('cohorts', nargs='+', type=Path)
    args = p.parse_args()
    result = inventory(args.root, args.previous, args.cohorts)
    print({k: result[k] for k in ('labels', 'pairs', 'repeated_pairs', 'valid_target_league_matches_after_quarantine')})
