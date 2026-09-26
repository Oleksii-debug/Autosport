import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from autosport.provider_sport_mapping import (
    CanonicalSportResolution,
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)


T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


def evidence_bytes(**overrides) -> bytes:
    payload = {
        "schema": "autosport.provider_sport_mapping_evidence",
        "schema_version": 1,
        "provider_namespace": "betfair",
        "provider_sport_id": "1",
        "canonical_sport": "football",
        "valid_from": T0,
        "valid_until": None,
        "evidence_available_at": T1,
    }
    payload.update(overrides)
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def registry(tmp: str) -> ProviderSportMappingRegistry:
    return ProviderSportMappingRegistry.initialize_pristine(Path(tmp) / "sport-map.json")


def add(reg: ProviderSportMappingRegistry, **overrides):
    return reg.register_evidence(evidence_bytes(**overrides))


def just_before(instant: str) -> str:
    value = datetime.fromisoformat(instant.replace("Z", "+00:00")) - timedelta(microseconds=1)
    return value.isoformat().replace("+00:00", "Z")


def test_round_trip_resolution_binds_exact_source_bytes_and_registry_digest():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        raw = evidence_bytes()
        binding = reg.register_evidence(raw)
        result = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        assert result.canonical_sport == "football"
        assert result.binding_id == binding.binding_id
        assert result.source_snapshot_sha256 == hashlib.sha256(raw).hexdigest()
        assert result.registry_sha256 == reg.registry_sha256
        reopened = ProviderSportMappingRegistry(Path(tmp) / "sport-map.json")
        assert reopened.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE
        ) == result


def test_caller_cannot_mint_positive_authority_from_digest_labels_or_backdated_clock():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        with pytest.raises(TypeError):
            ProviderSportMappingRegistry.initialize_pristine(path, clock=lambda: T0)
        reg = ProviderSportMappingRegistry.initialize_pristine(path)
        with pytest.raises(ProviderSportMappingError, match="not positive authority"):
            reg.register(
                provider_namespace="betfair",
                provider_sport_id="1",
                canonical_sport="football",
                valid_from=T0,
                valid_until=None,
                evidence_available_at=T0,
                source_snapshot_sha256="a" * 64,
                recorded_at=T0,
            )
        assert reg.bindings == ()


def test_resolution_is_not_backdated_before_product_owned_recording_time():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        binding = add(reg)
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=just_before(binding.recorded_at),
            )
        assert reg.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=binding.recorded_at
        ).canonical_sport == "football"


def test_exact_source_byte_change_changes_provenance_digest():
    raw = evidence_bytes()
    alternate = raw + b"\n"
    with TemporaryDirectory() as first_tmp, TemporaryDirectory() as second_tmp:
        first = registry(first_tmp).register_evidence(raw)
        second = registry(second_tmp).register_evidence(alternate)
        assert first.source_snapshot_sha256 != second.source_snapshot_sha256
        assert first.binding_id != second.binding_id


def test_source_snapshot_must_mechanically_contain_claimed_mapping_contract():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        for malformed in (
            b'{"schema":"autosport.provider_sport_mapping_evidence"}',
            b'{"schema":"autosport.provider_sport_mapping_evidence","schema":"autosport.provider_sport_mapping_evidence"}',
            b"not json",
        ):
            with pytest.raises(ProviderSportMappingError):
                reg.register_evidence(malformed)
        assert reg.bindings == ()


def test_provider_namespace_is_nfkc_normalized_but_opaque_id_is_not():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, provider_namespace="ｂｅｔｆａｉｒ")
        result = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        assert result.provider_namespace == "betfair"
        with pytest.raises(ProviderSportMappingError, match="curation authority"):
            add(reg, provider_sport_id="１")
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="１", as_of=FUTURE)


def test_uncurated_provider_cannot_reuse_a_curated_opaque_id():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, provider_namespace="betfair", canonical_sport="football")
        with pytest.raises(ProviderSportMappingError, match="curation authority"):
            add(reg, provider_namespace="matchbook", canonical_sport="tennis")
        assert reg.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE
        ).canonical_sport == "football"
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="matchbook", provider_sport_id="1", as_of=FUTURE)


def test_half_open_validity_boundary_and_successor_mapping():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, valid_until=T2)
        add(reg, valid_from=T2, evidence_available_at=T2)
        assert reg.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE
        ).canonical_sport == "football"
        first = reg.bindings[0]
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=just_before(first.recorded_at),
            )


