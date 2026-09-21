from __future__ import annotations

from decimal import Decimal

from autosport.risk import RiskOfRuinEvidence
from autosport.risk_of_ruin_authority import (
    risk_of_ruin_result_sha256,
    verify_risk_of_ruin_authority,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ScientificRegistry,
)


def test_risk_authority_rejects_dataset_outcomes_revealed_after_proposal(tmp_path) -> None:
    dataset_id = "future-outcome-dataset"
    manifest_sha256 = "9" * 64
    evaluator_sha256 = "e" * 64
    evaluated_at = "2026-09-16T15:00:01+00:00"
    proposal_at = "2026-09-16T15:00:02+00:00"

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=dataset_id,
            manifest_sha256=manifest_sha256,
            source_identity="risk-fixture",
            license_identity="internal-test",
            causal_cutoff="2026-09-16T14:59:58+00:00",
            available_at_utc="2026-09-16T14:59:59+00:00",
            outcome_reveal_after="2026-09-16T15:00:03+00:00",
        )
    )

    evidence = RiskOfRuinEvidence(
        evidence_id="future-outcome-ror",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="canonical-risk-evaluator",
        causal_cutoff="2026-09-16T14:59:58+00:00",
        evaluated_at=evaluated_at,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256="c" * 64,
        candidate_sha256="d" * 64,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0.005"),
    )
    result_sha256 = risk_of_ruin_result_sha256(
        evidence,
        kind="single",
        evaluator_source_sha256=evaluator_sha256,
        dataset_snapshot_id=dataset_id,
        dataset_manifest_sha256=manifest_sha256,
        effective_sample_size=1000,
        evaluation_available_at=evaluated_at,
    )
    registry.append(
        EvaluationBundleRef(
            evaluation_bundle_id=evidence.evidence_id,
            bundle_sha256=evidence.reproducibility_bundle_sha256,
            evaluator_source_sha256=evaluator_sha256,
            dataset_snapshot_id=dataset_id,
            protocol_sha256=evidence.research_protocol_sha256,
            artifact_hashes=(result_sha256,),
            created_at=evaluated_at,
            effective_sample_size=1000,
        )
    )

    verified, _ = verify_risk_of_ruin_authority(
        registry.path,
        evidence,
        kind="single",
        available_by=proposal_at,
    )

    assert not verified
