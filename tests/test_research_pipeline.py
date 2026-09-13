import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.research_pipeline import (
    DeterministicResearchCritic,
    ResearchDecisionPipeline,
    ResearchDecisionPolicy,
    ResearchEvidence,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


A = "match-1|winner|A"
B = "match-1|winner|B"
SNAPSHOT = "a" * 64
EVIDENCE_HASH = "b" * 64


class ResearchDecisionPipelineTests(unittest.TestCase):
    def _book(self) -> PaperBook:
        book = PaperBook("1000")
        book.open_ticket(
            [TicketLeg("match-1", "winner", "A", Decimal("2.00"))],
            "10",
            reason="existing A exposure",
            placed_at="2026-09-13T10:00:00+00:00",
        )
        return book

    def _groups(self):
        return [
            ScenarioGroup(
                "match-1-winner",
                (
                    ScenarioOutcome(A, Decimal("0.50")),
                    ScenarioOutcome(B, Decimal("0.50")),
                ),
            )
        ]

    def _candidate(self, probability: str = "0.50", odds: str = "2.00"):
        p = Decimal(probability)
        o = Decimal(odds)
        leg = CandidateLeg(B, "match-1", o, p)
        return ParlayCandidate((leg,), o, p, p * o - Decimal("1"))

    def _evidence(
        self,
        *,
        content_hash: str = EVIDENCE_HASH,
        odds: str = "2.00",
        available_at: str = "2026-09-13T10:00:01+00:00",
        quality_flags=(),
        evidence_id: str = "evidence-b-1",
    ):
        return ResearchEvidence(
            evidence_id=evidence_id,
            quote_key=B,
            source_id="provider",
            observed_at="2026-09-13T10:00:00+00:00",
            available_at=available_at,
            decimal_odds=Decimal(odds),
            content_sha256=content_hash,
            quality_flags=tuple(quality_flags),
            market_snapshot_hash=SNAPSHOT,
        )

    def _forecast(
        self,
        *,
        probability: str = "0.50",
        evidence_hashes=(EVIDENCE_HASH,),
        input_cutoff: str = "2026-09-13T10:00:01+00:00",
        generated_at: str = "2026-09-13T10:00:02+00:00",
        uncertainty: str = "0.10",
    ):
        return ForecastRecord(
            quote_key=B,
            probability=Decimal(probability),
            model_id="tt-model",
            model_version="1.0.0",
            strategy_version="research-v1",
            model_training_cutoff_ts="2026-09-13T09:00:00+00:00",
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal(uncertainty),
            evidence_hashes=tuple(evidence_hashes),
            market_snapshot_hash=SNAPSHOT,
            provenance={"source": "typed-test"},
        )

    def _decide(
        self,
        tmp: str,
        *,
        book=None,
        candidate=None,
        forecast=None,
        evidence=None,
        pipeline=None,
        stake="10",
        decision_ts="2026-09-13T10:00:03+00:00",
    ):
        book = book or self._book()
        candidate = candidate or self._candidate()
        forecast = forecast or self._forecast()
        evidence = [self._evidence()] if evidence is None else evidence
        ledger = JsonlDecisionLedger(Path(tmp) / "research-decisions.jsonl")
        pipeline = pipeline or ResearchDecisionPipeline()
        decision = pipeline.decide_and_open(
            book=book,
            candidate=candidate,
            groups=self._groups(),
            forecasts={B: forecast},
            evidence=evidence,
            stake=stake,
            decision_ts=decision_ts,
            decision_ledger=ledger,
            replay_run_id="research-run",
        )
        return book, ledger, decision

    def test_happy_path_uses_exact_portfolio_hedge_and_opens_paper_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, decision = self._decide(tmp)
            self.assertTrue(decision.approved)
            self.assertIsNotNone(decision.ticket_id)
            self.assertTrue(decision.critic.approved)
            self.assertTrue(decision.risk.allowed)
            self.assertTrue(decision.portfolio_impact.worst_case_change_proven)
            self.assertEqual(
                decision.portfolio_impact.ranking_risk_truth,
                "exact-worst-case-change",
            )
            self.assertEqual(
                decision.portfolio_impact.ranking_risk_change,
                Decimal("10"),
            )
            self.assertEqual(len(book.tickets), 2)

            lines = ledger.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            envelope = json.loads(lines[0])
            self.assertEqual(envelope["record"]["action"], "OPEN_PAPER_RESEARCH_TICKET")
            self.assertFalse(envelope["record"]["payload"]["real_money_execution"])
            self.assertEqual(
                envelope["record"]["payload"]["portfolio"]["ranking_risk_truth"],
                "exact-worst-case-change",
            )
            self.assertEqual(decision.audit_sha256, envelope["sha256"])

    def test_stale_source_evidence_is_rejected_without_book_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book()
            before = set(book.tickets)
            book, ledger, decision = self._decide(
                tmp,
                book=book,
                evidence=[self._evidence(quality_flags=("STALE_SOURCE",))],
            )
            self.assertFalse(decision.approved)
            self.assertEqual(set(book.tickets), before)
            self.assertTrue(
                any("STALE_SOURCE" in reason for reason in decision.reasons)
            )
            envelope = json.loads(ledger.path.read_text(encoding="utf-8"))
            self.assertEqual(
                envelope["record"]["action"], "REJECT_PAPER_RESEARCH_CANDIDATE"
            )

    def test_future_forecast_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, decision = self._decide(
                tmp,
                forecast=self._forecast(
                    generated_at="2026-09-13T10:00:04+00:00"
                ),
            )
            self.assertFalse(decision.approved)
            self.assertTrue(
                any("generated after decision time" in reason for reason in decision.reasons)
            )

    def test_candidate_probability_must_equal_forecast_probability(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, decision = self._decide(
                tmp,
                candidate=self._candidate(probability="0.55"),
                forecast=self._forecast(probability="0.50"),
            )
            self.assertFalse(decision.approved)
            self.assertTrue(
                any("probability does not match" in reason for reason in decision.reasons)
            )

    def test_forecast_must_link_latest_evidence_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, decision = self._decide(
                tmp,
                forecast=self._forecast(evidence_hashes=("c" * 64,)),
            )
            self.assertFalse(decision.approved)
            self.assertTrue(
                any("evidence hash" in reason for reason in decision.reasons)
            )

    def test_latest_evidence_after_forecast_cutoff_rejects_stale_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = self._evidence()
            newer = self._evidence(
                content_hash="d" * 64,
                odds="2.10",
                available_at="2026-09-13T10:00:02+00:00",
                evidence_id="evidence-b-2",
            )
            _book, _ledger, decision = self._decide(
                tmp,
                evidence=[older, newer],
                decision_ts="2026-09-13T10:00:03+00:00",
            )
            self.assertFalse(decision.approved)
            self.assertTrue(
                any(
                    "does not cover latest available evidence" in reason
                    for reason in decision.reasons
                )
            )

    def test_policy_can_require_exact_worst_case_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            optimizer = PortfolioAwareCandidateOptimizer(
                scenario_engine=ScenarioSearchEngine(
                    exact_state_limit=1,
                    branch_node_limit=1,
                    sample_count=10,
                    seed=7,
                )
            )
            critic = DeterministicResearchCritic(
                ResearchDecisionPolicy(require_worst_case_proof=True)
            )
            pipeline = ResearchDecisionPipeline(
                optimizer=optimizer,
                critic=critic,
            )
            _book, _ledger, decision = self._decide(tmp, pipeline=pipeline)
            self.assertFalse(decision.approved)
            self.assertFalse(decision.portfolio_impact.worst_case_change_proven)
            self.assertTrue(
                any("not proven exact" in reason for reason in decision.reasons)
            )

    def test_risk_policy_can_reject_otherwise_valid_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book()
            before = set(book.tickets)
            book, _ledger, decision = self._decide(
                tmp,
                book=book,
                stake="50",
            )
            self.assertFalse(decision.approved)
            self.assertEqual(set(book.tickets), before)
            self.assertTrue(any(reason.startswith("risk policy:") for reason in decision.reasons))


if __name__ == "__main__":
    unittest.main()
