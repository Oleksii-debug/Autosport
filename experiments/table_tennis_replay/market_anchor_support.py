"""Audit actual wager support behind a frozen shadow preference.

Descriptive only: this does not change the preregistered policy, fit thresholds,
promote a strategy, or treat opportunity counts as independent betting trials.
"""
import argparse
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from canonical_history_bridge import validated_rows
from h2h_exploratory import instant
from market_anchor_memory import hypothetical_return, replay
from market_anchor_screen import POLICIES, action


def support(rows, policy):
    tickets = [r for r in rows if action(r[policy], tuple(Decimal(v) for v in r['odds'])) is not None]
    returns = [hypothetical_return(r, policy) for r in tickets]
    total = sum(returns, Decimal(0))
    largest = max([Decimal(0), *returns])
    return {
        'opportunities': len(rows), 'hypothetical_tickets': len(tickets),
        'positive_ticket_returns': sum(v > 0 for v in returns),
        'negative_ticket_returns': sum(v < 0 for v in returns),
        'wager_days': len({r['day'] for r in tickets}),
        'unordered_pairs': len({(r['league'], *sorted((r['home'], r['away']))) for r in tickets}),
        'net_pnl': str(total), 'largest_positive_ticket': str(largest),
        'pnl_without_largest_positive_ticket': str(total - largest),
        'positive_result_depends_on_largest_win': total > 0 and total - largest <= 0,
    }


def audit(report, source, source_sha256):
    # Reuse validated causal replay so source identities/outcomes and cutoffs
    # cannot silently differ from the original adaptive experiment.
    memory_report, _ = replay(report, source, source_sha256)
    valid, _ = validated_rows(source)
    by_id = {r['fixture_id']: r for r, _ in valid}
    daily = []
    for day in memory_report['days']:
        cutoff = datetime.combine(datetime.fromisoformat(day['day']).date(), time.min, tzinfo=timezone.utc)
        prior = [r for r in report['rows'] if r['day'] < day['day']
                 and instant(by_id[r['fixture_id']]['updated_at']) < cutoff]
        prior.sort(key=lambda r: (instant(by_id[r['fixture_id']]['updated_at']), r['fixture_id']))
        window = prior[-memory_report['plan']['window']:]
        daily.append({'day': day['day'], 'selected_policy': day['policy'],
            'support_at_decision': {p: support(window, p) for p in POLICIES}})
    return {'schema': 'research.market_anchor_support.v1',
        'source_sha256': source_sha256, 'clock': 'SIMULATED_RETROSPECTIVE',
        'purpose': 'descriptive support audit; no threshold tuning or policy changes',
        'days': daily, 'whole_sample': {p: support(report['rows'], p) for p in POLICIES},
        'largest_win_removal_is_confidence_interval': False,
        'pairs_or_days_proven_independent': False, 'profitability_proven': False,
        'production_promotion': False, 'real_money_execution': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw, report_raw = args.source.read_bytes(), args.report.read_bytes()
    result = audit(json.loads(report_raw), json.loads(raw), hashlib.sha256(raw).hexdigest())
    result['input_report_sha256'] = hashlib.sha256(report_raw).hexdigest()
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2)
        handle.write('\n')
    print(json.dumps(result['whole_sample']))


if __name__ == '__main__':
    main()
