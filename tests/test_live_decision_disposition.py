from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal
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
    verify_product_policy_authority,
)
from autosport.portfolio_plan import EvidenceTruth, PortfolioAction, PortfolioPlan
from autosport.risk import PaperRiskPolicy


DECISION_AT = "2026-09-21T08:40:00.000000Z"
EVALUATED_AT = "2026-09-21T08:40:01.000000Z"
EXPIRES_AT = "2026-09-21T08:40:05.000000Z"
AFTER_EXPIRY = "2026-09-21T08:40:05.000000Z"


def _market() -> MarketDecisionIdentity:
    return MarketDecisionIdentity(
        sport="football",
        event_id="event-1",
        market_id="match-winner",
        selection_id="home",
        side="BACK",
        line=None,
    )


def _ref(
    kind: str = "quote",
    evidence_id: str = "quote-1",
    digest: str = "a" * 64,
) -> EvidenceReference:
    return EvidenceReference(
        authority_kind=kind,
        evidence_id=evidence_id,
        evidence_sha256=digest,
    )


def _predicate(
    predicate_id: str,
    truth: PredicateTruth = PredicateTruth.PROVEN,
    *,
    reason: str = "evidence-current",
    digest: str = "a" * 64,
) -> PredicateEvidence:
    return PredicateEvidence(
        predicate_id=predicate_id,
        truth=truth,
        reason_code=reason,
        evidence=(_ref(predicate_id, f"{predicate_id}-evidence", digest),),
    )


def _binding(*, positive: bool = True) -> ProductPolicyAuthorityBinding:
    return ProductPolicyAuthorityBinding(
        economic_decision_id="decision-1",
        economic_decision_sha256="0" * 64,
        record_context_sha256="1" * 64,
        decision_context_sha256="2" * 64,
        market_state_sha256="3" * 64,
        intent_provenance_sha256="4" * 64,
        economic_goal_contract_sha256="5" * 64,
        risk_policy_sha256="6" * 64,
        plan_sha256="7" * 64,
        plan_action="paper_plan" if positive else "zero",
        policy_evaluated_at=DECISION_AT,
        has_positive_execution_stake=positive,
    )


def _disposition(
    disposition: Disposition = Disposition.ACTIONABLE,
    *,
    predicates: tuple[PredicateEvidence, ...] | None = None,
    evaluated_at: str = EVALUATED_AT,
    predecessor: str | None = None,
    trigger: ReevaluationTrigger | None = None,
    **overrides: object,
) -> LiveDecisionDisposition:
    binding: ProductPolicyAuthorityBinding | None = None
    if disposition is Disposition.ACTIONABLE:
        binding = _binding(positive=True)
    elif disposition is Disposition.NO_BET_POLICY:
        binding = _binding(positive=False)
    values: dict[str, object] = {
        "decision_id": "decision-1",
        "strategy_id": "strategy-1",
        "strategy_version": "strategy-v1",
        "market": _market(),
        "required_evidence_policy_sha256": "f" * 64,
        "decision_at": DECISION_AT,
        "expires_at": EXPIRES_AT,
        "evaluated_at": evaluated_at,
        "disposition": disposition,
        "reason_code": (
            "eligible"
            if disposition is Disposition.ACTIONABLE
            else "bounded-reason"
        ),
        "predicates": predicates
        or (
            _predicate("quote_fresh"),
            _predicate("source_healthy", digest="b" * 64),
            _predicate("continuity_sufficient", digest="c" * 64),
        ),
        "product_policy_authority": binding,
        "predecessor_disposition_id": predecessor,
        "reevaluation_trigger": trigger,
    }
    values.update(overrides)
    return LiveDecisionDisposition(**values)


def _authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="goal-live-policy-test",
        revision=1,
        bankroll_id="bankroll-live-policy-test",
        currency="EUR",
    )
    return EconomicDecisionAuthority(
        contract=goal,
        risk_policy=PaperRiskPolicy(economic_goal=goal),
    )


