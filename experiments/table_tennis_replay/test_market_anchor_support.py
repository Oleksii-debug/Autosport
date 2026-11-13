from copy import deepcopy
from decimal import Decimal

from market_anchor_support import audit, support
from test_market_anchor_memory import population


def test_opportunities_and_tickets_are_distinct():
    report, source = population()
    for row in report['rows'][1:40]:
        row['elo_anchor'] = .5
    out = audit(report, source, 'a' * 64)
    prior = out['days'][1]['support_at_decision']['elo_anchor']
    assert prior['opportunities'] == 40
    assert prior['hypothetical_tickets'] == 1
    assert prior['unordered_pairs'] == prior['wager_days'] == 1
    assert prior['positive_result_depends_on_largest_win']
    assert Decimal(prior['pnl_without_largest_positive_ticket']) == 0


def test_current_day_outcomes_cannot_change_decision_support():
    report, source = population()
    before = audit(report, source, 'a' * 64)['days']
    for row in source['results'][40:]:
        row['periods'] = [{'name': 'Set ' + str(i + 1), 'score': '7-11'} for i in range(3)]
        row['final_score'] = '7-11,7-11,7-11'
    for row in report['rows'][40:]:
        row['outcome'] = 0
    assert audit(report, source, 'a' * 64)['days'] == before


def test_late_report_is_not_in_prior_support():
    report, source = population()
    for row in source['results'][:40]:
        row['updated_at'] = '2026-10-24T00:00:00Z'
    out = audit(report, source, 'a' * 64)
    assert out['days'][1]['support_at_decision']['elo_anchor']['opportunities'] == 0


def test_one_win_removal_preserves_other_losses_and_is_not_confidence_bound():
    report, _ = population()
    rows = deepcopy(report['rows'][:2])
    rows[1]['outcome'] = 0
    out = support(rows, 'elo_anchor')
    assert out['hypothetical_tickets'] == 2
    assert Decimal(out['net_pnl']) == Decimal('-.06')
    assert Decimal(out['pnl_without_largest_positive_ticket']) == Decimal('-1.01')
    assert not out['positive_result_depends_on_largest_win']
