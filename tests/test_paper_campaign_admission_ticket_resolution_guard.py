from __future__ import annotations

import pytest

from autosport.paper import PaperBook
from autosport.paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from paper_campaign_admission_test_support import AdmissionFixture


def _state_bytes(fixture: AdmissionFixture):
    return (
        (fixture.workspace / "paper-campaign-admission.json").read_bytes(),
        (fixture.workspace / "decisions.jsonl").read_bytes(),
        (fixture.workspace / "agent-loop.json").read_bytes(),
        (fixture.workspace / "paper_book.json").read_bytes(),
    )


def test_canonical_durable_ticket_positive_control(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    receipt = fixture.admit(coordinator)

    assert receipt.ticket_id == fixture.execution_ticket_id


def test_execution_ticket_replacement_after_construction_never_executes_attacker(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _state_bytes(fixture)
    attacker_called = False

    def attacker_execution_ticket(_self, _binding):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker execution-ticket resolver must never execute")

    monkeypatch.setattr(
        PaperCampaignAdmissionCoordinator,
        "_execution_ticket",
        attacker_execution_ticket,
    )

    with pytest.raises(PaperCampaignAdmissionError, match="execution-ticket authority changed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert _state_bytes(fixture) == before


def test_ticket_bridge_replacement_after_construction_never_executes_attacker(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _state_bytes(fixture)
    attacker_called = False

    def attacker_ticket(_self, *args, **kwargs):
        del args, kwargs
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker ticket bridge must never execute")

    monkeypatch.setattr(PaperCampaignAdmissionCoordinator, "_ticket", attacker_ticket)

    with pytest.raises(PaperCampaignAdmissionError, match="ticket bridge changed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert _state_bytes(fixture) == before


def test_rebound_paperbook_loader_cannot_synthesize_missing_canonical_ticket(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    paper_path = fixture.workspace / "paper_book.json"
    forged_book = PaperBook.load(paper_path)

    # Remove the real canonical ticket. A rebound loader that returns the old in-memory
    # book would have authorized it before the executable ticket-resolution seal.
    PaperBook("100").save(paper_path)
    before = _state_bytes(fixture)
    attacker_called = False

    def attacker_load(cls, path):
        del cls, path
        nonlocal attacker_called
        attacker_called = True
        return forged_book

    monkeypatch.setattr(PaperBook, "load", classmethod(attacker_load))

    with pytest.raises(PaperCampaignAdmissionError, match="PaperBook loader changed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert _state_bytes(fixture) == before


def test_paperbook_path_replacement_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _state_bytes(fixture)

    coordinator.paper_book_path = fixture.workspace / "alternate-paper-book.json"

    with pytest.raises(PaperCampaignAdmissionError, match="PaperBook path changed"):
        fixture.admit(coordinator)

    assert _state_bytes(fixture) == before
