"""Check chronological and orientation boundaries of the set-count challenger."""
from h2h_set_margin_probe import set_anchor, set_history
from set_anchor_replication import manifest


def test_selection_is_invariant_to_prices_outcomes_and_source_order():
    rows = [{'fixture_id': str(i), 'league': 'TT Cup',
             'finished_at': '2026-11-11T12:21:00Z', 'final_score': '3-0',
             'odds': 1.2, 'status': 'finished'} for i in range(1, 17)]
    selected = manifest({'results': rows})
    assert len(selected) == 1 and len(selected[0]['ids']) == 8
    assert manifest({'results': [{**r, 'final_score': '0-3', 'odds': 12,
                                  'status': 'unknown'} for r in reversed(rows)]}) == selected


def test_future_result_update_cannot_supply_third_meeting():
    cutoff = '2026-11-11T00:00:00Z'
    base = {'league': 'TT Cup', 'home': 'A', 'away': 'B',
            'finished_at': '2026-11-10T23:55:00Z', 'updated_at': '2026-11-10T23:56:00Z',
            'periods': [{'score': '11-8'}, {'score': '11-9'}, {'score': '11-7'}]}
    evidence = [(base, 0), (base, 0), ({**base, 'updated_at': cutoff}, 0)]
    target = {'league': 'TT Cup', 'home': 'A', 'away': 'B', 'market': .45}
    assert set_anchor(target, *set_history(evidence, cutoff)) == (.45, 6, 0)
    assert set_anchor(target, *set_history([*evidence, (base, 0)], cutoff))[0] > .45


def test_swapping_the_sides_preserves_the_set_model_probability():
    played = {'league': 'TT Cup', 'home': 'B', 'away': 'A',
              'finished_at': '2026-11-10T20:30:00Z', 'updated_at': '2026-11-10T20:31:00Z',
              'periods': [{'score': '11-9'}, {'score': '9-11'}, {'score': '9-11'}, {'score': '9-11'}]}
    state = set_history([(played, 1)] * 3, '2026-11-11T00:00:00Z')
    p, a, b = set_anchor({'league': 'TT Cup', 'home': 'A', 'away': 'B', 'market': .44}, *state)
    q, x, y = set_anchor({'league': 'TT Cup', 'home': 'B', 'away': 'A', 'market': .56}, *state)
    assert (a, b) == (y, x) == (9, 3)
    assert abs(p + q - 1) < 1e-14
