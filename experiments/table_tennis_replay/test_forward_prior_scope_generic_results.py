from forward_prior_scope import build_scope
from test_delayed_portfolio_replay import later_result, set_session_results
from test_forward_portfolio_journal import HEAD
from test_forward_portfolio_revisions import add, fixture
from test_quote_delay_audit import stamp


def test_unselected_id_from_generic_result_is_still_excluded(tmp_path):
    session, journal, history = fixture(tmp_path)
    set_session_results(session, 3, [later_result("generic-only", finished=60, updated=70)])
    head, _, _ = add(session, journal, history, None, [], 0)
    scope = build_scope(session, journal, frozenset(), HEAD, history, head, before=stamp(300))
    assert "generic-only" in scope["ids"]
    assert scope["counts"]["prior_session_result_ids"] == 1
