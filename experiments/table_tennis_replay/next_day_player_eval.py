"""Retrospective two-day player and head-to-head comparison, not a betting model.

Dates are UTC archive dates. Historical acquisition and test decision times are
unknown, so this is not an availability-qualified or prospective evaluation.
"""
import argparse
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import random

from canonical_history_bridge import validated_rows
from h2h_exploratory import instant


def _scores(rows, key):
    if not rows:
        return None
    n = len(rows)
    return {'brier': sum((r[key] - r['outcome']) ** 2 for r in rows) / n,
            'log_loss': -sum(math.log(r[key] if r['outcome'] else 1 - r[key]) for r in rows) / n}


def _paired_interval(rows, key, reference, *, repeats=1000):
    # Resample whole league/player-pair clusters, not individual matches.
    groups = defaultdict(list)
    for r in rows:
        groups[(r['league'], *sorted((r['home'], r['away'])))].append(
            (r[key] - r['outcome']) ** 2 - (r[reference] - r['outcome']) ** 2)
    if len(groups) < 2:
        return None
    aggregates = [(sum(v), len(v)) for _, v in sorted(groups.items())]
    rng = random.Random(20261029)
    samples = []
    for _ in range(repeats):
        sample = [rng.choice(aggregates) for _ in aggregates]
        samples.append(sum(x[0] for x in sample) / sum(x[1] for x in sample))
    samples.sort()
    return {'low_2_5_pct': samples[int(.025 * (repeats - 1))],
            'high_97_5_pct': samples[int(.975 * (repeats - 1))],
            'clusters': len(groups), 'resamples': repeats,
            'meaning': 'challenger Brier minus reference Brier; negative favors challenger'}


def evaluate(training, test):
    train_date = date.fromisoformat(training.get('date', ''))
    test_date = date.fromisoformat(test.get('date', ''))
    if test_date != train_date + timedelta(days=1):
        raise ValueError('test date must be the next UTC date')
    train_valid, train_excluded = validated_rows(training)
    test_valid, test_excluded = validated_rows(test)
    train_ids = {row['fixture_id'] for row in training['results']}
    if train_ids.intersection(row['fixture_id'] for row in test['results']):
        raise ValueError('fixture ID overlaps training and test')
    players = defaultdict(lambda: [0, 0])
    pairs = defaultdict(lambda: [0, 0])
    train_used = 0
    for row, winner in train_valid:
        # No record updated on the test date contributes to fitting.
        if (instant(row['finished_at']).date() != train_date or
                instant(row['updated_at']).date() != train_date):
            continue
        league, home, away = row['league'], row['home'], row['away']
        for index, name in enumerate((home, away)):
            stats = players[(league, name)]
            stats[0] += 1
            stats[1] += index == winner
        a, b = sorted((home, away))
        stats = pairs[(league, a, b)]
        stats[0] += 1
        stats[1] += (home if winner == 0 else away) == a
        train_used += 1
    rows = []
    for row, winner in test_valid:
        if instant(row['finished_at']).date() != test_date:
            continue
        league, home, away = row['league'], row['home'], row['away']
        hn, hw = players.get((league, home), (0, 0))
        an, aw = players.get((league, away), (0, 0))
        hp, ap = (hw + 1) / (hn + 2), (aw + 1) / (an + 2)
        # Bradley-Terry style difference of logits from smoothed win fractions.
        odds_ratio = (hp / (1 - hp)) / (ap / (1 - ap))
        strength = odds_ratio / (1 + odds_ratio)
        a, b = sorted((home, away))
        count, a_wins = pairs.get((league, a, b), (0, 0))
        home_wins = a_wins if home == a else count - a_wins
        h2h = (home_wins + 5 * strength) / (count + 5)
        rows.append({'fixture_id': row['fixture_id'], 'league': league,
                     'home': home, 'away': away, 'outcome': int(winner == 0),
                     'home_prior_matches': hn, 'away_prior_matches': an,
                     'prior_meetings': count, 'prior_home_wins': home_wins,
                     'neutral': .5, 'strength': strength, 'strength_h2h': h2h})
    def subset(items):
        return {'matches': len(items), 'neutral': _scores(items, 'neutral'),
                'strength': _scores(items, 'strength'),
                'strength_h2h': _scores(items, 'strength_h2h'),
                'strength_vs_neutral_brier_interval': _paired_interval(items, 'strength', 'neutral'),
                'h2h_vs_strength_brier_interval': _paired_interval(items, 'strength_h2h', 'strength')}
    return {'schema': 'research.next_day_player_eval.v1',
            'train_date': str(train_date), 'test_date': str(test_date),
            'train_source_rows': len(training['results']), 'train_valid': len(train_valid),
            'train_used_before_midnight': train_used, 'train_excluded': train_excluded,
            'test_source_rows': len(test['results']), 'test_valid': len(test_valid),
            'test_excluded': test_excluded, 'all': subset(rows),
            'both_players_known': subset([r for r in rows if r['home_prior_matches'] and r['away_prior_matches']]),
            'pair_known': subset([r for r in rows if r['prior_meetings']]),
            'identity_verified': False, 'availability_verified': False,
            'decision_times_verified': False, 'independent_forward_holdout': False,
            'market_baseline_available': False, 'profitability_proven': False,
            'note': 'Descriptive date split; exact source names are proxies. No test outcomes update the fit.',
            'rows': rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('training', type=Path)
    p.add_argument('test', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    train_raw, test_raw = args.training.read_bytes(), args.test.read_bytes()
    report = evaluate(json.loads(train_raw), json.loads(test_raw))
    report['train_sha256'] = hashlib.sha256(train_raw).hexdigest()
    report['test_sha256'] = hashlib.sha256(test_raw).hexdigest()
    with args.output.open('x', encoding='utf8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')


if __name__ == '__main__':
    main()
