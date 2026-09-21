from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    CampaignPrecommitManifestError,
    load_campaign_precommit_manifest,
    write_campaign_precommit_manifest_once,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64


def manifest(**overrides: object) -> CampaignPrecommitManifest:
    values: dict[str, object] = {
        "campaign_id": "paper-forward-2026-09-22",
        "source_id": "betfair:exchange",
        "source_snapshot_sha256": A,
        "committed_at": "2026-09-21T19:00:00Z",
        "observation_not_before": "2026-09-22T06:00:00Z",
        "observation_not_after": "2026-09-29T06:00:00Z",
        "evaluation_universe_sha256": B,
        "strategy_version_id": "strategy-v17",
        "champion_version_id": "model-v42",
        "baseline_version_id": "market-baseline-v3",
        "cost_contract_sha256": C,
        "multiplicity_policy_sha256": D,
        "stopping_policy_sha256": E,
        "restart_policy_sha256": F,
        "causal_evidence_policy_sha256": ZERO,
        "config_sha256": ONE,
    }
    values.update(overrides)
    return CampaignPrecommitManifest(**values)


def test_manifest_digest_is_deterministic_and_sensitive() -> None:
    first = manifest()
    second = manifest()
    assert first.manifest_sha256 == second.manifest_sha256
    assert replace(first, strategy_version_id="strategy-v18").manifest_sha256 != (
        first.manifest_sha256
    )


@pytest.mark.parametrize(
    "field",
    [
        "source_snapshot_sha256",
        "evaluation_universe_sha256",
        "cost_contract_sha256",
        "multiplicity_policy_sha256",
        "stopping_policy_sha256",
        "restart_policy_sha256",
        "causal_evidence_policy_sha256",
        "config_sha256",
    ],
)
def test_sha_fields_require_exact_lowercase_sha256(field: str) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: "A" * 64})
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: "a" * 63})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("campaign_id", ""),
        ("campaign_id", " campaign"),
        ("source_id", "source "),
        ("strategy_version_id", "strategy\x00v1"),
        ("champion_version_id", 42),
        ("baseline_version_id", True),
    ],
)
def test_text_identity_fields_fail_closed(field: str, value: object) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("committed_at", "2026-09-21T21:00:00+02:00"),
        ("committed_at", "2026-09-21T19:00:00"),
        ("observation_not_before", "2026-09-22T08:00:00+02:00"),
        ("observation_not_after", "2026-09-29T06:00:00+00:00"),
    ],
)
def test_timestamps_require_canonical_utc_z_form(field: str, value: str) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: value})


def test_precommit_must_precede_first_observation() -> None:
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="before prospective observation",
    ):
        manifest(committed_at="2026-09-22T06:00:00Z")


def test_observation_window_must_be_nonempty() -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(observation_not_after="2026-09-22T06:00:00Z")


def test_champion_and_baseline_must_be_distinct() -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(baseline_version_id="model-v42")


def test_round_trip_record_preserves_identity() -> None:
    original = manifest()
    restored = CampaignPrecommitManifest.from_record(original.to_record())
    assert restored == original
    assert restored.manifest_sha256 == original.manifest_sha256


def test_schema_version_bool_alias_fails_closed() -> None:
    record = manifest().to_record()
    record["schema_version"] = True
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_unknown_field_fails_closed() -> None:
    record = manifest().to_record()
    record["outcome"] = "WIN"
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_tampered_payload_with_old_digest_fails_closed() -> None:
    record = manifest().to_record()
    record["strategy_version_id"] = "strategy-after-seeing-results"
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="digest mismatch",
    ):
        CampaignPrecommitManifest.from_record(record)


def test_tampered_digest_fails_closed() -> None:
    record = manifest().to_record()
    record["manifest_sha256"] = A
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_write_once_round_trip_and_identical_retry(tmp_path: Path) -> None:
    original = manifest()
    path = tmp_path / "evidence" / "precommit.json"
    first = write_campaign_precommit_manifest_once(path, original)
    second = write_campaign_precommit_manifest_once(path, original)
    assert first == second == original.manifest_sha256
    assert load_campaign_precommit_manifest(path) == original


def test_write_once_refuses_conflicting_successor(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    write_campaign_precommit_manifest_once(path, manifest())
    changed = manifest(strategy_version_id="strategy-v18")
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="conflicts with precommit",
    ):
        write_campaign_precommit_manifest_once(path, changed)


def test_manual_byte_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    original = manifest()
    write_campaign_precommit_manifest_once(path, original)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["baseline_version_id"] = "baseline-after-outcome"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_loader_rejects_non_json_or_non_object(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_write_api_requires_exact_manifest_type(tmp_path: Path) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        write_campaign_precommit_manifest_once(tmp_path / "x.json", object())  # type: ignore[arg-type]


def test_record_does_not_claim_execution_or_readiness_authority() -> None:
    record = manifest().to_record()
    forbidden = {
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "whole_product_complete",
        "execution_authorized",
        "promotion_authorized",
        "settlement_authoritative",
    }
    assert forbidden.isdisjoint(record)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    encoded = json.dumps(manifest().to_record(), separators=(",", ":"))
    encoded = encoded.replace(
        '"campaign_id":"paper-forward-2026-09-22"',
        '"campaign_id":"forged","campaign_id":"paper-forward-2026-09-22"',
        1,
    )
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_loader_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    encoded = json.dumps(manifest().to_record(), separators=(",", ":"))
    encoded = encoded.replace('"schema_version":1', '"schema_version":NaN', 1)
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


@pytest.mark.parametrize("rewrite", ("whitespace", "key_order", "extra_newline"))
def test_loader_rejects_semantically_equivalent_noncanonical_bytes(
    tmp_path: Path, rewrite: str
) -> None:
    path = tmp_path / "precommit.json"
    original = manifest()
    write_campaign_precommit_manifest_once(path, original)
    record = original.to_record()

    if rewrite == "whitespace":
        rewritten = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    elif rewrite == "key_order":
        reversed_record = dict(reversed(tuple(record.items())))
        rewritten = json.dumps(
            reversed_record,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
    else:
        rewritten = path.read_text(encoding="utf-8") + "\n"

    path.write_text(rewritten, encoding="utf-8")
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="bytes are not canonical",
    ):
        load_campaign_precommit_manifest(path)
