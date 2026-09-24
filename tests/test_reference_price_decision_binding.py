from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_decision_binding import (
    ReferencePriceDecisionBindingError,
    bind_reference_price_evidence,
    resolve_ledger_reference_price_evidence,
    resolve_reference_price_evidence,
)
from autosport.reference_price_evidence import (
    ReferencePriceProtocol,
    ReferenceTargetInclusionPolicy,
    build_reference_price_evidence,
)


class ReferencePriceDecisionBindingTests(unittest.TestCase):
    DECISION_TS = "2026-09-24T01:20:10+00:00"

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.ledger_path = Path(self.temporary.name) / "decision-ledger.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def event(
        source_id: str,
        odds: str,
        *,
        sequence: int = 1,
        observed_ts: str = "2026-09-24T01:20:00+00:00",
        source_ts: str = "2026-09-24T01:19:59+00:00",
        ingest_ts: str = "2026-09-24T01:20:01+00:00",
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=source_id,
            sequence=sequence,
            market_type=MarketType.WINNER,
            status="open",
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            metadata={
                "price_semantics": "best_available_to_back",
                "execution_quote_verified": False,
                "bookmaker_key": source_id,
            },
            sport="table_tennis",
            market_semantics_id="winner.match.v1",
        )

    def evidence(
        self,
        *,
        odds_a: str = "2.00",
        odds_b: str = "2.10",
    ):
        protocol = ReferencePriceProtocol(
            eligible_source_ids=("provider-a", "provider-b"),
            eligible_price_source_ids=("provider-a", "provider-b"),
            target_price_source_id="target-provider",
            target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
            price_semantics="best_available_to_back",
            max_age_seconds=30,
            max_skew_seconds=5,
            minimum_sources=2,
        )
        return build_reference_price_evidence(
            (
                self.event("provider-a", odds_a),
                self.event("provider-b", odds_b),
            ),
            decision_ts=self.DECISION_TS,
            protocol=protocol,
        )

    def decision(
        self,
        *,
        decision_id: str = "decision-1",
        observed_ts: str | None = None,
        payload: dict[str, object] | None = None,
        decision_kind: str = "GENERAL",
    ) -> DecisionRecord:
        return DecisionRecord(
            replay_run_id="run-live-1",
            agent="reference-strategy",
            observed_ts=observed_ts or self.DECISION_TS,
            action="EVALUATE_REFERENCE",
            payload=payload
            or {
                "candidate_id": "candidate-1",
                "target_quote_id": "target-quote-1",
            },
            context_hash="context-sha-placeholder",
            decision_id=decision_id,
            recorded_at="2026-09-24T01:20:10.500000+00:00",
            decision_kind=decision_kind,
        )

    def ledger(self) -> JsonlDecisionLedger:
        return JsonlDecisionLedger(self.ledger_path)

    def test_append_and_restart_reconstruct_exact_reference_evidence(self) -> None:
        evidence = self.evidence()
        decision = self.decision()
        bound = bind_reference_price_evidence(decision, evidence)

        self.ledger().append(bound)
        restarted = JsonlDecisionLedger(self.ledger_path)
        resolved = resolve_ledger_reference_price_evidence(
            restarted,
            decision.decision_id,
        )

        self.assertEqual(resolved.decision_id, decision.decision_id)
        self.assertEqual(resolved.evidence, evidence)
        self.assertEqual(resolved.evidence.evidence_id, evidence.evidence_id)
        self.assertEqual(len(resolved.binding_sha256), 64)
        self.assertFalse(resolved.executable_reference_verified)
        self.assertFalse(resolved.fair_probability_verified)
        self.assertFalse(resolved.real_money_execution_authorized)

    def test_restart_resolution_ignores_instance_verified_records_shadow(self) -> None:
        evidence = self.evidence()
        durable = bind_reference_price_evidence(
            self.decision(decision_id="durable-decision"),
            evidence,
        )
        ledger = self.ledger()
        ledger.append(durable)

        forged = bind_reference_price_evidence(
            self.decision(decision_id="forged-decision"),
            evidence,
        )
        ledger.verified_records = lambda: (forged,)  # type: ignore[method-assign]
        self.assertEqual(ledger.verified_records(), (forged,))

        resolved = resolve_ledger_reference_price_evidence(
            ledger,
            "durable-decision",
        )
        self.assertEqual(resolved.decision_id, "durable-decision")
        self.assertEqual(resolved.evidence, evidence)

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "exactly one requested decision",
        ):
            resolve_ledger_reference_price_evidence(
                ledger,
                "forged-decision",
            )

    def test_binding_embeds_exact_canonical_event_bytes(self) -> None:
        evidence = self.evidence()
        bound = bind_reference_price_evidence(self.decision(), evidence)

        binding = bound.to_dict()["payload"]["reference_price_decision_evidence"]
        embedded = binding["evidence"]

        self.assertEqual(embedded, evidence.to_dict())
        self.assertEqual(
            embedded["observations"][0]["event_canonical_json"],
            evidence.observations[0].event_canonical_json,
        )
        self.assertEqual(
            embedded["observations"][1]["event_canonical_json"],
            evidence.observations[1].event_canonical_json,
        )

    def test_equivalent_utc_spelling_is_same_decision_instant(self) -> None:
        decision = self.decision(observed_ts="2026-09-24T01:20:10Z")

        bound = bind_reference_price_evidence(decision, self.evidence())
        resolved = resolve_reference_price_evidence(bound)

        self.assertEqual(resolved.decision_id, decision.decision_id)

    def test_mismatched_decision_cutoff_fails_before_binding(self) -> None:
        decision = self.decision(
            observed_ts="2026-09-24T01:20:11+00:00",
        )

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "observed_ts",
        ):
            bind_reference_price_evidence(decision, self.evidence())

    def test_existing_binding_cannot_be_overwritten(self) -> None:
        first = bind_reference_price_evidence(
            self.decision(),
            self.evidence(),
        )

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "already contains",
        ):
            bind_reference_price_evidence(first, self.evidence(odds_b="2.20"))

    def test_post_binding_decision_payload_drift_is_detected(self) -> None:
        bound = bind_reference_price_evidence(
            self.decision(),
            self.evidence(),
        )
        mutated = bound.to_dict()
        mutated["payload"]["candidate_id"] = "candidate-changed"
        forged = DecisionRecord(**mutated)

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "does not match canonical evidence",
        ):
            resolve_reference_price_evidence(forged)

    def test_embedded_scalar_cannot_override_canonical_event_bytes(self) -> None:
        bound = bind_reference_price_evidence(
            self.decision(),
            self.evidence(),
        )
        mutated = bound.to_dict()
        binding = mutated["payload"]["reference_price_decision_evidence"]
        binding["evidence"]["observations"][0]["decimal_odds"] = "99.0"
        forged = DecisionRecord(**mutated)

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "exact canonical payload",
        ):
            resolve_reference_price_evidence(forged)

    def test_binding_cannot_be_copied_to_another_decision_id(self) -> None:
        bound = bind_reference_price_evidence(
            self.decision(decision_id="decision-a"),
            self.evidence(),
        )
        copied = bound.to_dict()
        copied["decision_id"] = "decision-b"
        forged = DecisionRecord(**copied)

        with self.assertRaises(
            ReferencePriceDecisionBindingError,
        ):
            resolve_reference_price_evidence(forged)

    def test_later_corrected_reference_uses_new_decision_without_rewriting_old(self) -> None:
        first_evidence = self.evidence(odds_b="2.10")
        second_evidence = self.evidence(odds_b="2.30")
        first = bind_reference_price_evidence(
            self.decision(decision_id="decision-before-correction"),
            first_evidence,
        )
        second = bind_reference_price_evidence(
            self.decision(decision_id="decision-after-correction"),
            second_evidence,
        )

        ledger = self.ledger()
        ledger.append(first)
        ledger.append(second)
        restarted = JsonlDecisionLedger(self.ledger_path)

        old = resolve_ledger_reference_price_evidence(
            restarted,
            "decision-before-correction",
        )
        new = resolve_ledger_reference_price_evidence(
            restarted,
            "decision-after-correction",
        )

        self.assertEqual(old.evidence.evidence_id, first_evidence.evidence_id)
        self.assertEqual(new.evidence.evidence_id, second_evidence.evidence_id)
        self.assertNotEqual(old.evidence.evidence_id, new.evidence.evidence_id)

    def test_ledger_byte_tamper_fails_before_reference_readback(self) -> None:
        bound = bind_reference_price_evidence(
            self.decision(),
            self.evidence(),
        )
        ledger = self.ledger()
        ledger.append(bound)

        raw = self.ledger_path.read_text(encoding="utf-8")
        self.ledger_path.write_text(
            raw.replace("candidate-1", "candidate-X"),
            encoding="utf-8",
        )

        with self.assertRaises(DecisionLedgerIntegrityError):
            resolve_ledger_reference_price_evidence(
                JsonlDecisionLedger(self.ledger_path),
                "decision-1",
            )

    def test_protocol_relabel_inside_binding_fails_canonical_reconstruction(self) -> None:
        bound = bind_reference_price_evidence(
            self.decision(),
            self.evidence(),
        )
        mutated = bound.to_dict()
        binding = mutated["payload"]["reference_price_decision_evidence"]
        binding["evidence"]["protocol"]["price_semantics"] = "other-semantics"
        forged = DecisionRecord(**mutated)

        with self.assertRaises(
            ReferencePriceDecisionBindingError,
        ):
            resolve_reference_price_evidence(forged)

    def test_general_decision_without_binding_is_explicitly_unresolved(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "schema",
        ):
            resolve_reference_price_evidence(self.decision())

    def test_economic_decision_is_outside_this_child_authority(self) -> None:
        economic = self.decision(
            decision_kind=ECONOMIC_DECISION_KIND,
        )

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "GENERAL",
        ):
            bind_reference_price_evidence(economic, self.evidence())

    def test_missing_decision_id_after_restart_is_not_fabricated(self) -> None:
        ledger = self.ledger()
        ledger.append(
            bind_reference_price_evidence(
                self.decision(decision_id="decision-present"),
                self.evidence(),
            )
        )

        with self.assertRaisesRegex(
            ReferencePriceDecisionBindingError,
            "exactly one",
        ):
            resolve_ledger_reference_price_evidence(
                JsonlDecisionLedger(self.ledger_path),
                "decision-missing",
            )

    def test_duplicate_decision_id_corrupts_ledger_instead_of_rewriting_history(self) -> None:
        ledger = self.ledger()
        ledger.append(
            bind_reference_price_evidence(
                self.decision(decision_id="same-decision"),
                self.evidence(odds_b="2.10"),
            )
        )
        ledger.append(
            bind_reference_price_evidence(
                self.decision(decision_id="same-decision"),
                self.evidence(odds_b="2.30"),
            )
        )

        with self.assertRaisesRegex(
            DecisionLedgerIntegrityError,
            "duplicate decision_id",
        ):
            resolve_ledger_reference_price_evidence(
                JsonlDecisionLedger(self.ledger_path),
                "same-decision",
            )

    def test_binding_digest_is_stable_across_restart(self) -> None:
        evidence = self.evidence()
        bound = bind_reference_price_evidence(self.decision(), evidence)
        before = resolve_reference_price_evidence(bound)

        self.ledger().append(bound)
        after = resolve_ledger_reference_price_evidence(
            JsonlDecisionLedger(self.ledger_path),
            bound.decision_id,
        )

        self.assertEqual(after.binding_sha256, before.binding_sha256)
        self.assertEqual(
            after.decision_shell_sha256,
            before.decision_shell_sha256,
        )
        self.assertEqual(after.evidence_sha256, before.evidence_sha256)


if __name__ == "__main__":
    unittest.main()
