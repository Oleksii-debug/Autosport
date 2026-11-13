"""Exploratory set-score signal on already examined, fixed historical cohorts."""
import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

from canonical_history_bridge import validated_rows
from early_set_economics import _set_skill, match_probability
from forward_residual_trial import sha, write
from h2h_exploratory import instant
from market_anchor_screen import LEAGUES, action, history_state, probabilities
from market_residual_calibration import fixed_action_economics
from next_day_player_eval import _paired_interval, _scores


def set_history(history, cutoff):
    """Count sets and matches for exact oriented source labels."""
    time = instant(cutoff)
    sets, matches = defaultdict(Counter), Counter()
    for q, _ in history:
        if (q['league'] not in LEAGUES or not time - timedelta(days=30) <= instant(q['finished_at'])
                <= instant(q['updated_at']) < time):
            continue
        a, b = q['home'], q['away']
        key = (q['league'], *sorted((a, b)))
        matches[key] += 1
        for game in q['periods']:
            h, v = map(int, game['score'].split('-'))
            sets[key][a if h > v else b] += 1
    return sets, matches


def set_anchor(row, sets, matches):
    """Shrink observed H2H set results toward 80 market-implied prior sets."""
    a, b = row['home'], row['away']
    key = (row['league'], *sorted((a, b)))
    p = row['market']
    if matches[key] >= 3:
        q = (80 * _set_skill(p) + sets[key][a]) / (80 + sets[key][a] + sets[key][b])
        p = match_probability(q)
    return p, sets[key][a], sets[key][b]


def probe(rows, history, cutoff):
    time = instant(cutoff)
    state = history_state(history, time, exclude_id=None)
    sets, matches = set_history(history, cutoff)
    output = []
    for r in rows:
        if instant(r['kickoff']) < time:
            raise ValueError('target before historical cutoff')
        baseline = probabilities(r, r['market'], state)
        if any(baseline[k] != r[k] for k in ('h2h_anchor', 'prior_meetings', 'prior_home_wins')):
            raise ValueError('cannot reproduce existing H2H control')
        p, home_sets, away_sets = set_anchor(r, sets, matches)
        odds = tuple(Decimal(v) for v in r['odds'])
        output.append({**r, 'h2h_set_anchor': p, 'prior_home_sets': home_sets, 'prior_away_sets': away_sets,
                       'actions': {k: action(r[k] if k != 'h2h_set_anchor' else p, odds)
                                   for k in ('market', 'h2h_anchor', 'h2h_set_anchor')}})
    keys = ('market', 'h2h_anchor', 'h2h_set_anchor')
    return {'rows': output, 'scores': {k: _scores(output, k) for k in keys},
            'economics': {k: fixed_action_economics(output, k) for k in keys},
            'set_vs_match_paired_brier': _paired_interval(output, 'h2h_set_anchor', 'h2h_anchor')}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output', type=Path)
    p.add_argument('old_history', type=Path)
    p.add_argument('day1', type=Path)
    p.add_argument('day2', type=Path)
    args = p.parse_args()
    old = args.old_history.read_bytes()
    new = (args.day1 / 'test-source.body.json').read_bytes()
    x, _ = validated_rows(json.loads(old)); y, _ = validated_rows(json.loads(new))
    overlap = {r['fixture_id'] for r, _ in x} & {r['fixture_id'] for r, _ in y}
    history = [[(r, w) for r, w in x if r['fixture_id'] not in ('204173406', '204188383')],
               [(r, w) for r, w in x+y if r['fixture_id'] not in overlap]]
    report = {'method': 'one exploratory variant; 80 market-implied prior sets; at least three prior meetings',
              'already_examined_cohorts': True, 'prospective': False, 'promoted': False, 'cohorts': [],
              'history_sha256': [sha(old), sha(new)]}
    for folder, h, date in zip((args.day1, args.day2), history, ('2026-11-09', '2026-11-10'), strict=True):
        raw = (folder / 'evaluation.json').read_bytes()
        result = probe(json.loads(raw)['rows'], h, date+'T00:00:00Z')
        report['cohorts'].append({'date': date, 'evaluation_sha256': sha(raw), **result})
    write(args.output.parent, args.output.name, report)
    for r in report['cohorts']:
        print(json.dumps({k: r[k] for k in ('date', 'scores', 'economics', 'set_vs_match_paired_brier')}))


if __name__ == '__main__':
    main()
