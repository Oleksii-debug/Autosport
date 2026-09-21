from __future__ import annotations

import copy
import unittest

from autosport.live_decision_disposition import (
    Disposition,
    EvidenceReference,
    LiveDecisionDisposition,
    LiveDecisionDispositionError,
    MarketDecisionIdentity,
    PredicateEvidence,
    PredicateTruth,
    ProductPolicyAuthorityBinding,
)


def _ref(kind: str, evidence_id: str, digest: str) -> EvidenceReference:
    return EvidenceReference(
        authority_kind=kind,
        evidence_id=evidence_id,
        evidence_sha256=digest,
    )


def _predicate(
    predicate_id: str,
    *refs: EvidenceReference,
) -> PredicateEvidence:
    return PredicateEvidence(
        predicate_id=predicate_id,
        truth=PredicateTruth.PROVEN,
        reason_code="proven",
        evidence=tuple(refs),
    )


def _product_binding() -> ProductPolicyAuthorityBinding:
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
        plan_action="paper_plan",
        policy_evaluated_at="2026-09-21T08:40:00.000000Z",
        has_positive_execution_stake=True,
    )


def _disposition(
    predicates: tuple[PredicateEvidence, ...],
) -> LiveDecisionDisposition:
    return LiveDecisionDisposition(
        decision_id="decision-1",
        strategy_id="strategy-1",
        strategy_version="v1",
        market=MarketDecisionIdentity(
            sport="football",
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            side="BACK",
        ),
        required_evidence_policy_sha256="f" * 64,
        decision_at="2026-09-21T08:40:00.000000Z",
        expires_at="2026-09-21T08:40:05.000000Z",
        evaluated_at="2026-09-21T08:40:01.000000Z",
        disposition=Disposition.ACTIONABLE,
        reason_code="eligible",
        predicates=predicates,
        product_policy_authority=_product_binding(),
    )


class LiveDecisionDispositionCanonicalOrderTests(unittest.TestCase):
    def test_semantically_identical_permutations_share_one_identity(self) -> None:
        quote_a = _ref("quote", "a", "a" * 64)
        quote_b = _ref("quote", "b", "b" * 64)
        source = _ref("source-health", "health", "c" * 64)

        first = _disposition(
            (
                _predicate("quote_fresh", quote_b, quote_a),
                _predicate("source_healthy", source),
            )
        )
        second = _disposition(
            (
                _predicate("source_healthy", source),
                _predicate("quote_fresh", quote_a, quote_b),
            )
        )

        self.assertEqual(first.disposition_id, second.disposition_id)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(
            [item.predicate_id for item in first.predicates],
            ["quote_fresh", "source_healthy"],
        )
        self.assertEqual(
            [item.evidence_id for item in first.predicates[0].evidence],
            ["a", "b"],
        )

    def test_noncanonical_wire_order_is_rejected_even_with_valid_digest(self) -> None:
        quote_a = _ref("quote", "a", "a" * 64)
        quote_b = _ref("quote", "b", "b" * 64)
        source = _ref("source-health", "health", "c" * 64)
        value = _disposition(
            (
                _predicate("quote_fresh", quote_a, quote_b),
                _predicate("source_healthy", source),
            )
        )
        payload = value.to_dict()
        reversed_predicates = copy.deepcopy(payload)
        reversed_predicates["predicates"] = list(
            reversed(reversed_predicates["predicates"])
        )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "not canonical",
        ):
            LiveDecisionDisposition.from_dict(reversed_predicates)

        reversed_evidence = copy.deepcopy(payload)
        reversed_evidence["predicates"][0]["evidence"] = list(
            reversed(reversed_evidence["predicates"][0]["evidence"])
        )
        with self.assertRaisesRegex(
            LiveDecisionDispositionError,
            "not canonical",
        ):
            LiveDecisionDisposition.from_dict(reversed_evidence)


if __name__ == "__main__":
    unittest.main()
