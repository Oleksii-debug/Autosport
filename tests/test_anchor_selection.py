import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.anchor_selection import (
    AnchorDecisionState,
    AnchorMetricRule,
    AnchorSelectionError,
    AnchorSelectionProtocol,
    AnchorSupplementalEvidence,
    AnchorSupplementalMetric,
    MetricDirection,
    evaluate_anchor_selection,
)
from autosport.sport_domain_fitness import (
    DomainProfile,
    EvidenceProvenance,
    EvidenceState,
    MetricEvidence,
    SportDomainFitnessObservation,
)


SHA = "a" * 64
T0 = "2026-01-01T00:00:00Z"
D1 = "2026-01-03T00:00:00Z"
D2 = "2026-01-20T00:00:00Z"
D3 = "2026-01-21T00:00:00Z"


def met(value, unit, state=EvidenceState.MEASURED):
    return MetricEvidence(
        state,
        None if value is None else Decimal(str(value)),
        unit,
    )


def observation(sport, oid, *, league="league-1", provider="provider-1", overrides=None):
    values = dict(
        observation_id=oid,
        sport_id=sport,
        league_id=league,
        market_id="match",
        provider_id=provider,
        measured_from=T0,
        measured_until=D1,
        available_at=D1,
        evidence_sha256=(SHA[:-1] + str(len(oid)))[:64],
        provenance=EvidenceProvenance.OBSERVED,
        domain_profile=DomainProfile.FAST,
        catalogue_coverage=met("0.9", "fraction"),
        quote_coverage=met("0.8", "fraction"),
        recurrence_per_hour=met("12", "events/hour"),
        freshness_seconds=met("1", "seconds"),
        reaction_slack_seconds=met("5", "seconds"),
        executable_liquidity=met("100", "units"),
        fee_fraction=met("0.01", "fraction"),
        slippage_fraction=met("0.01", "fraction"),
        capital_time_hours=met("0.25", "hours"),
        data_cost=met("0.10", "cost"),
        compute_cost=met("2", "cost"),
        compute_duration_seconds=met("2", "seconds"),
        slow_analysis_deadline_seconds=met("1", "seconds"),
        freshness_ttl_seconds=met("10", "seconds"),
    )
    if overrides:
        values.update(overrides)
    return SportDomainFitnessObservation(**values)


def supplemental(
    item,
    *,
    measured_until=D1,
    available_at=D1,
    provenance=EvidenceProvenance.OBSERVED,
    source_evidence_sha256="b" * 64,
):
    return AnchorSupplementalEvidence(
        observation_id=item.observation_id,
        evidence_sha256=item.evidence_sha256,
        source_evidence_sha256=source_evidence_sha256,
        measured_until=measured_until,
        available_at=available_at,
        provenance=provenance,
        calibration_error=AnchorSupplementalMetric(
            EvidenceState.MEASURED, Decimal("0.10"), "fraction"
        ),
        execution_feasibility=AnchorSupplementalMetric(
            EvidenceState.MEASURED, Decimal("0.90"), "fraction"
        ),
        settlement_identity_complexity=AnchorSupplementalMetric(
            EvidenceState.MEASURED, Decimal("1"), "complexity"
        ),
        oos_net_economic_value=AnchorSupplementalMetric(
            EvidenceState.MEASURED, Decimal("0.20"), "net_economic_value"
        ),
    )


def protocol(**overrides):
    values = dict(
        experiment_id="anchor-test",
        candidate_sports=("fast-sport", "slow-sport"),
        measurement_start=T0,
        measurement_end=D2,
        decision_as_of=D2,
        minimum_observations=2,
        minimum_effective_sample_size=2,
        max_missing_fraction=Decimal("0.25"),
        minimum_score=Decimal("0.10"),
        minimum_score_separation=Decimal("0.01"),
    )
    values.update(overrides)
    return AnchorSelectionProtocol(**values)


