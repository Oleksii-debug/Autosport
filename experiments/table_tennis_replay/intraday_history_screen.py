"""Exploratory historical same-day refresh, with a common H2H window endpoint.

The input sample's outcomes were inspected earlier. This is not a fresh holdout
and must not tune the separately frozen prospective intraday cohort.
"""
import argparse
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from canonical_history_bridge import validated_rows
from h2h_exploratory import instant
from market_anchor_screen import POLICIES, history_state, probabilities, economics
from next_day_player_eval import _scores, _paired_interval

RULES = (*POLICIES, *(f'intraday_{p}' for p in POLICIES if p != 'market'))


def predict_pair(valid, row):
    cutoff = instant(row['kickoff'])
    midnight = datetime.combine(cutoff.date(), time.min, tzinfo=timezone.utc)
    # Filter the old arm's observations, but keep both 30-day windows anchored
    # at the SAME prediction time. Only the available source history differs.
    old = [(r, w) for r, w in valid if instant(r['finished_at']) < midnight
           and instant(r['updated_at']) < midnight]
    old_state = history_state(old, cutoff, exclude_id=row['fixture_id'])
    fresh_state = history_state(valid, cutoff, exclude_id=row['fixture_id'])
    base = probabilities(row, row['market'], old_state)
    fresh = probabilities(row, row['market'], fresh_state)
    return {**row, **base, **{'intraday_' + k: v for k, v in fresh.items() if k != 'market'}}


def evaluate(screen, source, source_sha256):
    if screen.get('source_sha256') != source_sha256:
        raise ValueError('different source archive')
    valid, excluded = validated_rows(source)
    by_id = {r['fixture_id']: (r, winner) for r, winner in valid}
    rows, seen = [], set()
    for row in screen['rows']:
        ident = row['fixture_id']
        if ident in seen or ident not in by_id:
            raise ValueError('duplicate or unknown event')
        original, winner = by_id[ident]
        if (any(row[k] != original[k] for k in ('league', 'home', 'away'))
                or row['outcome'] != int(winner == 0)
                or row['day'] != instant(row['kickoff']).date().isoformat()
                or not instant(row['kickoff']) < instant(original['finished_at'])):
            raise ValueError('source result or prediction chronology mismatch')
        prices = tuple(Decimal(v) for v in row['odds'])
        if len(prices) != 2 or any(not v.is_finite() or v <= 1 for v in prices):
            raise ValueError('invalid odds')
        reference = float(prices[1] / sum(prices))
        if abs(reference - row['market']) > 1e-14:
            raise ValueError('market probability differs from recorded prices')
        seen.add(ident)
        rows.append(predict_pair(valid, row))

    def summarize(items):
        return {p: {'scores': _scores(items, p), 'economics': economics(items, p)} for p in RULES}

    return {'schema': 'research.intraday_history_screen.v1', 'source_sha256': source_sha256,
        'sample': len(rows), 'rules': list(RULES), 'source_exclusions': excluded,
        'all': summarize(rows),
        'early_period': summarize([r for r in rows if r['day'] <= '2026-10-30']),
        'later_period': summarize([r for r in rows if r['day'] >= '2026-10-31']),
        'fresh_minus_old_pair_cluster_brier_intervals': {
            p: _paired_interval(rows, 'intraday_' + p, p) for p in POLICIES if p != 'market'},
        'comparative_window_endpoint': 'same actual prediction kickoff in both arms',
        'exploratory_previously_seen_outcomes': True, 'independent_forward_holdout': False,
        'historical_local_availability_verified': False, 'identity_verified': False,
        'executable_price_verified': False, 'profitability_proven': False,
        'production_promotion': False, 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('screen', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw, screen_raw = args.source.read_bytes(), args.screen.read_bytes()
    report = evaluate(json.loads(screen_raw), json.loads(raw), hashlib.sha256(raw).hexdigest())
    report['input_screen_sha256'] = hashlib.sha256(screen_raw).hexdigest()
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({p: {k: v for k, v in r['economics'].items() if k != 'daily'}
                      for p, r in report['all'].items()}, indent=2))


if __name__ == '__main__':
    main()