def _product_zero_policy(
    root: Path,
) -> tuple[
    JsonlDecisionLedger,
    EconomicDecisionAuthority,
    ProductPolicyAuthorityBinding,
]:
    authority = _authority()
    plan = PortfolioPlan(
        decision_ts=DECISION_AT,
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
        reason="product policy zero plan",
    )
    decision_id = "live-policy-zero"
    payload = {
        "schema": "autosport.persistent_live_decision",
        "schema_version": 2,
        "loop_id": "loop-live-policy-test",
        "mode": "paper",
        "gate": "normal",
        "market_state_sha256": "a" * 64,
        "decision_context_sha256": "b" * 64,
        "intent_strategy_version_id": "strategy-v1",
        "intent_model_version_id": None,
        "intent_provenance_sha256": "c" * 64,
        "affected_input_ids": ["input-1"],
        "plan_sha256": plan.plan_sha256,
        "plan": plan.to_dict(),
        MATERIAL_ACTION_ID_PAYLOAD_KEY: decision_id,
    }
    ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    ledger.append_economic(
        DecisionRecord(
            replay_run_id="live:loop-live-policy-test",
            agent="autosport-live-decision-loop",
            observed_ts=DECISION_AT,
            action="LIVE_ZERO",
            payload=payload,
            context_hash="d" * 64,
            decision_id=decision_id,
            recorded_at="2026-09-21T08:40:00.500000Z",
            decision_kind=ECONOMIC_DECISION_KIND,
        ),
        authority,
    )
    return (
        ledger,
        authority,
        bind_product_policy_authority(
            ledger=ledger,
            authority=authority,
            decision_id=decision_id,
        ),
    )


