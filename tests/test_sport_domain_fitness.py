import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.sport_domain_fitness import (
    CausalView, DomainProfile, EvidenceProvenance, EvidenceState,
    MetricEvidence, RouteRecommendation, RouteStatus, SportDomainFitnessError,
    SportDomainFitnessObservation, SportDomainFitnessStore, recommend_route,
)


SHA = "a" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T1_PLUS_5 = "2026-01-02T00:00:05Z"
T1_PLUS_11 = "2026-01-02T00:00:11Z"


def met(value, unit, state=EvidenceState.MEASURED):
    return MetricEvidence(state, None if value is None else Decimal(str(value)), unit)


def make_observation(**overrides):
    values = dict(
        observation_id="obs-default", sport_id="table-tennis", league_id="league-1",
        market_id="match", provider_id="provider-1",
        measured_from=T0, measured_until=T1, available_at=T1,
        evidence_sha256=SHA, provenance=EvidenceProvenance.OBSERVED,
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
    values.update(overrides)
    return SportDomainFitnessObservation(**values)


class SportDomainFitnessTests(unittest.TestCase):
    def test_round_trip_restart_and_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fitness.json"
            store = SportDomainFitnessStore(path)
            obs = make_observation()
            self.assertTrue(store.add(obs))
            reopened = SportDomainFitnessStore(path)
            self.assertEqual(reopened.get(obs.observation_id), obs)
            self.assertEqual(
                reopened.recommend(
                    sport_id="table-tennis", league_id="league-1",
                    market_id="match", provider_id="provider-1", as_of=T1_PLUS_5,
                )[0].status,
                RouteStatus.ROUTE_BASELINE,
            )

    def test_duplicate_json_keys_fail_closed_before_last_wins_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fitness.json"
            store = SportDomainFitnessStore(path)
            store.add(make_observation(observation_id="duplicate-key"))

            original = path.read_text(encoding="utf-8")
            canonical = '  "schema": "autosport.sport_domain_fitness",'
            tampered = original.replace(
                canonical,
                '  "schema": "attacker-controlled",\n' + canonical,
                1,
            )
            self.assertNotEqual(tampered, original)
            path.write_text(tampered, encoding="utf-8")

            with self.assertRaisesRegex(
                SportDomainFitnessError,
                "cannot load fitness evidence store",
            ):
                SportDomainFitnessStore(path)


    def test_failed_publication_does_not_expose_live_uncommitted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fitness.json"
            store = SportDomainFitnessStore(path)
            obs = make_observation(observation_id="fault")
            with patch.object(store, "_persist", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.add(obs)
            self.assertIsNone(store.get("fault"))
            self.assertIsNone(SportDomainFitnessStore(path).get("fault"))

    def test_future_evidence_is_hidden_from_decision_view_but_research_can_inspect_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SportDomainFitnessStore(Path(tmp) / "fitness.json")
            store.add(make_observation(observation_id="future", measured_until=T2, available_at=T2))
            kwargs = dict(sport_id="table-tennis", league_id="league-1",
                          market_id="match", provider_id="provider-1", as_of=T1)
            self.assertEqual(store.lookup(**kwargs), ())
            self.assertEqual(
                len(store.lookup(**kwargs, view=CausalView.RESTATED_RESEARCH)), 0,
            )
            # Even retrospective route generation is blocked: live routing only
            # accepts decision-time evidence.
            kwargs["as_of"] = T2
            self.assertEqual(
                store.recommend(**kwargs, view=CausalView.RESTATED_RESEARCH)[0].status,
                RouteStatus.DO_NOT_ROUTE,
            )

    def test_simulation_can_never_become_a_live_route(self):
        obs = make_observation(observation_id="sim", provenance=EvidenceProvenance.SIMULATED)
        self.assertEqual(recommend_route(obs, as_of=T2).status, RouteStatus.DO_NOT_ROUTE)

    def test_metric_states_are_explicit_and_fail_closed(self):
        for state in (EvidenceState.UNKNOWN, EvidenceState.UNAVAILABLE, EvidenceState.INSUFFICIENT):
            with self.subTest(state=state):
                obs = make_observation(
                    observation_id=state.value,
                    executable_liquidity=MetricEvidence(state, None, "units"),
                )
                self.assertEqual(
                    recommend_route(obs, as_of=T1_PLUS_5).status,
                    RouteStatus.INSUFFICIENT_EVIDENCE,
                )

    def test_nonfinite_negative_fraction_and_impossible_time_are_rejected(self):
        with self.assertRaises(SportDomainFitnessError):
            MetricEvidence(EvidenceState.MEASURED, Decimal("-1"), "seconds")
        with self.assertRaises(SportDomainFitnessError):
            MetricEvidence(EvidenceState.MEASURED, Decimal("NaN"), "seconds")
        with self.assertRaises(SportDomainFitnessError):
            make_observation(catalogue_coverage=met("1.1", "fraction"))
        with self.assertRaises(SportDomainFitnessError):
            make_observation(freshness_ttl_seconds=met("0", "seconds"))

    def test_stale_reaction_slack_and_zero_coverage_fail_closed(self):
        stale = make_observation(observation_id="stale", freshness_seconds=met("11", "seconds"))
        zero = make_observation(observation_id="zero", quote_coverage=met("0", "fraction"))
        self.assertEqual(recommend_route(stale, as_of=T1).status, RouteStatus.DO_NOT_ROUTE)
        self.assertEqual(recommend_route(zero, as_of=T1_PLUS_5).status, RouteStatus.DO_NOT_ROUTE)

    def test_evidence_age_is_causally_bound_to_decision_boundary(self):
        old_but_intrinsically_fresh = make_observation(
            observation_id="old-but-fresh",
            freshness_seconds=met("1", "seconds"),
            freshness_ttl_seconds=met("10", "seconds"),
        )
        self.assertEqual(
            recommend_route(old_but_intrinsically_fresh, as_of=T1_PLUS_5).status,
            RouteStatus.ROUTE_BASELINE,
        )
        self.assertEqual(
            recommend_route(old_but_intrinsically_fresh, as_of=T1_PLUS_11).status,
            RouteStatus.DO_NOT_ROUTE,
        )

        delayed_publication = make_observation(
            observation_id="delayed-publication",
            available_at=T1_PLUS_11,
            freshness_seconds=met("1", "seconds"),
            freshness_ttl_seconds=met("10", "seconds"),
        )
        self.assertEqual(
            recommend_route(delayed_publication, as_of=T1_PLUS_11).status,
            RouteStatus.DO_NOT_ROUTE,
        )

    def test_route_contract_cannot_expand_into_model_or_money_authority(self):
        route = recommend_route(make_observation(observation_id="bounded-route"), as_of=T1_PLUS_5)
        self.assertEqual(
            set(RouteRecommendation.__dataclass_fields__),
            {"status", "reason", "observation_id", "domain_profile"},
        )
        self.assertFalse(hasattr(route, "model_id"))
        self.assertFalse(hasattr(route, "stake"))
        self.assertFalse(hasattr(route, "execution_authorized"))

    def test_simulated_evidence_cannot_be_reintroduced_as_observed_under_new_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SportDomainFitnessStore(Path(tmp) / "fitness.json")
            simulated = make_observation(
                observation_id="simulated",
                provenance=EvidenceProvenance.SIMULATED,
            )
            self.assertTrue(store.add(simulated))
            observed = replace(
                simulated,
                observation_id="observed-relabel",
                provenance=EvidenceProvenance.OBSERVED,
            )
            with self.assertRaisesRegex(SportDomainFitnessError, "provenance"):
                store.add(observed)
            reopened = SportDomainFitnessStore(Path(tmp) / "fitness.json")
            with self.assertRaisesRegex(SportDomainFitnessError, "provenance"):
                reopened.add(observed)

            with tempfile.TemporaryDirectory() as second_tmp:
                reverse = SportDomainFitnessStore(Path(second_tmp) / "fitness.json")
                observed_first = make_observation(observation_id="observed-first")
                self.assertTrue(reverse.add(observed_first))
                simulated_later = replace(
                    observed_first,
                    observation_id="simulated-later",
                    provenance=EvidenceProvenance.SIMULATED,
                )
                with self.assertRaisesRegex(SportDomainFitnessError, "provenance"):
                    reverse.add(simulated_later)

    def test_equivalent_time_spelling_cannot_bypass_provenance_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SportDomainFitnessStore(Path(tmp) / "fitness.json")
            simulated = make_observation(
                observation_id="sim-z",
                provenance=EvidenceProvenance.SIMULATED,
            )
            store.add(simulated)
            observed = replace(
                simulated,
                observation_id="obs-z",
                provenance=EvidenceProvenance.OBSERVED,
                measured_from="2025-12-31T19:00:00-05:00",
                measured_until="2026-01-01T19:00:00-05:00",
            )
            with self.assertRaisesRegex(SportDomainFitnessError, "provenance"):
                store.add(observed)

    def test_slow_route_uses_duration_not_cost_and_requires_measured_budget(self):
        slow = make_observation(
            observation_id="slow", domain_profile=DomainProfile.SLOW,
            reaction_slack_seconds=met("5", "seconds"),
            compute_cost=met("200", "cost"),
            compute_duration_seconds=met("2", "seconds"),
            slow_analysis_deadline_seconds=met("2", "seconds"),
        )
        self.assertEqual(recommend_route(slow, as_of=T1_PLUS_5).status, RouteStatus.ROUTE_SLOW_RESEARCH)
        too_slow = replace(
            slow, observation_id="too-slow",
            compute_duration_seconds=met("4", "seconds"),
        )
        self.assertEqual(recommend_route(too_slow, as_of=T1_PLUS_5).status, RouteStatus.ROUTE_BASELINE)
        too_tight = replace(
            slow, observation_id="too-tight",
            reaction_slack_seconds=met("1", "seconds"),
        )
        self.assertEqual(recommend_route(too_tight, as_of=T1_PLUS_5).status, RouteStatus.ROUTE_BASELINE)

    def test_conflicting_immutable_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SportDomainFitnessStore(Path(tmp) / "fitness.json")
            obs = make_observation(observation_id="immutable")
            store.add(obs)
            conflicting = replace(obs, quote_coverage=met("0.6", "fraction"))
            with self.assertRaises(SportDomainFitnessError):
                store.add(conflicting)

    def test_observed_vs_simulated_is_part_of_immutable_payload(self):
        observed = make_observation(observation_id="prov")
        simulated = replace(observed, provenance=EvidenceProvenance.SIMULATED)
        self.assertNotEqual(observed.payload()["provenance"], simulated.payload()["provenance"])

    def test_decision_boundary_rejects_pre_availability_evidence(self):
        obs = make_observation(observation_id="future2", available_at=T2, measured_until=T1)
        self.assertEqual(recommend_route(obs, as_of=T1).status, RouteStatus.DO_NOT_ROUTE)

    def test_payload_is_json_safe_and_decimal_exact(self):
        payload = make_observation().payload()
        encoded = json.dumps(payload, sort_keys=True)
        self.assertIn('"2"', encoded)
        self.assertNotIn("2.0", encoded)


if __name__ == "__main__":
    unittest.main()
