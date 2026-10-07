from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.research_pipeline import (
    DeterministicResearchCritic,
    ResearchDecisionPipeline,
    ResearchEvidence,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


QUOTE_A = "match-1|winner|A"
QUOTE_B = "match-1|winner|B"
SNAPSHOT = "a" * 64
EVIDENCE_HASH = "b" * 64
OBSERVED = "2026-09-13T10:00:00+00:00"
AVAILABLE = "2026-09-13T10:00:01+00:00"
DECISION = "2026-09-13T10:00:03+00:00"


def _candidate() -> ParlayCandidate:
    leg = CandidateLeg(
        QUOTE_B,
        "match-1",
        Decimal("2.00"),
        Decimal("0.50"),
    )
    return ParlayCandidate(
        (leg,),
        Decimal("2.00"),
        Decimal("0.50"),
        Decimal("0"),
    )


def _forecast() -> ForecastRecord:
    return ForecastRecord(
        quote_key=QUOTE_B,
        probability=Decimal("0.50"),
        model_id="typed-model",
        model_version="1.0.0",
        strategy_version="research-v1",
        model_training_cutoff_ts="2026-09-13T09:00:00+00:00",
        input_cutoff_ts=AVAILABLE,
        generated_at="2026-09-13T10:00:02+00:00",
        uncertainty=Decimal("0.10"),
        evidence_hashes=(EVIDENCE_HASH,),
        market_snapshot_hash=SNAPSHOT,
        provenance={"source": "causal-use-boundary-test"},
    )


def _evidence(*, available_at: str = AVAILABLE) -> ResearchEvidence:
    return ResearchEvidence(
        evidence_id="evidence-b-1",
        quote_key=QUOTE_B,
        source_id="provider",
        observed_at=OBSERVED,
        available_at=available_at,
        decimal_odds=Decimal("2.00"),
        content_sha256=EVIDENCE_HASH,
        quality_flags=(),
        market_snapshot_hash=SNAPSHOT,
    )


def _groups() -> list[ScenarioGroup]:
    return [
        ScenarioGroup(
            "match-1-winner",
            (
                ScenarioOutcome(QUOTE_A, Decimal("0.50")),
                ScenarioOutcome(QUOTE_B, Decimal("0.50")),
            ),
        )
    ]


def test_pipeline_rejects_future_evidence_before_book_or_ledger_mutation() -> None:
    future = _evidence(available_at="2026-09-13T10:00:04+00:00")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "research-decisions.jsonl"
        book = PaperBook("1000")

        with pytest.raises(
            ValueError,
            match="research evidence was not available by decision time",
        ):
            ResearchDecisionPipeline().decide_and_open(
                book=book,
                candidate=_candidate(),
                groups=_groups(),
                forecasts={QUOTE_B: _forecast()},
                evidence=(future,),
                stake="10",
                decision_ts=DECISION,
                decision_ledger=JsonlDecisionLedger(path),
                replay_run_id="research-run",
            )

        assert book.tickets == {}
        assert not path.exists()


def test_critic_rejects_post_construction_inverted_evidence_chronology() -> None:
    evidence = _evidence()
    object.__setattr__(
        evidence,
        "available_at",
        "2026-09-13T09:59:59+00:00",
    )

    with pytest.raises(
        ValueError,
        match="research evidence cannot be available before it was observed",
    ):
        DeterministicResearchCritic().review(
            _candidate(),
            {QUOTE_B: _forecast()},
            (evidence,),
            decision_ts=DECISION,
        )


def test_pipeline_rejects_post_construction_naive_evidence_timestamp() -> None:
    evidence = _evidence()
    object.__setattr__(evidence, "available_at", "2026-09-13T10:00:01")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "research-decisions.jsonl"
        with pytest.raises(ValueError, match="timezone-aware"):
            ResearchDecisionPipeline().decide_and_open(
                book=PaperBook("1000"),
                candidate=_candidate(),
                groups=_groups(),
                forecasts={QUOTE_B: _forecast()},
                evidence=(evidence,),
                stake="10",
                decision_ts=DECISION,
                decision_ledger=JsonlDecisionLedger(path),
                replay_run_id="research-run",
            )
        assert not path.exists()