class LiveDecisionDispositionTests(unittest.TestCase):
    def test_actionable_requires_every_predicate_proven_but_never_authorizes_execution(
        self,
    ) -> None:
        value = _disposition()
        self.assertIs(value.disposition, Disposition.ACTIONABLE)
        self.assertIsNotNone(value.product_policy_authority)
        self.assertFalse(value.execution_authorized)
        self.assertFalse(value.provider_write_authorized)
        self.assertFalse(value.learning_outcome_authorized)
        self.assertFalse(value.real_money_execution)
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "ACTIONABLE requires",
        ):
            _disposition(
                predicates=(
                    _predicate("quote_fresh"),
                    _predicate(
                        "continuity_sufficient",
                        PredicateTruth.UNKNOWN,
                    ),
                )
            )

    def test_positive_policy_outcomes_require_product_authority_reference(self) -> None:
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "requires product-owned economic policy authority",
        ):
            _disposition(
                Disposition.ACTIONABLE,
                product_policy_authority=None,
            )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "requires product-owned economic policy authority",
        ):
            _disposition(
                Disposition.NO_BET_POLICY,
                product_policy_authority=None,
            )

    def test_missing_or_ambiguous_evidence_is_wait_not_no_bet(self) -> None:
        predicates = (
            _predicate("quote_fresh"),
            _predicate(
                "continuity_sufficient",
                PredicateTruth.UNKNOWN,
                reason="continuity-unverified",
            ),
        )
        wait = _disposition(
            Disposition.WAIT_EVIDENCE,
            predicates=predicates,
        )
        self.assertIs(wait.disposition, Disposition.WAIT_EVIDENCE)
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "NO_BET_POLICY requires",
        ):
            _disposition(
                Disposition.NO_BET_POLICY,
                predicates=predicates,
            )

    def test_no_bet_policy_is_a_proven_negative_decision(self) -> None:
        value = _disposition(Disposition.NO_BET_POLICY)
        self.assertTrue(
            all(item.truth is PredicateTruth.PROVEN for item in value.predicates)
        )
        self.assertIsNotNone(value.product_policy_authority)
        self.assertFalse(value.learning_outcome_authorized)

    def test_no_bet_rejects_positive_plan_reference_even_if_predicates_are_proven(self) -> None:
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "zero-stake product-owned portfolio plan",
        ):
            _disposition(
                Disposition.NO_BET_POLICY,
                product_policy_authority=_binding(positive=True),
            )

    def test_failed_predicate_requires_halt_safety(self) -> None:
        predicates = (
            _predicate("quote_fresh"),
            _predicate(
                "identity_consistent",
                PredicateTruth.FAILED,
                reason="identity-conflict",
            ),
        )
        halt = _disposition(
            Disposition.HALT_SAFETY,
            predicates=predicates,
        )
        self.assertIs(halt.disposition, Disposition.HALT_SAFETY)
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "WAIT_EVIDENCE requires",
        ):
            _disposition(
                Disposition.WAIT_EVIDENCE,
                predicates=predicates,
            )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "FAILED safety",
        ):
            _disposition(
                Disposition.EXPIRED,
                predicates=predicates,
                evaluated_at=AFTER_EXPIRY,
            )

    def test_expiry_is_temporal_and_cannot_be_implicitly_revived(self) -> None:
        expired = _disposition(
            Disposition.EXPIRED,
            evaluated_at=AFTER_EXPIRY,
        )
        self.assertIs(expired.disposition, Disposition.EXPIRED)
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "ACTIONABLE disposition is expired",
        ):
            _disposition(
                Disposition.ACTIONABLE,
                evaluated_at=AFTER_EXPIRY,
            )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "EXPIRED requires",
        ):
            _disposition(
                Disposition.EXPIRED,
                evaluated_at=EVALUATED_AT,
            )

    def test_restart_round_trip_preserves_exact_identity_and_unknown(self) -> None:
        original = _disposition(
            Disposition.WAIT_EVIDENCE,
            predicates=(
                _predicate("quote_fresh"),
                _predicate(
                    "history_continuity",
                    PredicateTruth.UNKNOWN,
                    reason="backfill-unproven",
                ),
            ),
        )
        reopened = LiveDecisionDisposition.from_json(original.to_json())
        self.assertEqual(reopened, original)
        self.assertEqual(reopened.disposition_id, original.disposition_id)
        self.assertIs(
            reopened.predicates[1].truth,
            PredicateTruth.UNKNOWN,
        )
        self.assertIs(
            reopened.disposition,
            Disposition.WAIT_EVIDENCE,
        )

    def test_positive_restart_round_trip_preserves_policy_reference_but_not_verification(
        self,
    ) -> None:
        original = _disposition(Disposition.NO_BET_POLICY)
        reopened = LiveDecisionDisposition.from_json(original.to_json())
        self.assertEqual(reopened, original)
        self.assertEqual(
            reopened.product_policy_authority,
            original.product_policy_authority,
        )

    def test_changed_evidence_changes_disposition_identity(self) -> None:
        first = _disposition()
        second = _disposition(
            predicates=(
                _predicate("quote_fresh", digest="d" * 64),
                _predicate("source_healthy", digest="b" * 64),
                _predicate("continuity_sufficient", digest="c" * 64),
            )
        )
        self.assertNotEqual(
            first.disposition_id,
            second.disposition_id,
        )

    def test_reevaluation_requires_exact_predecessor_and_trigger_pair(
        self,
    ) -> None:
        predecessor = _disposition(
            Disposition.WAIT_EVIDENCE,
            predicates=(
                _predicate(
                    "quote_fresh",
                    PredicateTruth.UNKNOWN,
                ),
            ),
        )
        current = _disposition(
            predecessor=predecessor.disposition_id,
            trigger=ReevaluationTrigger.NEW_EVIDENCE,
        )
        self.assertEqual(
            current.predecessor_disposition_id,
            predecessor.disposition_id,
        )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "present together",
        ):
            _disposition(predecessor=predecessor.disposition_id)
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "present together",
        ):
            _disposition(trigger=ReevaluationTrigger.NEW_EVIDENCE)

    def test_duplicate_predicate_and_evidence_identities_fail_closed(
        self,
    ) -> None:
        duplicate_predicate = _predicate("quote_fresh")
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "predicate_id values",
        ):
            _disposition(
                predicates=(
                    duplicate_predicate,
                    duplicate_predicate,
                )
            )
        evidence = _ref()
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "contains duplicates",
        ):
            PredicateEvidence(
                predicate_id="quote_fresh",
                truth=PredicateTruth.PROVEN,
                reason_code="current",
                evidence=(evidence, evidence),
            )

    def test_strict_wire_types_and_canonical_timestamps_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "disposition must",
        ):
            _disposition(
                disposition="ACTIONABLE"  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "canonical UTC",
        ):
            _disposition(
                decision_at="2026-09-21T08:40:00+00:00"
            )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "unsupported disposition schema",
        ):
            _disposition(schema_version=True)

    def test_digest_tamper_and_unknown_fields_fail_closed(self) -> None:
        value = _disposition()
        payload = value.to_dict()
        payload["disposition_id"] = "0" * 64
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "digest mismatch",
        ):
            LiveDecisionDisposition.from_dict(payload)

        payload = value.to_dict()
        payload["unexpected"] = "field"
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "fields mismatch",
        ):
            LiveDecisionDisposition.from_dict(payload)

    def test_duplicate_json_keys_fail_closed(self) -> None:
        value = _disposition()
        raw = value.to_json().strip()
        malicious = raw[:-1] + ',"decision_id":"other"}'
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "duplicate JSON key",
        ):
            LiveDecisionDisposition.from_json(malicious)

    def test_false_authority_flags_cannot_be_promoted_on_reopen(self) -> None:
        payload = _disposition().to_dict()
        for field in (
            "execution_authorized",
            "provider_write_authorized",
            "learning_outcome_authorized",
            "real_money_execution",
        ):
            changed = dict(payload)
            changed[field] = True
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    LiveDecisionDispositionError,
                    "must remain exactly false",
                ):
                    LiveDecisionDisposition.from_dict(changed)

    def test_contract_contains_no_provider_credentials_or_money_fields(self) -> None:
        names = {
            field.name.lower()
            for field in fields(LiveDecisionDisposition)
        }
        forbidden = (
            "password",
            "token",
            "secret",
            "credential",
            "api_key",
            "payout",
            "profit",
            "bankroll",
            "balance",
            "stake",
        )
        self.assertFalse(
            any(
                part in name
                for name in names
                for part in forbidden
            )
        )

    def test_market_identity_is_sport_and_line_aware(self) -> None:
        football = _disposition()
        tennis = _disposition(
            market=MarketDecisionIdentity(
                sport="tennis",
                event_id="event-1",
                market_id="match-winner",
                selection_id="home",
                side="BACK",
                line=None,
            )
        )
        handicap = _disposition(
            market=MarketDecisionIdentity(
                sport="football",
                event_id="event-1",
                market_id="handicap",
                selection_id="home",
                side="BACK",
                line="-1.5",
            )
        )
        self.assertNotEqual(
            football.disposition_id,
            tennis.disposition_id,
        )
        self.assertNotEqual(
            football.disposition_id,
            handicap.disposition_id,
        )

    def test_product_no_bet_re_resolves_exact_durable_economic_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger, authority, binding = _product_zero_policy(Path(temporary))
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=binding.economic_decision_id,
                decision_at=binding.policy_evaluated_at,
                product_policy_authority=binding,
            )

            record = verify_product_policy_authority(
                disposition,
                ledger=ledger,
                authority=authority,
            )

            self.assertEqual(record.decision_id, binding.economic_decision_id)
            self.assertEqual(
                record.payload["plan_sha256"],
                binding.plan_sha256,
            )
            self.assertFalse(binding.has_positive_execution_stake)
            self.assertEqual(binding.plan_action, "zero")

    def test_random_product_policy_witness_digest_fails_re_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger, authority, binding = _product_zero_policy(Path(temporary))
            forged = replace(
                binding,
                economic_decision_sha256="f" * 64,
            )
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=forged.economic_decision_id,
                decision_at=forged.policy_evaluated_at,
                product_policy_authority=forged,
            )

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "binding does not match durable truth",
            ):
                verify_product_policy_authority(
                    disposition,
                    ledger=ledger,
                    authority=authority,
                )

    def test_wrong_product_decision_record_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger, authority, binding = _product_zero_policy(Path(temporary))
            forged = replace(
                binding,
                economic_decision_id="live-other-decision",
            )
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=forged.economic_decision_id,
                decision_at=forged.policy_evaluated_at,
                product_policy_authority=forged,
            )

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "cannot be re-resolved",
            ):
                verify_product_policy_authority(
                    disposition,
                    ledger=ledger,
                    authority=authority,
                )

    def test_changed_decision_context_reference_fails_re_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger, authority, binding = _product_zero_policy(Path(temporary))
            forged = replace(
                binding,
                decision_context_sha256="9" * 64,
            )
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=forged.economic_decision_id,
                decision_at=forged.policy_evaluated_at,
                product_policy_authority=forged,
            )

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "binding does not match durable truth",
            ):
                verify_product_policy_authority(
                    disposition,
                    ledger=ledger,
                    authority=authority,
                )

    def test_changed_economic_goal_authority_fails_re_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger, authority, binding = _product_zero_policy(Path(temporary))
            changed_goal = replace(authority.contract, revision=2)
            changed_authority = EconomicDecisionAuthority(
                contract=changed_goal,
                risk_policy=PaperRiskPolicy(economic_goal=changed_goal),
            )
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=binding.economic_decision_id,
                decision_at=binding.policy_evaluated_at,
                product_policy_authority=binding,
            )

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "cannot be re-resolved",
            ):
                verify_product_policy_authority(
                    disposition,
                    ledger=ledger,
                    authority=changed_authority,
                )

    def test_restart_tamper_of_product_policy_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger, authority, binding = _product_zero_policy(root)
            disposition = _disposition(
                Disposition.NO_BET_POLICY,
                decision_id=binding.economic_decision_id,
                decision_at=binding.policy_evaluated_at,
                product_policy_authority=binding,
            )
            raw = ledger.path.read_text(encoding="utf-8")
            self.assertIn("strategy-v1", raw)
            ledger.path.write_text(
                raw.replace("strategy-v1", "strategy-v2", 1),
                encoding="utf-8",
                newline="\n",
            )
            restarted = JsonlDecisionLedger(ledger.path)

            with self.assertRaisesRegex(
                LiveDecisionDispositionError,
                "cannot be re-resolved",
            ):
                verify_product_policy_authority(
                    disposition,
                    ledger=restarted,
                    authority=authority,
                )


if __name__ == "__main__":
    unittest.main()
