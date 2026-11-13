import pytest

from forward_prior_scope import build_scope
from test_forward_portfolio_journal import HEAD
from test_forward_portfolio_revisions import add, fixture, results
from test_delayed_portfolio_replay import later_result
from test_quote_delay_audit import stamp


def previous(tmp_path):
    session, journal, history = fixture(tmp_path)
    head, _, _ = add(session, journal, history, None, results(), 0)
    head, _, _ = add(session, journal, history, head,
                     results(updated=180) + [later_result("other", finished=90, updated=180)], 1)
    return session, journal, history, head


def scope(data, *, before=stamp(400), base=frozenset(), head=None):
    session, journal, history, actual = data
    return build_scope(session, journal, base, HEAD, history, head or actual, before=before)


def test_collects_all_scoped_ids_including_unselected_and_dated(tmp_path):
    value = scope(previous(tmp_path))
    assert value["ids"] == ["a", "b", "c", "other"]
    assert value["counts"]["prior_session_odds_ids"] == 3
    assert value["counts"]["dated_result_ids"] == 3
    assert value["counts"]["dated_receipts"] == 2
    assert value["complete_prior_universe"] is value["independent_holdout"] is False


def test_must_use_bound_base_set_and_latest_pinned_head(tmp_path):
    data = previous(tmp_path)
    with pytest.raises(ValueError, match="causal source selection"):
        scope(data, base={"different"})
    with pytest.raises(ValueError, match="externally pinned"):
        scope(data, head="b" * 64)


def test_evidence_at_or_after_cutoff_is_forbidden(tmp_path):
    data = previous(tmp_path)
    with pytest.raises(ValueError, match="strictly before"):
        scope(data, before=stamp(220))
    with pytest.raises(ValueError, match="strictly before"):
        scope(data, before=stamp(219))


@pytest.mark.parametrize("part", ["session", "dated_body", "dated_receipt", "dated_link"])
def test_changed_bytes_fail_closed(tmp_path, part):
    data = previous(tmp_path)
    session, _, history, _ = data
    path = (session / "0003.results.body.json" if part == "session" else
            history / ("0001." + part.removeprefix("dated_") + ".json"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        scope(data)


def test_incomplete_session_or_history_is_rejected(tmp_path):
    data = previous(tmp_path)
    (data[0] / "0003.results.receipt.json").unlink()
    with pytest.raises(ValueError):
        scope(data)
