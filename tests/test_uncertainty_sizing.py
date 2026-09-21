from __future__ import annotations

import copy
import unittest
from decimal import Decimal

from autosport.uncertainty_sizing import (
    SizingAction,
    UncertaintySizingError,
    UncertaintySizingEvidence,
    UncertaintySizingPolicy,
    UncertaintySizingRequest,
    evaluate_uncertainty_sizing,
)


class UncertaintySizingTests(unittest.TestCase):
    QUOTE_SHA = "a" * 64
    CALIBRATION_SHA = "b" * 64

    @classmethod
    def _evidence(cls, **overrides) -> UncertaintySizingEvidence:
        values = {
            "evidence_id": "uncertainty-evidence-001",
            "candidate_id": "candidate-001",
            "quote_sha256": cls.QUOTE_SHA,
            "probability_model_version_id": "prob-model-v7",
            "calibration_bundle_sha256": cls.CALIBRATION_SHA,
            "causal_cutoff": "2026-09-21T08:00:00+00:00",
            "produced_at": "2026-09-21T08:01:00+00:00",
            "valid_until": "2026-09-21T08:06:00+00:00",
            "probability_lower": Decimal("0.55"),
            "probability_point": Decimal("0.60"),
            "probability_upper": Decimal("0.65"),
            "net_win_profit_per_stake": Decimal("1"),
            "evidence_refs": (
                "evidence://calibration/prob-model-v7",
                "evidence://quote/candidate-001",
            ),
        }
        values.update(overrides)
        return UncertaintySizingEvidence(**values)

    @classmethod
    def _request(cls, **overrides) -> UncertaintySizingRequest:
        values = {
            "candidate_id": "candidate-001",
            "quote_sha256": cls.QUOTE_SHA,
            "decision_ts": "2026-09-21T08:02:00+00:00",
            "bankroll_id": "paper-bankroll",
            "currency": "EUR",
            "bankroll": Decimal("1000"),
        }
        values.update(overrides)
        return UncertaintySizingRequest(**values)

    @staticmethod
    def _policy(**overrides) -> UncertaintySizingPolicy:
        values = {
            "fractional_kelly": Decimal("0.25"),
            "max_bankroll_fraction": Decimal("0.05"),
            "max_uncertainty_width": Decimal("0.20"),
            "min_conservative_ev_per_stake": Decimal("0"),
        }
        values.update(overrides)
        return UncertaintySizingPolicy(**values)

    def test_valid_evidence_uses_lower_bound_for_conservative_fractional_kelly(self) -> None:
        decision = evaluate_uncertainty_sizing(
            self._evidence(),
            self._request(),
            self._policy(),
        )

        self.assertEqual(decision.action, SizingAction.ELIGIBLE)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.conservative_ev_per_stake, Decimal("0.10"))
        self.assertEqual(
            decision.conservative_full_kelly_fraction,
            Decimal("0.10"),
        )
        self.assertEqual(decision.stake_fraction_ceiling, Decimal("0.0250"))
        self.assertEqual(decision.stake_ceiling, Decimal("25.0000"))
        self.assertEqual(len(decision.evidence_fingerprint_sha256), 64)

    def test_bankroll_fraction_cap_tightens_positive_kelly_size(self) -> None:
        evidence = self._evidence(
            probability_lower=Decimal("0.80"),
            probability_point=Decimal("0.85"),
            probability_upper=Decimal("0.90"),
        )
        decision = evaluate_uncertainty_sizing(
            evidence,
            self._request(),
            self._policy(
                fractional_kelly=Decimal("0.50"),
                max_bankroll_fraction=Decimal("0.03"),
            ),
        )
        self.assertEqual(decision.action, SizingAction.ELIGIBLE)
        self.assertEqual(decision.conservative_ev_per_stake, Decimal("0.60"))
        self.assertEqual(decision.conservative_full_kelly_fraction, Decimal("0.60"))
        self.assertEqual(decision.stake_fraction_ceiling, Decimal("0.03"))
        self.assertEqual(decision.stake_ceiling, Decimal("30.00"))

    def test_break_even_or_below_threshold_abstains(self) -> None:
        break_even = self._evidence(
            probability_lower=Decimal("0.50"),
            probability_point=Decimal("0.55"),
            probability_upper=Decimal("0.60"),
        )
        decision = evaluate_uncertainty_sizing(
            break_even,
            self._request(),
            self._policy(),
        )
        self.assertEqual(decision.action, SizingAction.ABSTAIN)
        self.assertEqual(decision.reasons, ("insufficient_conservative_edge",))
        self.assertEqual(decision.stake_ceiling, Decimal("0"))

        positive_but_below_owner_cushion = self._evidence(
            probability_lower=Decimal("0.53"),
            probability_point=Decimal("0.56"),
            probability_upper=Decimal("0.59"),
        )
        decision = evaluate_uncertainty_sizing(
            positive_but_below_owner_cushion,
            self._request(),
            self._policy(min_conservative_ev_per_stake=Decimal("0.07")),
        )
        self.assertEqual(decision.action, SizingAction.ABSTAIN)
        self.assertEqual(decision.conservative_ev_per_stake, Decimal("0.06"))
        self.assertEqual(decision.reasons, ("insufficient_conservative_edge",))

    def test_wide_uncertainty_abstains_even_when_point_estimate_is_attractive(self) -> None:
        evidence = self._evidence(
            probability_lower=Decimal("0.51"),
            probability_point=Decimal("0.75"),
            probability_upper=Decimal("0.90"),
        )
        decision = evaluate_uncertainty_sizing(
            evidence,
            self._request(),
            self._policy(max_uncertainty_width=Decimal("0.20")),
        )
        self.assertEqual(decision.action, SizingAction.ABSTAIN)
        self.assertEqual(decision.reasons, ("uncertainty_too_wide",))
        self.assertEqual(decision.stake_ceiling, Decimal("0"))

    def test_stale_evidence_fails_closed_at_exact_validity_boundary(self) -> None:
        evidence = self._evidence()
        before = evaluate_uncertainty_sizing(
            evidence,
            self._request(decision_ts="2026-09-21T08:05:59+00:00"),
            self._policy(),
        )
        self.assertEqual(before.action, SizingAction.ELIGIBLE)

        at_boundary = evaluate_uncertainty_sizing(
            evidence,
            self._request(decision_ts=evidence.valid_until),
            self._policy(),
        )
        self.assertEqual(at_boundary.action, SizingAction.ABSTAIN)
        self.assertIn("evidence_expired", at_boundary.reasons)

    def test_future_evidence_and_identity_mismatch_fail_closed(self) -> None:
        evidence = self._evidence()
        result = evaluate_uncertainty_sizing(
            evidence,
            self._request(
                candidate_id="other-candidate",
                quote_sha256="c" * 64,
                decision_ts="2026-09-21T08:00:30+00:00",
            ),
            self._policy(),
        )
        self.assertEqual(result.action, SizingAction.ABSTAIN)
        self.assertEqual(
            result.reasons,
            (
                "candidate_identity_mismatch",
                "quote_identity_mismatch",
                "evidence_not_yet_produced",
            ),
        )
        self.assertEqual(result.stake_fraction_ceiling, Decimal("0"))

    def test_probability_interval_and_payoff_are_exact_and_bounded(self) -> None:
        with self.assertRaisesRegex(UncertaintySizingError, "probability interval"):
            self._evidence(
                probability_lower=Decimal("0.61"),
                probability_point=Decimal("0.60"),
            )
        with self.assertRaisesRegex(UncertaintySizingError, "probability interval"):
            self._evidence(probability_upper=Decimal("1.01"))
        with self.assertRaisesRegex(UncertaintySizingError, "finite exact Decimal"):
            self._evidence(probability_lower=0.55)
        with self.assertRaisesRegex(UncertaintySizingError, "strictly positive"):
            self._evidence(net_win_profit_per_stake=Decimal("0"))
        with self.assertRaisesRegex(UncertaintySizingError, "strictly positive"):
            self._request(bankroll=Decimal("0"))

    def test_temporal_and_evidence_provenance_are_canonical(self) -> None:
        with self.assertRaisesRegex(UncertaintySizingError, "must not exceed produced_at"):
            self._evidence(causal_cutoff="2026-09-21T08:01:01+00:00")
        with self.assertRaisesRegex(UncertaintySizingError, "strictly after produced_at"):
            self._evidence(valid_until="2026-09-21T08:01:00+00:00")
        with self.assertRaisesRegex(UncertaintySizingError, "UTC \+00:00"):
            self._evidence(produced_at="2026-09-21T10:01:00+02:00")
        with self.assertRaisesRegex(UncertaintySizingError, "sorted and unique"):
            self._evidence(
                evidence_refs=("evidence://z", "evidence://a"),
            )
        with self.assertRaisesRegex(UncertaintySizingError, "must not be empty"):
            self._evidence(evidence_refs=())

    def test_strict_json_round_trip_rejects_numeric_or_boolean_schema_aliases(self) -> None:
        evidence = self._evidence()
        payload = evidence.to_dict()
        restored = UncertaintySizingEvidence.from_dict(copy.deepcopy(payload))
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.fingerprint_sha256, evidence.fingerprint_sha256)

        bool_schema = dict(payload)
        bool_schema["schema_version"] = True
        with self.assertRaisesRegex(UncertaintySizingError, "unsupported"):
            UncertaintySizingEvidence.from_dict(bool_schema)

        float_schema = dict(payload)
        float_schema["schema_version"] = 1.0
        with self.assertRaisesRegex(UncertaintySizingError, "unsupported"):
            UncertaintySizingEvidence.from_dict(float_schema)

        numeric_probability = dict(payload)
        numeric_probability["probability_lower"] = 0.55
        with self.assertRaisesRegex(UncertaintySizingError, "decimal JSON string"):
            UncertaintySizingEvidence.from_dict(numeric_probability)

        extra = dict(payload)
        extra["execution_authorized"] = True
        with self.assertRaisesRegex(UncertaintySizingError, "exactly canonical fields"):
            UncertaintySizingEvidence.from_dict(extra)

    def test_fingerprint_changes_when_uncertainty_or_quote_economics_change(self) -> None:
        base = self._evidence()
        changed_interval = self._evidence(probability_lower=Decimal("0.54"))
        changed_payoff = self._evidence(net_win_profit_per_stake=Decimal("0.99"))
        changed_quote = self._evidence(quote_sha256="c" * 64)

        self.assertNotEqual(base.fingerprint_sha256, changed_interval.fingerprint_sha256)
        self.assertNotEqual(base.fingerprint_sha256, changed_payoff.fingerprint_sha256)
        self.assertNotEqual(base.fingerprint_sha256, changed_quote.fingerprint_sha256)

    def test_policy_is_strict_and_result_cannot_mint_execution_or_release_truth(self) -> None:
        with self.assertRaisesRegex(UncertaintySizingError, "fractional_kelly"):
            self._policy(fractional_kelly=Decimal("0"))
        with self.assertRaisesRegex(UncertaintySizingError, "max_bankroll_fraction"):
            self._policy(max_bankroll_fraction=Decimal("1.01"))
        with self.assertRaisesRegex(UncertaintySizingError, "max_uncertainty_width"):
            self._policy(max_uncertainty_width=Decimal("1.01"))
        with self.assertRaisesRegex(UncertaintySizingError, "non-negative"):
            self._policy(min_conservative_ev_per_stake=Decimal("-0.01"))

        decision = evaluate_uncertainty_sizing(
            self._evidence(),
            self._request(),
            self._policy(),
        )
        self.assertFalse(hasattr(decision, "execution_authorized"))
        self.assertFalse(hasattr(decision, "real_money_execution"))
        self.assertFalse(hasattr(decision, "human_tested"))
        self.assertFalse(hasattr(decision, "nvda_verified"))
        self.assertFalse(hasattr(decision, "v1_ready"))
        self.assertFalse(hasattr(decision, "whole_product_complete"))


if __name__ == "__main__":
    unittest.main()