class AnchorSelectionTests(unittest.TestCase):
    def test_requires_frozen_sorted_unique_candidate_universe(self):
        with self.assertRaises(AnchorSelectionError):
            protocol(candidate_sports=("slow-sport", "fast-sport"))
        with self.assertRaises(AnchorSelectionError):
            protocol(candidate_sports=("fast-sport", "fast-sport"))

    def test_future_and_simulated_evidence_are_excluded(self):
        future = observation(
            "fast-sport", "future",
            overrides={
                "measured_until": D2,
                "available_at": D2,
            },
        )
        simulated = replace(
            observation("fast-sport", "sim"),
            observation_id="sim",
            provenance=EvidenceProvenance.SIMULATED,
        )
        report = evaluate_anchor_selection(
            [
                observation("fast-sport", "f1", league="league-1", provider="p1"),
                observation("fast-sport", "f2", league="league-2", provider="p2"),
                observation("slow-sport", "s1", league="league-1", provider="p1"),
                observation("slow-sport", "s2", league="league-2", provider="p2"),
                future,
                simulated,
            ],
            protocol(decision_as_of=D1),
        )
        self.assertEqual(report.decision_state, AnchorDecisionState.CHECKPOINT)
        ids = set(report.input_observation_ids)
        self.assertNotIn("future", ids)
        self.assertNotIn("sim", ids)

    def test_rejects_future_or_non_observed_supplemental_evidence(self):
        item = observation("fast-sport", "f1")
        with self.assertRaisesRegex(
            AnchorSelectionError, "not causally available"
        ):
            evaluate_anchor_selection(
                [item],
                protocol(),
                [supplemental(item, measured_until=D3, available_at=D3)],
            )
        with self.assertRaisesRegex(
            AnchorSelectionError, "observed provenance"
        ):
            evaluate_anchor_selection(
                [item],
                protocol(),
                [supplemental(item, provenance=EvidenceProvenance.SIMULATED)],
            )

    def test_rejects_supplemental_evidence_with_wrong_observation_hash(self):
        item = observation("fast-sport", "f1")
        bad = replace(supplemental(item), evidence_sha256="c" * 64)
        with self.assertRaisesRegex(
            AnchorSelectionError, "does not bind the observation evidence hash"
        ):
            evaluate_anchor_selection([item], protocol(), [bad])

    def test_protocol_cannot_omit_required_anchor_dimensions(self):
        rules = protocol().metrics
        with self.assertRaisesRegex(
            AnchorSelectionError, "preserve all required dimensions"
        ):
            protocol(metrics=rules[:-1])

    def test_supplemental_digest_binds_timing_provenance_and_values(self):
        item = observation("fast-sport", "f1")
        first = supplemental(item)
        changed = replace(first, available_at="2026-01-04T00:00:00Z")
        self.assertNotEqual(first.supplemental_sha256, changed.supplemental_sha256)

    def test_dependence_clusters_prevent_duplicate_inflation(self):
        duplicated = observation("fast-sport", "dup-2")
        normal = observation("fast-sport", "f2", league="league-2", provider="p2")
        report = evaluate_anchor_selection(
            [
                observation("fast-sport", "dup-1"),
                duplicated,
                normal,
                observation("slow-sport", "s1", league="league-1", provider="p1"),
                observation("slow-sport", "s2", league="league-2", provider="p2"),
            ],
            protocol(),
        )
        fast = next(item for item in report.candidate_reports if item.sport_id == "fast-sport")
        self.assertEqual(fast.raw_observation_count, 3)
        self.assertEqual(fast.effective_sample_size, 2)

    def test_missing_evidence_is_penalized_and_can_block_selection(self):
        missing = observation(
            "fast-sport", "missing",
            overrides={
                "quote_coverage": MetricEvidence(EvidenceState.UNKNOWN, None, "fraction"),
            },
        )
        complete_fast = observation("fast-sport", "fast-2", league="league-2", provider="p2")
        slow1 = observation("slow-sport", "slow-1", league="league-1", provider="p1")
        slow2 = observation("slow-sport", "slow-2", league="league-2", provider="p2")
        report = evaluate_anchor_selection(
            [missing, complete_fast, slow1, slow2],
            protocol(max_missing_fraction=Decimal("0.01")),
        )
        self.assertEqual(report.decision_state, AnchorDecisionState.INSUFFICIENT)
        fast = next(item for item in report.candidate_reports if item.sport_id == "fast-sport")
        self.assertGreater(fast.missing_fraction, Decimal("0"))

    def test_short_window_emits_checkpoint(self):
        p = protocol(
            measurement_end=D1,
            decision_as_of=D1,
            minimum_observations=2,
            minimum_effective_sample_size=2,
        )
        report = evaluate_anchor_selection(
            [
                observation("fast-sport", "f1", league="league-1", provider="p1"),
                observation("fast-sport", "f2", league="league-2", provider="p2"),
                observation("slow-sport", "s1", league="league-1", provider="p1"),
                observation("slow-sport", "s2", league="league-2", provider="p2"),
            ],
            p,
        )
        self.assertEqual(report.decision_state, AnchorDecisionState.CHECKPOINT)
        self.assertIsNone(report.selected_sport_id)

    def test_fourteen_day_winner_requires_clear_conservative_separation(self):
        fast = [
            observation("fast-sport", "f1", league="league-1", provider="p1"),
            observation("fast-sport", "f2", league="league-2", provider="p2"),
        ]
        slow = [
            observation("slow-sport", "s1", league="league-1", provider="p1", overrides={
                "catalogue_coverage": met("0.3", "fraction"),
                "quote_coverage": met("0.3", "fraction"),
                "recurrence_per_hour": met("2", "events/hour"),
                "freshness_seconds": met("8", "seconds"),
                "reaction_slack_seconds": met("2", "seconds"),
                "executable_liquidity": met("10", "units"),
                "fee_fraction": met("0.04", "fraction"),
                "slippage_fraction": met("0.04", "fraction"),
                "capital_time_hours": met("12", "hours"),
                "data_cost": met("8", "cost"),
                "compute_cost": met("8", "cost"),
                "compute_duration_seconds": met("8", "seconds"),
                "slow_analysis_deadline_seconds": met("2", "seconds"),
                "freshness_ttl_seconds": met("8", "seconds"),
            }),
            observation("slow-sport", "s2", league="league-2", provider="p2", overrides={
                "catalogue_coverage": met("0.3", "fraction"),
                "quote_coverage": met("0.3", "fraction"),
                "recurrence_per_hour": met("2", "events/hour"),
                "freshness_seconds": met("8", "seconds"),
                "reaction_slack_seconds": met("2", "seconds"),
                "executable_liquidity": met("10", "units"),
                "fee_fraction": met("0.04", "fraction"),
                "slippage_fraction": met("0.04", "fraction"),
                "capital_time_hours": met("12", "hours"),
                "data_cost": met("8", "cost"),
                "compute_cost": met("8", "cost"),
                "compute_duration_seconds": met("8", "seconds"),
                "slow_analysis_deadline_seconds": met("2", "seconds"),
                "freshness_ttl_seconds": met("8", "seconds"),
            }),
        ]
        report = evaluate_anchor_selection(fast + slow, protocol(minimum_score_separation=Decimal("0.01")))
        self.assertEqual(report.decision_state, AnchorDecisionState.SELECT)
        self.assertEqual(report.selected_sport_id, "fast-sport")

    def test_uncertain_fourteen_day_result_continues(self):
        p = protocol(minimum_score_separation=Decimal("0.90"))
        observations = [
            observation("fast-sport", "f1", league="league-1", provider="p1"),
            observation("fast-sport", "f2", league="league-2", provider="p2"),
            observation("slow-sport", "s1", league="league-1", provider="p1"),
            observation("slow-sport", "s2", league="league-2", provider="p2"),
        ]
        report = evaluate_anchor_selection(observations, p)
        self.assertEqual(report.decision_state, AnchorDecisionState.CONTINUE)
        self.assertIsNone(report.selected_sport_id)


if __name__ == "__main__":
    unittest.main()
