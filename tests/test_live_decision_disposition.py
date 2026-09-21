from __future__ import annotations

from dataclasses import fields
import unittest

from autosport.live_decision_disposition import (
    Disposition,
    EvidenceReference,
    LiveDecisionDisposition,
    LiveDecisionDispositionError,
    MarketDecisionIdentity,
    PredicateEvidence,
    PredicateTruth,
    ReevaluationTrigger,
)


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


def _disposition(
    disposition: Disposition = Disposition.ACTIONABLE,
    *,
    predicates: tuple[PredicateEvidence, ...] | None = None,
    evaluated_at: str = EVALUATED_AT,
    predecessor: str | None = None,
    trigger: ReevaluationTrigger | None = None,
    **overrides: object,
) -> LiveDecisionDisposition:
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
        "predecessor_disposition_id": predecessor,
        "reevaluation_trigger": trigger,
    }
    values.update(overrides)
    return LiveDecisionDisposition(**values)


class LiveDecisionDispositionTests(unittest.TestCase):
    def test_actionable_requires_every_predicate_proven_but_never_authorizes_execution(
        self,
    ) -> None:
        value = _disposition()
        self.assertIs(value.disposition, Disposition.ACTIONABLE)
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
        self.assertFalse(value.learning_outcome_authorized)

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

    def test_strict_wire_types_and_canonical_timestamps_fail_closed(
        self,
    ) -> None:
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

    def test_contract_contains_no_provider_credentials_or_money_fields(
        self,
    ) -> None:
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


if __name__ == "__main__":
    unittest.main()
