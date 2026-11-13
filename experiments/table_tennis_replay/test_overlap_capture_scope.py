import json

import pytest

from forward_portfolio_journal import _excluded_sha, freeze_proposal
from forward_scoped_capture import load_scope
from overlap_capture_scope import derive
from test_forward_portfolio_journal import HEAD, capture, clock, setup
from test_quote_delay_audit import match, stamp


def first(root, *, fixtures="abc", excluded=frozenset({"old"})):
    root.mkdir()
    session, journal = setup(root)
    scope = {"schema": "research.tt_forward_prior_scope.v1", "history_head": HEAD,
             "prior_as_of": stamp(-1), "ids": sorted(excluded), "id_set_sha256": _excluded_sha(excluded)}
    (root / "prior-scope.json").write_text(json.dumps(scope), encoding="utf-8")
    capture(session, 1, "odds", 0, [match(x, 0) for x in fixtures])
    capture(session, 1, "results", 2, [])
    freeze_proposal(session, journal, 1, excluded, HEAD, clock=clock(4, 5))
    return scope


def test_second_scope_adds_only_frozen_first_selection(tmp_path):
    a = tmp_path / "a"; scope = first(a)
    value = derive(a, HEAD, scope["id_set_sha256"], before=stamp(6))
    assert set(value["ids"]) == {"old", "a", "b"}
    assert value["first_proposal_selected_count"] == 2 and value["first_selected_new_ids"] == 2
    b = tmp_path / "b"; b.mkdir(); (b / "prior-scope.json").write_text(json.dumps(value))
    _, ids = load_scope(b, HEAD, value["id_set_sha256"])
    assert ids == {"old", "a", "b"}
    assert value["player_identity_verified"] is value["independent_holdout"] is False


def test_no_pair_is_not_replaced_or_invented(tmp_path):
    a = tmp_path / "a"; scope = first(a, fixtures="a")
    value = derive(a, HEAD, scope["id_set_sha256"], before=stamp(6))
    assert value["first_proposal_selected_count"] == 1 and set(value["ids"]) == {"old", "a"}


def test_second_scope_requires_strictly_earlier_first_proposal(tmp_path):
    a = tmp_path / "a"; scope = first(a)
    with pytest.raises(ValueError, match="strictly available"):
        derive(a, HEAD, scope["id_set_sha256"], before=stamp(5))


def test_changed_first_capture_or_base_scope_fails_closed(tmp_path):
    a = tmp_path / "a"; scope = first(a)
    with pytest.raises(ValueError, match="scope differs"):
        derive(a, HEAD, "f" * 64, before=stamp(6))
    path = a / "session/0001.odds.body.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt does not bind"):
        derive(a, HEAD, scope["id_set_sha256"], before=stamp(6))


def test_no_second_scope_after_first_session_moves_ahead(tmp_path):
    a = tmp_path / "a"; scope = first(a)
    capture(a / "session", 2, "odds", 40, [match("a", 40), match("b", 40)])
    with pytest.raises(ValueError, match="exactly one complete"):
        derive(a, HEAD, scope["id_set_sha256"], before=stamp(50))
