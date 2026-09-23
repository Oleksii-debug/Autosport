from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from fractions import Fraction
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    REQUIRED_BASELINE_KINDS,
    canonical_evaluation_contract,
)
from autosport.market_implied_baseline import (
    METHOD_ID,
    MarketImpliedBaselineCohortEvidence,
    MarketImpliedBaselineError,
    MarketImpliedBaselineEvidence,
    bind_market_implied_baseline_cohort,
    build_market_implied_baseline_evidence,
    market_implied_baseline_config_sha256,
    market_implied_evidence_manifest_sha256,
)
from autosport.market_mirror import MarketMirror
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.opportunity import StrategyClass
from autosport.storage import SQLiteMarketStore


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class MarketImpliedBaselineTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc)
    SOURCE_ID = "betfair_exchange_historical"
    SPORT = "table_tennis"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteMarketStore(Path(self.directory.name) / "market.db")
        self.addCleanup(self.store.close)

    def authority(
        self,
        *,
        event_id: str = "event-1",
        observed_at: str = "2026-09-18T15:00:01Z",
        publish_at: str = "2026-09-18T15:00:00Z",
    ) -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition={
                "eventId": event_id,
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [
                    {"id": "away"},
                    {"id": "draw"},
                    {"id": "home"},
                ],
            },
            provider_publish_at=publish_at,
            observed_at=observed_at,
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority  # type: ignore[return-value]

    def event(
        self,
        selection_id: str,
        odds: str,
        sequence: int,
        *,
        event_id: str = "event-1",
        observed_ts: str = "2026-09-18T15:04:00Z",
        ingest_ts: str | None = None,
        source_ts: str | None = "2026-09-18T15:04:00Z",
        status: str = "open",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id="match_odds",
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            ingest_ts=ingest_ts or observed_ts,
            source_id=self.SOURCE_ID,
            sequence=sequence,
            market_type=MarketType.WINNER,
            status=status,
            source_ts=source_ts,
            sport=self.SPORT,
        )

    def persist(
        self,
        rows: tuple[tuple[str, str], ...] = (
            ("away", "3.00"),
            ("draw", "3.00"),
            ("home", "3.00"),
        ),
        *,
        observed_ts: str = "2026-09-18T15:04:00Z",
        source_ts: str | None = "2026-09-18T15:04:00Z",
    ) -> None:
        mirror = MarketMirror()
        for sequence, (selection_id, odds) in enumerate(rows, start=1):
            mirror.persist_and_apply(
                self.store,
                self.event(
                    selection_id,
                    odds,
                    sequence,
                    observed_ts=observed_ts,
                    source_ts=source_ts,
                ),
            )

    def evidence(
        self,
        *,
        authority: MarketSettlementOutcomeAuthority | None = None,
        cutoff: datetime | None = None,
        max_age: timedelta = timedelta(minutes=10),
    ) -> MarketImpliedBaselineEvidence:
        return build_market_implied_baseline_evidence(
            cohort_key="row-1",
            store=self.store,
            outcome_authority=authority or self.authority(),
            decision_cutoff=cutoff or self.CUTOFF,
            max_age=max_age,
        )

    def test_complete_roster_yields_exact_probability_vector_without_authority_widening(self) -> None:
        self.persist()
        evidence = self.evidence()

        self.assertEqual(
            tuple((p.selection_id, p.numerator, p.denominator) for p in evidence.probabilities),
            (("away", 1, 3), ("draw", 1, 3), ("home", 1, 3)),
        )
        self.assertEqual((evidence.overround_numerator, evidence.overround_denominator), (1, 1))
        self.assertEqual(evidence.to_dict()["method_id"], METHOD_ID)
        self.assertTrue(all(q.market_snapshot_hash is None for q in evidence.quotes))
        self.assertEqual(len(evidence.quote_snapshot_sha256), 64)
        truth = evidence.to_dict()["truth"]
        self.assertTrue(truth["forecast_comparator_only"])
        self.assertFalse(truth["source_stream_continuity_proven"])
        self.assertFalse(truth["execution_authority"])
        self.assertFalse(truth["promotion_authority"])
        self.assertFalse(truth["real_money_execution"])

    def test_nontrivial_devig_is_exact_and_independent_of_decimal_context(self) -> None:
        self.persist((("away", "2.10"), ("draw", "3.40"), ("home", "4.20")))
        with localcontext() as context:
            context.prec = 6
            low_precision = self.evidence()
        with localcontext() as context:
            context.prec = 50
            high_precision = self.evidence()

        self.assertEqual(
            tuple((p.numerator, p.denominator) for p in low_precision.probabilities),
            tuple((p.numerator, p.denominator) for p in high_precision.probabilities),
        )
        self.assertEqual(low_precision.evidence_sha256, high_precision.evidence_sha256)
        self.assertEqual(
            sum((p.fraction for p in low_precision.probabilities), start=Fraction(0, 1)),
            Fraction(1, 1),
        )

    def test_missing_three_way_selection_cannot_be_silently_renormalized(self) -> None:
        self.persist((("away", "2.10"), ("home", "2.20")))
        with self.assertRaisesRegex(
            MarketImpliedBaselineError,
            "complete fresh open quote set is unavailable",
        ):
            self.evidence()

    def test_replay_excludes_later_closing_quote(self) -> None:
        self.persist()
        mirror = MarketMirror.from_store(self.store)
        mirror.persist_and_apply(
            self.store,
            self.event(
                "home",
                "1.20",
                100,
                observed_ts="2026-09-18T15:06:00Z",
                ingest_ts="2026-09-18T15:06:00Z",
                source_ts="2026-09-18T15:06:00Z",
            ),
        )

        evidence = self.evidence()
        home = next(q for q in evidence.quotes if q.selection_id == "home")
        self.assertEqual(home.decimal_odds, Decimal("3.00"))

    def test_stale_future_or_suspended_quote_makes_row_unavailable(self) -> None:
        self.persist(
            observed_ts="2026-09-18T14:40:00Z",
            source_ts="2026-09-18T14:40:00Z",
        )
        with self.assertRaisesRegex(MarketImpliedBaselineError, "complete fresh open"):
            self.evidence(max_age=timedelta(minutes=5))

        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        future_store = SQLiteMarketStore(Path(other_dir.name) / "future.db")
        self.addCleanup(future_store.close)
        old_store = self.store
        self.store = future_store
        try:
            self.persist(source_ts="2026-09-18T15:06:00Z")
            with self.assertRaisesRegex(MarketImpliedBaselineError, "complete fresh open"):
                self.evidence()
        finally:
            self.store = old_store

        suspended_dir = tempfile.TemporaryDirectory()
        self.addCleanup(suspended_dir.cleanup)
        suspended_store = SQLiteMarketStore(Path(suspended_dir.name) / "suspended.db")
        self.addCleanup(suspended_store.close)
        self.store = suspended_store
        try:
            mirror = MarketMirror()
            for sequence, selection in enumerate(("away", "draw", "home"), start=1):
                mirror.persist_and_apply(
                    self.store,
                    self.event(
                        selection,
                        "3.00",
                        sequence,
                        status="suspended" if selection == "home" else "open",
                    ),
                )
            with self.assertRaisesRegex(MarketImpliedBaselineError, "complete fresh open"):
                self.evidence()
        finally:
            self.store = old_store

    def test_verified_roster_must_be_causally_available_by_cutoff(self) -> None:
        self.persist()
        authority = self.authority(
            publish_at="2026-09-18T15:06:00Z",
            observed_at="2026-09-18T15:06:01Z",
        )
        with self.assertRaisesRegex(
            MarketImpliedBaselineError,
            "roster is not causally available",
        ):
            self.evidence(authority=authority)

    def test_provider_market_identity_cannot_substitute_for_verified_roster(self) -> None:
        self.persist()
        with self.assertRaisesRegex(MarketImpliedBaselineError, "complete fresh open"):
            self.evidence(authority=self.authority(event_id="foreign-event"))

    def test_identity_changes_with_cutoff_and_verified_roster_revision(self) -> None:
        self.persist()
        first = self.evidence()
        later = self.evidence(
            cutoff=datetime(2026, 9, 18, 15, 5, 30, tzinfo=timezone.utc)
        )
        revised = self.evidence(
            authority=self.authority(observed_at="2026-09-18T15:00:02Z")
        )
        self.assertNotEqual(first.evidence_sha256, later.evidence_sha256)
        self.assertNotEqual(first.evidence_sha256, revised.evidence_sha256)

    def test_serialized_evidence_requires_independent_rebuild_and_rejects_tamper(self) -> None:
        self.persist()
        verified = self.evidence()
        raw = verified.to_dict()

        with self.assertRaisesRegex(
            MarketImpliedBaselineError,
            "requires independently rebuilt",
        ):
            MarketImpliedBaselineEvidence.from_dict(raw)

        restored = MarketImpliedBaselineEvidence.from_dict(
            raw,
            verified_evidence=self.evidence(),
        )
        self.assertEqual(restored.evidence_sha256, verified.evidence_sha256)

        tampered = copy.deepcopy(raw)
        tampered["truth"]["execution_authority"] = True
        with self.assertRaisesRegex(MarketImpliedBaselineError, "does not match rebuilt"):
            MarketImpliedBaselineEvidence.from_dict(
                tampered,
                verified_evidence=self.evidence(),
            )

    def definitions(
        self,
        *,
        market_config: str | None = None,
    ) -> tuple[BaselineDefinition, ...]:
        values = []
        for kind in REQUIRED_BASELINE_KINDS:
            supported = kind is BaselineKind.MARKET_IMPLIED_DEVIG
            config = (
                (market_config or market_implied_baseline_config_sha256())
                if supported
                else _hash("config:" + kind.value)
            )
            values.append(
                BaselineDefinition(
                    kind=kind,
                    baseline_id="baseline:" + kind.value,
                    implementation_sha256=_hash("impl:" + kind.value),
                    config_sha256=config,
                    supported=supported,
                    unsupported_reason=None if supported else "not supported in fixture",
                )
            )
        return tuple(values)

    def protocol(
        self,
        row: MarketImpliedBaselineEvidence,
        *,
        cohort_keys: tuple[str, ...] = ("row-1",),
        market_config: str | None = None,
        family: EvaluationContractFamily = EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
    ) -> FrozenBaselineProtocol:
        contract = canonical_evaluation_contract(family)
        strategy = (
            StrategyClass.PREDICTIVE_EDGE
            if family is EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
            else StrategyClass.LIVE_PRICE_MOVEMENT
        )
        return FrozenBaselineProtocol(
            protocol_id="market-implied-test",
            frozen_at="2026-09-18T14:30:00Z",
            evidence_scope=FrozenEvidenceScope(
                dataset_sha256=_hash("dataset"),
                dataset_cutoff="2026-09-18T14:00:00Z",
                cohort_keys=cohort_keys,
                market_evidence_sha256=market_implied_evidence_manifest_sha256((row,)),
                outcome_evidence_sha256=_hash("outcomes"),
                cost_model_sha256=_hash("cost"),
                execution_model_sha256=_hash("execution"),
            ),
            candidate_id="candidate",
            candidate_artifact_sha256=_hash("candidate"),
            strategy_class=strategy,
            evaluation_contract_family=family,
            evaluation_semantics=contract["evaluation_semantics"],
            evaluation_contract_sha256=contract["evaluation_contract_sha256"],
            primary_metric=contract["primary_metric"],
            uncertainty_method=contract["uncertainty_method"],
            baselines=self.definitions(market_config=market_config),
        )

    def market_definition(self, protocol: FrozenBaselineProtocol) -> BaselineDefinition:
        return next(
            item
            for item in protocol.baselines
            if item.kind is BaselineKind.MARKET_IMPLIED_DEVIG
        )

    def test_protocol_binding_freezes_exact_manifest_cohort_and_devig_method(self) -> None:
        self.persist()
        row = self.evidence()
        protocol = self.protocol(row)
        bound = bind_market_implied_baseline_cohort(
            protocol=protocol,
            baseline_definition=self.market_definition(protocol),
            evidence=(row,),
        )
        self.assertEqual(
            bound.market_evidence_sha256,
            protocol.evidence_scope.market_evidence_sha256,
        )
        self.assertEqual(bound.row_evidence_sha256, (row.evidence_sha256,))
        truth = bound.to_dict()["truth"]
        self.assertFalse(truth["same_frozen_cohort"])
        self.assertTrue(truth["same_frozen_cohort_labels"])
        self.assertFalse(truth["canonical_evaluation_universe_bound"])
        self.assertFalse(truth["metric_computed"])
        self.assertFalse(truth["promotion_authority"])

        changed = self.protocol(row, market_config=_hash("post-hoc-devig-choice"))
        with self.assertRaisesRegex(MarketImpliedBaselineError, "built-in frozen method"):
            bind_market_implied_baseline_cohort(
                protocol=changed,
                baseline_definition=self.market_definition(changed),
                evidence=(row,),
            )

    def test_protocol_binding_rejects_dropped_row_and_execution_relabelling(self) -> None:
        self.persist()
        row = self.evidence()
        missing = self.protocol(row, cohort_keys=("row-1", "row-2"))
        with self.assertRaisesRegex(MarketImpliedBaselineError, "exact frozen cohort"):
            bind_market_implied_baseline_cohort(
                protocol=missing,
                baseline_definition=self.market_definition(missing),
                evidence=(row,),
            )

        live = self.protocol(row, family=EvaluationContractFamily.LIVE_PRICE_EXECUTION)
        with self.assertRaisesRegex(MarketImpliedBaselineError, "forecast-only"):
            bind_market_implied_baseline_cohort(
                protocol=live,
                baseline_definition=self.market_definition(live),
                evidence=(row,),
            )

    def test_cohort_readback_requires_fresh_requalification(self) -> None:
        self.persist()
        row = self.evidence()
        protocol = self.protocol(row)
        definition = self.market_definition(protocol)
        verified = bind_market_implied_baseline_cohort(
            protocol=protocol,
            baseline_definition=definition,
            evidence=(row,),
        )
        raw = verified.to_dict()

        with self.assertRaisesRegex(
            MarketImpliedBaselineError,
            "requires independently rebuilt",
        ):
            MarketImpliedBaselineCohortEvidence.from_dict(raw)

        rebuilt_row = self.evidence()
        rebuilt = bind_market_implied_baseline_cohort(
            protocol=protocol,
            baseline_definition=definition,
            evidence=(rebuilt_row,),
        )
        restored = MarketImpliedBaselineCohortEvidence.from_dict(
            raw,
            verified_evidence=rebuilt,
        )
        self.assertEqual(restored.evidence_sha256, verified.evidence_sha256)


if __name__ == "__main__":
    unittest.main()
