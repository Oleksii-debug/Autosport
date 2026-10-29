"""Compare the frozen next-day models with a conservative closing-price subset.

Historical prices are not verified executable fills. Capture receipts are
required, and missing responses remain missing rather than losing bets.
"""
import argparse
from collections import Counter
from datetime import datetime, time, timezone
import hashlib
import json
import math
from pathlib import Path

from capture_closing_subset import selected_ids
from h2h_exploratory import instant
from next_day_player_eval import _paired_interval, _scores


def market_probability(data, row, cutoff):
    if data.get('fixture_id') != row['fixture_id']:
        return None, 'fixture_mismatch'
    if data.get('kickoff_known') is not True or data.get('kickoff_estimated') is not False or data.get('pre_close_basis') != 'kickoff':
        return None, 'kickoff_unconfirmed'
    try:
        kickoff = instant(data.get('kickoff_utc'))
    except ValueError:
        return None, 'kickoff_unconfirmed'
    if kickoff <= cutoff:
        return None, 'kickoff_before_training_cutoff'
    sides = {}
    for selection in data.get('selections', []):
        if (str(selection.get('group_id')) != '920000' or
                selection.get('group_name') != 'To Win Match' or
                selection.get('line') != 'To Win' or selection.get('side') not in (row['home'], row['away'])):
            continue
        side = selection['side']
        quote = selection.get('pre_close')
        if not isinstance(quote, dict) or quote.get('suspended') is not False:
            continue
        try:
            timestamp = instant(quote.get('ts'))
        except ValueError:
            continue
        value = quote.get('odds_dec')
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 1 or timestamp <= cutoff or
                not 0 < (kickoff - timestamp).total_seconds() <= 600):
            continue
        market = str(selection.get('market_id') or '')
        if any(field in quote and quote[field] != selection.get(field)
               for field in ('side', 'line', 'market_id', 'group_id', 'sel_id')):
            continue
        if not market or side in sides:
            return None, 'ambiguous_market'
        sides[side] = (market, float(value))
    if set(sides) != {row['home'], row['away']}:
        return None, 'missing_fresh_pair'
    if sides[row['home']][0] != sides[row['away']][0]:
        return None, 'different_markets'
    home_inv, away_inv = 1 / sides[row['home']][1], 1 / sides[row['away']][1]
    return home_inv / (home_inv + away_inv), None


def evaluate(evaluation, directory):
    rows_by_id = {r['fixture_id']: r for r in evaluation['rows']}
    ids = selected_ids(evaluation)
    cutoff = datetime.combine(datetime.fromisoformat(evaluation['test_date']).date(), time.min, tzinfo=timezone.utc)
    reasons, rows = Counter(), []
    for ident in ids:
        path, receipt_path = directory / (ident + '.json'), directory / (ident + '.receipt.json')
        if not path.exists() and not receipt_path.exists():
            reasons['not_captured'] += 1
            continue
        if not path.is_file() or not receipt_path.is_file():
            reasons['incomplete_capture'] += 1
            continue
        raw = path.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding='utf8'))
        if receipt.get('sha256') != hashlib.sha256(raw).hexdigest():
            raise ValueError('capture receipt digest mismatch')
        market, reason = market_probability(json.loads(raw), rows_by_id[ident], cutoff)
        if reason:
            reasons[reason] += 1
            continue
        rows.append({**rows_by_id[ident], 'market': market})
    return {'schema': 'research.closing_baseline_eval.v1', 'selected': len(ids),
            'included': len(rows), 'excluded_or_missing': dict(reasons),
            'neutral': _scores(rows, 'neutral'), 'market': _scores(rows, 'market'),
            'strength': _scores(rows, 'strength'), 'strength_h2h': _scores(rows, 'strength_h2h'),
            'strength_vs_market_brier_interval': _paired_interval(rows, 'strength', 'market'),
            'h2h_vs_market_brier_interval': _paired_interval(rows, 'strength_h2h', 'market'),
            'availability_verified': False, 'executable_quotes_verified': False,
            'independent_forward_holdout': False, 'profitability_proven': False,
            'note': 'pre-close prices retrieved retrospectively; 10-minute age is not proof of quote freshness or concurrency',
            'rows': rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('evaluation', type=Path)
    p.add_argument('closing_directory', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    report = evaluate(json.loads(args.evaluation.read_text(encoding='utf8')), args.closing_directory)
    with args.output.open('x', encoding='utf8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2); f.write('\n')


if __name__ == '__main__':
    main()
