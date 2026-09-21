from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.live_decision_disposition import (
    Disposition,
    EvidenceReference,
    LiveDecisionDisposition,
    LiveDecisionDispositionError,
    MarketDecisionIdentity,
    PredicateEvidence,
    PredicateTruth,
    ProductPolicyAuthorityBinding,
    ReevaluationTrigger,
    bind_product_policy_authority,
)
from autosport.live_decision_reevaluation import (
    persist_live_decision_disposition,
    resolve_live_decision_disposition,
    verify_reevaluation_transition,
)
from autosport.portfolio_plan import EvidenceTruth, PortfolioAction, PortfolioPlan
from autosport.risk import PaperRiskPolicy


T0 = "2026-09-21T08:40:00.000000Z"
T1 = "2026-09-21T08:40:01.000000Z"
T2 = "2026-09-21T08:40:02.000000Z"
T3 = "2026-09-21T08:40:03.000000Z"
T5 = "2026-09-21T08:40:05.000000Z"
T6 = "2026-09-21T08:40:06.000000Z"
T10 = "2026-09-21T08:40:10.000000Z"
T20 = "2026-09-21T08:40:20.000000Z"


def _market(*, event_id: str = "event-1") -> MarketDecisionIdentity:
    return MarketDecisionIdentity(
        sport="football",
        event_id=event_id,
        market_id="match-winner",
        selection_id="home",
        side="BACK",
        line=None,
    )


def _predicate(
    truth: PredicateTruth,
    *,
    digest: str = "a" * 64,
    reason: str = "evidence-state",
) -> PredicateEvidence:
    return PredicateEvidence(
        predicate_id="quote_fresh",
        truth=truth,
        reason_code=reason,
        evidence=(
            EvidenceReference(
                authority_kind="quote",
                evidence_id="quote-evidence-1",
                evidence_sha256=digest,
            ),
        ),
    )


def _pre_policy(
    disposition: Disposition,
    *,
    predicate: PredicateEvidence,
    evaluated_at: str = T1,
    expires_at: str = T5,
    predecessor: str | None = None,
    trigger: ReevaluationTrigger | None = None,
    market: MarketDecisionIdentity | None = None,
    reason_code: str = "bounded-reason",
) -> LiveDecisionDisposition:
    return LiveDecisionDisposition(
        decision_id="decision-pre-policy",
        strategy_id="strategy-1",
        strategy_version="strategy-v1",
        market=market or _market(),
        required_evidence_policy_sha256="f" * 64,
        decision_at=T0,
        expires_at=expires_at,
        evaluated_at=evaluated_at,
        disposition=disposition,
        reason_code=reason_code,
        predicates=(predicate,),
        predecessor_disposition_id=predecessor,
        reevaluation_trigger=trigger,
    )


def _authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="goal-reevaluation-test",
        revision=1,
        bankroll_id="bankroll-reevaluation-test",
        currency="EUR",
    )
    return EconomicDecisionAuthority(
        contract=goal,
        risk_policy=PaperRiskPolicy(economic_goal=goal),
    )


def _append_zero_policy(
    ledger: JsonlDecisionLedger,
    authority: EconomicDecisionAuthority,
    *,
    decision_id: str,
    decision_at: str,
    plan_reason: str,
    context_digit: str,
) -> ProductPolicyAuthorityBinding:
    plan = PortfolioPlan(
        decision_ts=decision_at,
        action=PortfolioAction.ZERO,
        stakes=(),
        intent_ids=(),
        intent_sha256s=(),
        opportunity_classes=(),
        portfolio_sha256=None,
        dependency_graph=None,
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(
            authority.contract
        ).contract_sha256,
        risk_policy_sha256=authority.risk_policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason=plan_reason,
    )
    payload = {
        "schema": "autosport.persistent_live_decision",
        "schema_version": 2,
        "loop_id": "loop-reevaluation-test",
        "mode": "paper",
        "gate": "normal",
        "market_state_sha256": context_digit * 64,
        "decision_context_sha256": chr(ord(context_digit) + 1) * 64,
        "intent_strategy_version_id": "strategy-v1",
        "intent_model_version_id": None,
        "intent_provenance_sha256": chr(ord(context_digit) + 2) * 64,
        "affected_input_ids": ["input-1"],
        "plan_sha256": plan.plan_sha256,
        "plan": plan.to_dict(),
        MATERIAL_ACTION_ID_PAYLOAD_KEY: decision_id,
    }
    ledger.append_economic(
        DecisionRecord(
            replay_run_id="live:loop-reevaluation-test",
            agent="autosport-live-decision-loop",
            observed_ts=decision_at,
            action="LIVE_ZERO",
            payload=payload,
            context_hash=chr(ord(context_digit) + 3) * 64,
            decision_id=decision_id,
            decision_kind=ECONOMIC_DECISION_KIND,
        ),
        authority,
    )
    return bind_product_policy_authority(
        ledger=ledger,
        authority=authority,
        decision_id=decision_id,
    )


