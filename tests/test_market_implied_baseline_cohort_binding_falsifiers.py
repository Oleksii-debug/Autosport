from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    MarketImpliedBaselineError,
    bind_market_implied_baseline_cohort,
    build_market_implied_baseline_evidence,
    market_implied_baseline_config_sha256,
    market_implied_evidence_manifest_sha256,
)
from autosport.market_mirror import MarketMirror
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.opportunity import StrategyClass
from autosport.storage import SQLiteMarketStore


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class MarketImpliedCohortBindingFalsifiers(unittest.TestCase):
    cutoff = datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc)
    source_id = "betfair_exchange_historical"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _store(self, name: str) -> SQLiteMarketStore:
        store = SQLiteMarketStore(Path(self.directory.name) / f"{name}.db")
        self.addCleanup(store.close)
        return store

    def _authority(self, event_id: str):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition={
                "eventId": event_id,
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": "away"}, {"id": "draw"}, {"id": "home"}],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def _persist_market(self, store: SQLiteMarketStore, event_id: str) -> None:
        mirror = MarketMirror()
        for sequence, (selection_id, odds) in enumerate(
            (("away", "2.80"), ("draw", "3.20"), ("home", "2.50")),
            start=1,
        ):
            mirror.persist_and_apply(
                store,
                MarketEvent(
                    event_id=event_id,
                    market_id="match_odds",
                    selection_id=selection_id,
                    decimal_odds=Decimal(odds),
                    observed_ts="2026-09-18T15:04:00Z",
                    ingest_ts="2026-09-18T15:04:00Z",
                    source_id=self.source_id,
                    sequence=sequence,
                    market_type=MarketType.WINNER,
                    status="open",
                    source_ts="2026-09-18T15:04:00Z",
                    sport="table_tennis",
                ),
            )

    def _evidence(self, *, cohort_key: str, event_id: str):
        store = self._store(event_id)
        self._persist_market(store, event_id)
        return build_market_implied_baseline_evidence(
            cohort_key=cohort_key,
            store=store,
            outcome_authority=self._authority(event_id),
            decision_cutoff=self.cutoff,
            max_age=timedelta(minutes=10),
        )

    @staticmethod
    def _definitions() -> tuple[BaselineDefinition, ...]:
        definitions = []
        for kind in REQUIRED_BASELINE_KINDS:
            supported = kind is BaselineKind.MARKET_IMPLIED_DEVIG
            definitions.append(
                BaselineDefinition(
                    kind=kind,
                    baseline_id=f"baseline:{kind.value}",
                    implementation_sha256=_sha(f"impl:{kind.value}"),
                    config_sha256=(
                        market_implied_baseline_config_sha256()
                        if supported
                        else _sha(f"config:{kind.value}")
                    ),
                    supported=supported,
                    unsupported_reason=None if supported else "not supported in falsifier",
                )
            )
        return tuple(definitions)

    def _protocol(self, evidence_rows):
        rows = tuple(evidence_rows)
        contract = canonical_evaluation_contract(
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
        )
        return FrozenBaselineProtocol(
            protocol_id="cohort-market-binding-falsifier",
            frozen_at="2026-09-18T14:30:00Z",
            evidence_scope=FrozenEvidenceScope(
                dataset_sha256=_sha("dataset"),
                dataset_cutoff="2026-09-18T14:00:00Z",
                cohort_keys=tuple(row.cohort_key for row in rows),
                market_evidence_sha256=market_implied_evidence_manifest_sha256(rows),
                outcome_evidence_sha256=_sha("outcomes"),
                cost_model_sha256=_sha("cost"),
                execution_model_sha256=_sha("execution"),
            ),
            candidate_id="candidate",
            candidate_artifact_sha256=_sha("candidate"),
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            evaluation_contract_family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
            evaluation_semantics=contract["evaluation_semantics"],
            evaluation_contract_sha256=contract["evaluation_contract_sha256"],
            primary_metric=contract["primary_metric"],
            uncertainty_method=contract["uncertainty_method"],
            baselines=self._definitions(),
        )

    @staticmethod
    def _market_definition(protocol: FrozenBaselineProtocol) -> BaselineDefinition:
        return next(
            baseline
            for baseline in protocol.baselines
            if baseline.kind is BaselineKind.MARKET_IMPLIED_DEVIG
        )

    def test_caller_label_without_canonical_row_authority_cannot_bind(self) -> None:
        mislabeled = self._evidence(
            cohort_key="row-a",
            event_id="unrelated-event-b",
        )
        protocol = self._protocol((mislabeled,))

        with self.assertRaises(MarketImpliedBaselineError):
            bind_market_implied_baseline_cohort(
                protocol=protocol,
                baseline_definition=self._market_definition(protocol),
                evidence=(mislabeled,),
            )

    def test_swapped_markets_cannot_be_frozen_under_each_others_cohort_labels(self) -> None:
        row_a_label_on_market_b = self._evidence(
            cohort_key="row-a",
            event_id="event-b",
        )
        row_b_label_on_market_a = self._evidence(
            cohort_key="row-b",
            event_id="event-a",
        )
        swapped = (row_a_label_on_market_b, row_b_label_on_market_a)
        protocol = self._protocol(swapped)

        with self.assertRaises(MarketImpliedBaselineError):
            bind_market_implied_baseline_cohort(
                protocol=protocol,
                baseline_definition=self._market_definition(protocol),
                evidence=swapped,
            )


if __name__ == "__main__":
    unittest.main()