def test_overlapping_mapping_is_rejected_even_with_different_exact_source_bytes():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, valid_until=FUTURE)
        with pytest.raises(ProviderSportMappingError, match="overlapping"):
            add(reg, valid_from=T2, evidence_available_at=T2)


def test_exact_registration_retry_is_idempotent_and_preserves_original_record_time():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        raw = evidence_bytes()
        first = reg.register_evidence(raw)
        before = reg.path.read_bytes()
        second = reg.register_evidence(raw)
        assert second == first
        assert second.recorded_at == first.recorded_at
        assert reg.path.read_bytes() == before
        assert len(reg.bindings) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_namespace", ""),
        ("provider_namespace", " betfair"),
        ("provider_sport_id", ""),
        ("provider_sport_id", " 1"),
        ("canonical_sport", "Tennis"),
        ("canonical_sport", "unknown"),
        ("canonical_sport", "ice hockey"),
    ],
)
def test_invalid_identity_fields_fail_closed(field, value):
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError):
            add(reg, **{field: value})


def test_invalid_temporal_order_and_future_evidence_fail_closed():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError, match="available before"):
            add(reg, valid_from=T2, evidence_available_at=T1)
        with pytest.raises(ProviderSportMappingError, match="after valid_from"):
            add(reg, valid_until=T0)
        with pytest.raises(ProviderSportMappingError, match="future"):
            add(reg, evidence_available_at="2099-01-01T00:00:00Z")


def test_naive_timestamp_is_rejected():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError, match="timezone"):
            add(reg, valid_from="2026-01-01T00:00:00")


def test_failed_publication_does_not_expose_uncommitted_binding():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        before = reg.path.read_bytes()
        with patch("autosport.provider_sport_mapping.atomic_write_json", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                add(reg)
        assert reg.path.read_bytes() == before
        assert reg.bindings == ()


def test_tampered_registry_digest_fails_closed():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg)
        payload = json.loads(reg.path.read_text(encoding="utf-8"))
        payload["bindings"][0]["canonical_sport"] = "tennis"
        reg.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ProviderSportMappingError, match="digest mismatch"):
            ProviderSportMappingRegistry(reg.path)


def test_binding_id_tamper_fails_even_when_outer_digest_is_recomputed():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg)
        payload = json.loads(reg.path.read_text(encoding="utf-8"))
        payload["bindings"][0]["binding_id"] = "c" * 64
        unsigned = {key: payload[key] for key in ("schema", "schema_version", "bindings")}
        payload["registry_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        reg.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ProviderSportMappingError, match="binding_id"):
            ProviderSportMappingRegistry(reg.path)


def test_resolution_contract_contains_no_execution_or_money_authority():
    fields = set(CanonicalSportResolution.__dataclass_fields__)
    assert fields == {
        "provider_namespace", "provider_sport_id", "canonical_sport", "as_of",
        "binding_id", "source_snapshot_sha256", "registry_sha256",
    }
    assert "stake" not in fields
    assert "execution_authorized" not in fields
    assert "settlement_authorized" not in fields


def test_stale_instances_cannot_overwrite_each_others_append():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        ProviderSportMappingRegistry.initialize_pristine(path)
        first = ProviderSportMappingRegistry(path)
        second = ProviderSportMappingRegistry(path)
        first.register_evidence(evidence_bytes(provider_sport_id="1", canonical_sport="football"))
        second.register_evidence(evidence_bytes(provider_sport_id="2", canonical_sport="tennis"))
        reopened = ProviderSportMappingRegistry(path)
        assert len(reopened.bindings) == 2
        assert reopened.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE
        ).canonical_sport == "football"
        assert reopened.resolve(
            provider_namespace="betfair", provider_sport_id="2", as_of=FUTURE
        ).canonical_sport == "tennis"


def test_unrelated_registry_extension_changes_registry_version_not_binding_identity():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        first = add(reg)
        before = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        add(reg, provider_sport_id="2", canonical_sport="tennis")
        after = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        assert after.binding_id == first.binding_id == before.binding_id
        assert after.source_snapshot_sha256 == before.source_snapshot_sha256
        assert after.registry_sha256 != before.registry_sha256
