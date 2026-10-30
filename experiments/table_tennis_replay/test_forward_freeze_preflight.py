"""The research journal must not publish receipts for rejected clock times."""
import pytest

import forward_portfolio_journal as journal_module
from forward_portfolio_journal import freeze_action, freeze_proposal
from test_forward_portfolio_journal import HEAD, capture, clock, setup
from test_quote_delay_audit import match


@pytest.mark.parametrize("computed,reason", [(1, "before its source receipt"),
                                              (32, "more than 30 seconds")])
def test_rejected_proposal_time_writes_no_immutable_pair(tmp_path, computed, reason):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    with pytest.raises(ValueError, match=reason):
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(computed, computed + 1))
    assert not list(journal.iterdir())
    if computed == 1:  # Clock advances from before receipt to a valid instant.
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(4, 5))
        assert len(list(journal.iterdir())) == 2


@pytest.mark.parametrize("computed,reason", [(40, "before its source receipt"),
                                              (72, "more than 30 seconds")])
def test_rejected_action_time_writes_no_immutable_pair(tmp_path, computed, reason):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(4, 5))
    capture(session, 2, "odds", 40, [match(x, 40) for x in "ab"])
    before = {p.name for p in journal.iterdir()}
    with pytest.raises(ValueError, match=reason):
        freeze_action(session, journal, 1, frozenset(), HEAD, clock=clock(computed, computed + 1))
    assert {p.name for p in journal.iterdir()} == before
    if computed == 40:
        freeze_action(session, journal, 1, frozenset(), HEAD, clock=clock(42, 43))
        assert len(list(journal.iterdir())) == 4


def test_partial_io_failure_is_preserved_and_blocks_retry(tmp_path, monkeypatch):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    write = journal_module._write_exclusive
    def fail_receipt(path, content):
        if path.name.endswith(".receipt.json"):
            raise OSError("synthetic receipt failure")
        write(path, content)
    monkeypatch.setattr(journal_module, "_write_exclusive", fail_receipt)
    with pytest.raises(OSError, match="synthetic receipt failure"):
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(4, 5))
    assert (journal / "proposal-0001.body.json").is_file()
    assert not (journal / "proposal-0001.receipt.json").exists()
    with pytest.raises(ValueError, match="record already exists"):
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(6, 7))
