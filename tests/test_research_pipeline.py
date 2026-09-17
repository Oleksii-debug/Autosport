import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.decision_ledger import ECONOMIC_DECISION_KIND, JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.research_pipeline import (
    DeterministicResearchCritic,
    ResearchDecisionAlreadyCommitted,
    ResearchDecisionPipeline,
    ResearchDecisionReconciliationRequired,
    ResearchDecisionPolicy,
    ResearchEvidence,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


A = "match-1|winner|A"
B = "match-1|winner|B"
SNAPSHOT = "a" * 64
EVIDENCE_HASH = "b" * 64


class _PreWriteFailingEconomicLedger(JsonlDecisionLedger):
    def append_economic(self, record, contract):
        raise RuntimeError("injected pre-write economic ledger failure")


class _PostWriteThenRaiseEconomicLedger(JsonlDecisionLedger):
    def append_economic(self, record, contract):
        super().append_economic(record, contract)
        raise RuntimeError("injected post-write economic ledger ambiguity")


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
        market_quotes=None,
        decision_ledger=None,
        material_action_id=None,
    ):
        book = book or self._book()
        candidate = candidate or self._candidate()
        forecast = forecast or self._forecast()
        evidence = [self._evidence()] if evidence is None else evidence
        ledger = decision_ledger or JsonlDecisionLedger(
            Path(tmp) / "research-decisions.jsonl"
        )
        pipeline = pipeline or ResearchDecisionPipeline()
        decision = pipeline.decide_and_open(
            book=book,
            candidate=candidate,
            groups=self._groups(),
            forecasts={B: forecast},
            evidence=evidence,
            stake=stake,
            decision_ts=decision_ts,
            market_quotes=market_quotes,
            decision_ledger=ledger,
            replay_run_id="research-run",
            material_action_id=material_action_id,
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

    def _market_event(self) -> MarketEvent:
        return MarketEvent(
            event_id="match-1",
            market_id="winner",
            selection_id="B",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-13T10:00:00+00:00",
            source_id="provider",
            sequence=1,
            source_ts="2026-09-13T10:00:00+00:00",
            ingest_ts="2026-09-13T10:00:00+00:00",
        )

    def _economic_goal(self, **overrides) -> EconomicGoalContract:
        values = {
            "goal_id": "goal-research-pipeline",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.02"),
            "max_capital_at_risk_fraction": Decimal("0.20"),
            "max_concurrent_positions": 5,
            "max_quote_age_seconds": Decimal("5"),
        }
        values.update(overrides)
        return EconomicGoalContract(**values)

    def _goal_pipeline(self, goal: EconomicGoalContract) -> ResearchDecisionPipeline:
        return ResearchDecisionPipeline(
            risk_policy=PaperRiskPolicy(
                max_ticket_fraction=Decimal("1"),
                max_committed_fraction=Decimal("1"),
                minimum_cash_reserve_fraction=Decimal("0"),
                economic_goal=goal,
            )
        )

    def test_active_economic_goal_ignores_caller_stake_and_persists_provenance(self):
        goal = self._economic_goal()
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, decision = self._decide(
                tmp,
                candidate=self._candidate(probability="0.60"),
                forecast=self._forecast(probability="0.60"),
                pipeline=self._goal_pipeline(goal),
                stake="NaN",
                market_quotes=[self._market_event()],
            )

            self.assertTrue(decision.approved)
            self.assertIsNotNone(decision.portfolio_impact)
            self.assertEqual(decision.portfolio_impact.stake, Decimal("20.00"))
            ticket = book.tickets[decision.ticket_id]
            self.assertEqual(ticket.stake, Decimal("20.00"))
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].decision_kind, ECONOMIC_DECISION_KIND)
            self.assertEqual(records[0].payload["stake"], "20.00")
            self.assertEqual(records[0].payload["stake_source"], "economic-goal-derived")
            rebound = JsonlDecisionLedger(ledger.path).verified_economic_decision(
                records[0].decision_id,
                goal,
            )
            self.assertEqual(rebound, records[0])

    def test_active_economic_goal_exhaustion_records_zero_without_ticket(self):
        goal = self._economic_goal(max_stake_fraction=Decimal("0"))
        book = self._book()
        before = set(book.tickets)
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, decision = self._decide(
                tmp,
                book=book,
                candidate=self._candidate(probability="0.60"),
                forecast=self._forecast(probability="0.60"),
                pipeline=self._goal_pipeline(goal),
                stake="999999999999999999999",
                market_quotes=[self._market_event()],
            )

            self.assertFalse(decision.approved)
            self.assertIsNone(decision.portfolio_impact)
            self.assertEqual(set(book.tickets), before)
            self.assertTrue(any("ZERO stake" in reason for reason in decision.reasons))
            record = ledger.verified_records()[0]
            self.assertEqual(record.decision_kind, ECONOMIC_DECISION_KIND)
            self.assertEqual(record.payload["stake"], "0")
            self.assertEqual(record.payload["stake_source"], "economic-goal-derived")
            JsonlDecisionLedger(ledger.path).verified_economic_decision(
                record.decision_id,
                goal,
            )

    def test_economic_ledger_prewrite_failure_rolls_back_exact_paper_state(self):
        goal = self._economic_goal()
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book()
            before_balance = book.balance
            before_tickets = dict(book.tickets)
            before_lifecycle = list(book._lifecycle)
            ledger = _PreWriteFailingEconomicLedger(
                Path(tmp) / "research-decisions.jsonl"
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "injected pre-write economic ledger failure",
            ):
                self._decide(
                    tmp,
                    book=book,
                    candidate=self._candidate(probability="0.60"),
                    forecast=self._forecast(probability="0.60"),
                    pipeline=self._goal_pipeline(goal),
                    stake="NaN",
                    market_quotes=[self._market_event()],
                    decision_ledger=ledger,
                    material_action_id="research-action-prewrite",
                )

            self.assertEqual(book.balance, before_balance)
            self.assertEqual(book.tickets, before_tickets)
            self.assertEqual(book._lifecycle, before_lifecycle)
            self.assertFalse(ledger.path.exists())

    def test_economic_ledger_postwrite_ambiguity_keeps_exact_durable_ticket(self):
        goal = self._economic_goal()
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book()
            before_ticket_ids = set(book.tickets)
            ledger = _PostWriteThenRaiseEconomicLedger(
                Path(tmp) / "research-decisions.jsonl"
            )
            book, ledger, decision = self._decide(
                tmp,
                book=book,
                candidate=self._candidate(probability="0.60"),
                forecast=self._forecast(probability="0.60"),
                pipeline=self._goal_pipeline(goal),
                stake="NaN",
                market_quotes=[self._market_event()],
                decision_ledger=ledger,
                material_action_id="research-action-postwrite",
            )

            self.assertTrue(decision.approved)
            self.assertEqual(len(set(book.tickets) - before_ticket_ids), 1)
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(
                records[0].payload["material_action_id"],
                "research-action-postwrite",
            )
            envelope = json.loads(ledger.path.read_text(encoding="utf-8"))
            self.assertEqual(decision.audit_sha256, envelope["sha256"])
            self.assertEqual(
                envelope["record"]["payload"]["ticket_id"],
                decision.ticket_id,
            )

    def test_economic_material_action_restart_is_idempotent(self):
        goal = self._economic_goal()
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "research-decisions.jsonl"
            book_path = Path(tmp) / "paper-book.json"
            ledger = JsonlDecisionLedger(ledger_path)
            book, _ledger, first = self._decide(
                tmp,
                candidate=self._candidate(probability="0.60"),
                forecast=self._forecast(probability="0.60"),
                pipeline=self._goal_pipeline(goal),
                stake="NaN",
                market_quotes=[self._market_event()],
                decision_ledger=ledger,
                material_action_id="research-action-restart",
            )
            self.assertTrue(first.approved)
            book.save(book_path)
            restarted_book = PaperBook.load(book_path)
            before_balance = restarted_book.balance
            before_ticket_ids = set(restarted_book.tickets)
            before_lifecycle = list(restarted_book._lifecycle)

            with self.assertRaises(ResearchDecisionAlreadyCommitted) as caught:
                self._decide(
                    tmp,
                    book=restarted_book,
                    candidate=self._candidate(probability="0.60"),
                    forecast=self._forecast(probability="0.60"),
                    pipeline=self._goal_pipeline(goal),
                    stake="NaN",
                    market_quotes=[self._market_event()],
                    decision_ledger=JsonlDecisionLedger(ledger_path),
                    material_action_id="research-action-restart",
                )

            self.assertEqual(
                caught.exception.material_action_id,
                "research-action-restart",
            )
            self.assertEqual(restarted_book.balance, before_balance)
            self.assertEqual(set(restarted_book.tickets), before_ticket_ids)
            self.assertEqual(restarted_book._lifecycle, before_lifecycle)
            self.assertEqual(
                JsonlDecisionLedger(ledger_path).verify_integrity(),
                1,
            )

    def test_economic_material_action_restart_rejects_changed_intent(self):
        goal = self._economic_goal()
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "research-decisions.jsonl"
            book_path = Path(tmp) / "paper-book.json"
            ledger = JsonlDecisionLedger(ledger_path)
            book, _ledger, first = self._decide(
                tmp,
                candidate=self._candidate(probability="0.60"),
                forecast=self._forecast(probability="0.60"),
                pipeline=self._goal_pipeline(goal),
                stake="NaN",
                market_quotes=[self._market_event()],
                decision_ledger=ledger,
                material_action_id="research-action-intent",
            )
            self.assertTrue(first.approved)
            book.save(book_path)
            restarted_book = PaperBook.load(book_path)
            before_balance = restarted_book.balance
            before_tickets = dict(restarted_book.tickets)
            before_lifecycle = list(restarted_book._lifecycle)

            with self.assertRaisesRegex(
                ResearchDecisionReconciliationRequired,
                "does not match current decision intent",
            ):
                self._decide(
                    tmp,
                    book=restarted_book,
                    candidate=self._candidate(probability="0.61"),
                    forecast=self._forecast(probability="0.61"),
                    pipeline=self._goal_pipeline(goal),
                    stake="NaN",
                    market_quotes=[self._market_event()],
                    decision_ledger=JsonlDecisionLedger(ledger_path),
                    material_action_id="research-action-intent",
                )

            self.assertEqual(restarted_book.balance, before_balance)
            self.assertEqual(restarted_book.tickets, before_tickets)
            self.assertEqual(restarted_book._lifecycle, before_lifecycle)
            self.assertEqual(
                JsonlDecisionLedger(ledger_path).verify_integrity(),
                1,
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
