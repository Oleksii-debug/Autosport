from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture


def test_late_matching_decision_cannot_retroactively_authorize_campaign_admission():
    with tempfile.TemporaryDirectory() as tmp:
        fixture = AdmissionFixture(Path(tmp), seed_execution_decision=False)

        # Reproduce the formerly accepted causal inversion exactly: the PAPER run
        # and materialized ticket already exist, then a byte-correct matching
        # economic DecisionRecord is appended afterwards.
        fixture.append_execution_decision()
        decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
        before = decision_ledger.verified_records()
        assert len(before) == 1

        coordinator = fixture.coordinator()
        with pytest.raises(
            PaperCampaignAdmissionError,
            match="pre-execution decision-origin",
        ):
            fixture.admit(coordinator)

        # Failing the causal-order proof must not mint a campaign economic
        # decision after the fact.
        after = decision_ledger.verified_records()
        assert after == before
