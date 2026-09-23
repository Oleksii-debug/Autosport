from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.dataset import load_dataset
from autosport.domain import MarketEvent
from autosport.historical_corpus import assemble_historical_corpus


TERMS = "https://parlay-api.com/terms"
CAPTURED_AT = "2026-01-02T00:00:00+00:00"
REVEAL_AT = "2026-01-01T11:00:00+00:00"
IMPORTED_AT = "2026-01-02T00:05:00+00:00"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_snapshot(
    root: Path,
    *,
    sport: str,
    suffix: str,
    requested_at: str = "2026-01-01T10:00:20+00:00",
    source_id: str | None = None,
    event_id: str | None = None,
) -> tuple[Path, Path, MarketEvent]:
    source = source_id or f"parlayapi:{sport}"
    event = MarketEvent(
        event_id=event_id or f"event-{suffix}",
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal("1.80"),
        observed_ts="2026-01-01T10:00:00+00:00",
        source_id=source,
        sequence=1,
        source_ts="2026-01-01T09:59:59+00:00",
        ingest_ts=CAPTURED_AT,
        sport=sport,
        metadata={
            "bookmaker_key": "book-a",
            "source_time_semantics": "provider_quote_last_update",
        },
    )
    market = root / f"snapshot-{suffix}.jsonl"
    evidence = root / f"snapshot-{suffix}.evidence.json"
    market.write_text(
        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "parlayapi_point_in_time_historical_snapshot",
                "provider": "parlayapi",
                "sport_key": sport,
                "requested_at": requested_at,
                "snapshot_at": "2026-01-01T10:00:10+00:00",
                "captured_at": CAPTURED_AT,
                "response_sha256": "1" * 64,
                "market_sha256": _sha256(market),
                "quote_count": 1,
                "has_data": True,
                "market_types": [event.market_type.value],
                "bookmaker_keys": ["book-a"],
                "snapshot_timestamp_fallback_count": 0,
                "point_in_time_snapshot_contains_odds": True,
                "point_in_time_odds_market_coverage_verified": False,
                "historical_window_market_coverage_verified": False,
                "sealed_outcomes_present": False,
                "replay_corpus_ready": False,
                "terms_reference": TERMS,
                "licensing_or_retention_verified": False,
                "redistribution_verified": False,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return market, evidence, event


def _write_results(root: Path, events: tuple[MarketEvent, ...], *, suffix: str) -> Path:
    path = root / f"results-{suffix}.json"
    source_record = root / f"result-source-{suffix}.json"
    outcomes = {event.quote_key: "win" for event in events}
    source_record.write_text(
        json.dumps(
            {
                "source": f"official-results:{suffix}",
                "quote_outcomes": outcomes,
                "fixture_only": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": outcomes,
                "outcome_provenance": {
                    "schema_version": 1,
                    "kind": "historical_outcome_provenance",
                    "source_identity": f"official-results:{suffix}",
                    "source_record_file": source_record.name,
                    "source_record_sha256": _sha256(source_record),
                    "terms_reference": "https://example.test/result-terms",
                    "retention_basis": "deterministic test result authority",
                    "authority_reference": f"result-authority:{suffix}",
                    "available_at": REVEAL_AT,
                    "acquired_at": "2026-01-02T00:02:00+00:00",
                    "verified_at": "2026-01-02T00:03:00+00:00",
                    "licensing_or_retention_verified": True,
                    "redistribution_policy": "internal_only",
                    "redistribution_verified": False,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_governance(
    root: Path,
    source_ids: tuple[str, ...],
    *,
    suffix: str,
    authority_reference: str | None = None,
) -> Path:
    claims = {
        "source_identity": f"parlayapi:account-entitlement:{suffix}",
        "source_ids": list(source_ids),
        "terms_reference": TERMS,
        "retention_basis": "deterministic internal historical research fixture",
        "retention_expires_at": "2026-12-31T00:00:00+00:00",
        "authorization_valid_through": "2026-12-31T00:00:00+00:00",
        "retention_extension_authority_reference": f"retention-extension:{suffix}",
        "authority_reference": authority_reference or f"entitlement:{suffix}",
        "verified_at": "2026-01-02T00:01:00+00:00",
        "redistribution_policy": "internal_only",
        "redistribution_verified": False,
        "licensing_or_retention_verified": True,
    }
    authority = root / f"governance-authority-{suffix}.json"
    authority.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_authority_record",
                **claims,
                "evidence_reference": f"test-evidence:{suffix}",
                "verification_method": "deterministic test fixture",
                "recorded_by": "test-suite",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    proof = root / f"governance-proof-{suffix}.json"
    proof.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_proof",
                **claims,
                "authority_record_file": authority.name,
                "authority_record_sha256": _sha256(authority),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return proof


def _assemble(
    root: Path,
    snapshot: tuple[Path, Path, MarketEvent],
    *,
    output_name: str,
    governance_suffix: str,
) -> dict:
    market, evidence, event = snapshot
    source_id = event.source_id
    proof = _write_governance(root, (source_id,), suffix=governance_suffix)
    assemble_historical_corpus(
        [(market, evidence)],
        results_path=_write_results(root, (event,), suffix=output_name),
        governance_proof_path=proof,
        output_dir=root / output_name,
        name=f"{event.sport} governed historical fixture",
        outcome_reveal_after=REVEAL_AT,
        imported_at=IMPORTED_AT,
    )
    return json.loads((root / output_name / "manifest.json").read_text(encoding="utf-8"))


def test_explicit_second_sport_enters_existing_governed_historical_pipeline(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path, sport="basketball", suffix="basketball")
    manifest = _assemble(
        tmp_path,
        snapshot,
        output_name="basketball-corpus",
        governance_suffix="basketball",
    )
    dataset = load_dataset(tmp_path / "basketball-corpus")
    provenance = manifest["governance"]["acquisition_evidence"]["provenance"]

    assert dataset.schema_version == 3
    assert dataset.sport == "basketball"
    assert {event.sport for event in dataset.events} == {"basketball"}
    assert manifest["governance"]["coverage"]["source_ids"] == ["parlayapi:basketball"]
    assert provenance["product_kind"] == "POINT_IN_TIME_ODDS"
    assert provenance["causal_classification"] == "RETROSPECTIVE_POINT_IN_TIME_PRICE"
    assert provenance["qualification_scope"] == "selected_point_in_time_snapshot_corpus_v1"
    assert provenance["prospective_authority"] is False
    assert provenance["provider_response_metadata_bound"] is False
    assert provenance["raw_redistribution_authority"] is False
    for key in (
        "content_identity",
        "acquisition_identity",
        "governance_identity",
        "qualified_corpus_identity",
    ):
        assert len(provenance[key]) == 64
        int(provenance[key], 16)


def test_cross_sport_source_substitution_fails_closed(tmp_path: Path) -> None:
    market, evidence, event = _write_snapshot(
        tmp_path,
        sport="basketball",
        suffix="substitution",
        source_id="parlayapi:table_tennis",
    )
    proof = _write_governance(
        tmp_path,
        ("parlayapi:table_tennis",),
        suffix="substitution",
    )
    with pytest.raises(ValueError, match="provider/sport"):
        assemble_historical_corpus(
            [(market, evidence)],
            results_path=_write_results(tmp_path, (event,), suffix="substitution"),
            governance_proof_path=proof,
            output_dir=tmp_path / "blocked-substitution",
            name="blocked",
            outcome_reveal_after=REVEAL_AT,
            imported_at=IMPORTED_AT,
        )


def test_multiple_explicit_sports_cannot_be_laundered_into_one_corpus(tmp_path: Path) -> None:
    basketball = _write_snapshot(tmp_path, sport="basketball", suffix="basketball-mixed")
    football = _write_snapshot(
        tmp_path,
        sport="association_football",
        suffix="football-mixed",
    )
    proof = _write_governance(
        tmp_path,
        ("parlayapi:association_football", "parlayapi:basketball"),
        suffix="mixed",
    )
    with pytest.raises(ValueError, match="multiple explicit sport"):
        assemble_historical_corpus(
            [(basketball[0], basketball[1]), (football[0], football[1])],
            results_path=_write_results(
                tmp_path,
                (basketball[2], football[2]),
                suffix="mixed",
            ),
            governance_proof_path=proof,
            output_dir=tmp_path / "blocked-mixed",
            name="blocked",
            outcome_reveal_after=REVEAL_AT,
            imported_at=IMPORTED_AT,
        )


def test_acquisition_drift_changes_only_acquisition_and_qualified_identity(tmp_path: Path) -> None:
    first = _write_snapshot(
        tmp_path,
        sport="basketball",
        suffix="identity-a",
        requested_at="2026-01-01T10:00:20+00:00",
        event_id="event-identity-stable",
    )
    second = _write_snapshot(
        tmp_path,
        sport="basketball",
        suffix="identity-b",
        requested_at="2026-01-01T10:00:30+00:00",
        event_id="event-identity-stable",
    )

    first_manifest = _assemble(
        tmp_path,
        first,
        output_name="identity-corpus-a",
        governance_suffix="identity-shared-a",
    )
    second_manifest = _assemble(
        tmp_path,
        second,
        output_name="identity-corpus-b",
        governance_suffix="identity-shared-a",
    )
    first_provenance = first_manifest["governance"]["acquisition_evidence"]["provenance"]
    second_provenance = second_manifest["governance"]["acquisition_evidence"]["provenance"]

    assert first_provenance["content_identity"] == second_provenance["content_identity"]
    assert first_provenance["governance_identity"] == second_provenance["governance_identity"]
    assert first_provenance["acquisition_identity"] != second_provenance["acquisition_identity"]
    assert (
        first_provenance["qualified_corpus_identity"]
        != second_provenance["qualified_corpus_identity"]
    )