def _policy_disposition(
    binding: ProductPolicyAuthorityBinding,
    *,
    evaluated_at: str,
    expires_at: str,
    predecessor: str | None = None,
    trigger: ReevaluationTrigger | None = None,
) -> LiveDecisionDisposition:
    return LiveDecisionDisposition(
        decision_id=binding.economic_decision_id,
        strategy_id="strategy-1",
        strategy_version="strategy-v1",
        market=_market(),
        required_evidence_policy_sha256="f" * 64,
        decision_at=binding.policy_evaluated_at,
        expires_at=expires_at,
        evaluated_at=evaluated_at,
        disposition=Disposition.NO_BET_POLICY,
        reason_code="product-zero-plan",
        predicates=(_predicate(PredicateTruth.PROVEN),),
        product_policy_authority=binding,
        predecessor_disposition_id=predecessor,
        reevaluation_trigger=trigger,
    )


class LiveDecisionReevaluationTests(unittest.TestCase):
    def test_persist_is_idempotent_and_restart_resolves_exact_canonical_bytes(self) -> None:
        predecessor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)

            first = persist_live_decision_disposition(predecessor, ledger=ledger)
            second = persist_live_decision_disposition(predecessor, ledger=ledger)

            self.assertEqual(first.decision_id, second.decision_id)
            self.assertEqual(ledger.verify_integrity(), 1)
            restarted = JsonlDecisionLedger(path)
            self.assertEqual(
                resolve_live_decision_disposition(
                    predecessor.disposition_id,
                    ledger=restarted,
                ),
                predecessor,
            )

    def test_random_predecessor_reference_fails_closed(self) -> None:
        successor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),
            evaluated_at=T2,
            expires_at=T10,
            predecessor="0" * 64,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "predecessor disposition is missing",
            ):
                verify_reevaluation_transition(successor, ledger=ledger)

    def test_new_evidence_rejects_reason_clock_and_ttl_only_change(self) -> None:
        predecessor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
        )
        unchanged = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(
                PredicateTruth.UNKNOWN,
                reason="same-bytes-different-interpretation-text",
            ),
            evaluated_at=T2,
            expires_at=T10,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
            reason_code="new-reason-only",
        )
        changed = replace(
            unchanged,
            predicates=(_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            persist_live_decision_disposition(predecessor, ledger=ledger)

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "NEW_EVIDENCE requires materially changed bound evidence",
            ):
                verify_reevaluation_transition(unchanged, ledger=ledger)
            self.assertEqual(
                verify_reevaluation_transition(changed, ledger=ledger),
                predecessor,
            )

    def test_positive_new_evidence_requires_policy_decision_after_predecessor_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            authority = _authority()
            predecessor = _pre_policy(
                Disposition.WAIT_EVIDENCE,
                predicate=_predicate(PredicateTruth.UNKNOWN),
                evaluated_at=T2,
                expires_at=T10,
            )
            persist_live_decision_disposition(predecessor, ledger=ledger)

            stale_binding = _append_zero_policy(
                ledger,
                authority,
                decision_id="zero-policy-stale",
                decision_at=T1,
                plan_reason="stale product zero plan",
                context_digit="1",
            )
            stale = _policy_disposition(
                stale_binding,
                evaluated_at=T3,
                expires_at=T10,
                predecessor=predecessor.disposition_id,
                trigger=ReevaluationTrigger.NEW_EVIDENCE,
            )
            stale = replace(
                stale,
                predicates=(_predicate(PredicateTruth.PROVEN, digest="b" * 64),),
            )
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "policy decision must follow predecessor evaluation",
            ):
                verify_reevaluation_transition(
                    stale,
                    ledger=ledger,
                    authority=authority,
                )

            equal_binding = _append_zero_policy(
                ledger,
                authority,
                decision_id="zero-policy-equal",
                decision_at=T2,
                plan_reason="equal-time product zero plan",
                context_digit="4",
            )
            equal = _policy_disposition(
                equal_binding,
                evaluated_at=T3,
                expires_at=T10,
                predecessor=predecessor.disposition_id,
                trigger=ReevaluationTrigger.NEW_EVIDENCE,
            )
            equal = replace(
                equal,
                predicates=(_predicate(PredicateTruth.PROVEN, digest="b" * 64),),
            )
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "policy decision must follow predecessor evaluation",
            ):
                verify_reevaluation_transition(
                    equal,
                    ledger=ledger,
                    authority=authority,
                )

            fresh_binding = _append_zero_policy(
                ledger,
                authority,
                decision_id="zero-policy-fresh",
                decision_at=T3,
                plan_reason="fresh product zero plan",
                context_digit="a",
            )
            fresh = _policy_disposition(
                fresh_binding,
                evaluated_at=T5,
                expires_at=T10,
                predecessor=predecessor.disposition_id,
                trigger=ReevaluationTrigger.NEW_EVIDENCE,
            )
            fresh = replace(
                fresh,
                predicates=(_predicate(PredicateTruth.PROVEN, digest="b" * 64),),
            )
            self.assertEqual(
                verify_reevaluation_transition(
                    fresh,
                    ledger=ledger,
                    authority=authority,
                ),
                predecessor,
            )

    def test_cross_market_predecessor_is_rejected_even_with_changed_evidence(self) -> None:
        predecessor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
        )
        successor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),
            evaluated_at=T2,
            expires_at=T10,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
            market=_market(event_id="different-event"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            persist_live_decision_disposition(predecessor, ledger=ledger)
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "different decision lineage",
            ):
                verify_reevaluation_transition(successor, ledger=ledger)

    def test_expiry_recompute_requires_expired_predecessor_and_new_evidence(self) -> None:
        predecessor = _pre_policy(
            Disposition.EXPIRED,
            predicate=_predicate(PredicateTruth.PROVEN),
            evaluated_at=T5,
        )
        same_evidence = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
            evaluated_at=T6,
            expires_at=T20,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.EXPIRY_RECOMPUTE,
        )
        changed_evidence = replace(
            same_evidence,
            predicates=(_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            persist_live_decision_disposition(predecessor, ledger=ledger)
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "EXPIRY_RECOMPUTE requires materially changed bound evidence",
            ):
                verify_reevaluation_transition(same_evidence, ledger=ledger)
            self.assertEqual(
                verify_reevaluation_transition(changed_evidence, ledger=ledger),
                predecessor,
            )

    def test_safety_revalidation_cannot_clear_halt_by_relabeling_same_evidence(self) -> None:
        predecessor = _pre_policy(
            Disposition.HALT_SAFETY,
            predicate=_predicate(PredicateTruth.FAILED),
        )
        relabeled = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
            evaluated_at=T2,
            expires_at=T10,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.SAFETY_REVALIDATION,
        )
        changed = replace(
            relabeled,
            predicates=(_predicate(PredicateTruth.UNKNOWN, digest="c" * 64),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            persist_live_decision_disposition(predecessor, ledger=ledger)
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "SAFETY_REVALIDATION requires changed evidence",
            ):
                verify_reevaluation_transition(relabeled, ledger=ledger)
            self.assertEqual(
                verify_reevaluation_transition(changed, ledger=ledger),
                predecessor,
            )

    def test_policy_reevaluation_requires_two_reverified_changed_product_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            authority = _authority()
            first_binding = _append_zero_policy(
                ledger,
                authority,
                decision_id="zero-policy-1",
                decision_at=T0,
                plan_reason="first product zero plan",
                context_digit="1",
            )
            predecessor = _policy_disposition(
                first_binding,
                evaluated_at=T1,
                expires_at=T5,
            )
            persist_live_decision_disposition(predecessor, ledger=ledger)

            same_binding = _policy_disposition(
                first_binding,
                evaluated_at=T2,
                expires_at=T10,
                predecessor=predecessor.disposition_id,
                trigger=ReevaluationTrigger.POLICY_REEVALUATION,
            )
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "materially changed product policy authority",
            ):
                verify_reevaluation_transition(
                    same_binding,
                    ledger=ledger,
                    authority=authority,
                )

            second_binding = _append_zero_policy(
                ledger,
                authority,
                decision_id="zero-policy-2",
                decision_at=T2,
                plan_reason="second product zero plan",
                context_digit="4",
            )
            changed_binding = _policy_disposition(
                second_binding,
                evaluated_at=T3,
                expires_at=T10,
                predecessor=predecessor.disposition_id,
                trigger=ReevaluationTrigger.POLICY_REEVALUATION,
            )
            self.assertEqual(
                verify_reevaluation_transition(
                    changed_binding,
                    ledger=ledger,
                    authority=authority,
                ),
                predecessor,
            )

    def test_restart_tamper_of_persisted_predecessor_fails_closed(self) -> None:
        predecessor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
        )
        successor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),
            evaluated_at=T2,
            expires_at=T10,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            persist_live_decision_disposition(predecessor, ledger=ledger)
            raw = path.read_text(encoding="utf-8")
            self.assertIn("quote-evidence-1", raw)
            path.write_text(
                raw.replace("quote-evidence-1", "quote-evidence-X", 1),
                encoding="utf-8",
                newline="\n",
            )
            restarted = JsonlDecisionLedger(path)
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "durable disposition ledger cannot be verified",
            ):
                verify_reevaluation_transition(successor, ledger=restarted)

    def test_backdated_reevaluation_is_rejected(self) -> None:
        predecessor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN),
            evaluated_at=T2,
            expires_at=T10,
        )
        successor = _pre_policy(
            Disposition.WAIT_EVIDENCE,
            predicate=_predicate(PredicateTruth.UNKNOWN, digest="b" * 64),
            evaluated_at=T1,
            expires_at=T10,
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonlDecisionLedger(Path(temporary) / "decisions.jsonl")
            persist_live_decision_disposition(predecessor, ledger=ledger)
            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "evaluation time cannot move backwards",
            ):
                verify_reevaluation_transition(successor, ledger=ledger)


if __name__ == "__main__":
    unittest.main()
