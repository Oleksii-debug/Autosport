import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from autosport.provider_sport_mapping import (
    CanonicalSportResolution,
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"


def registry(tmp: str, now: str = T1) -> ProviderSportMappingRegistry:
    return ProviderSportMappingRegistry.initialize_pristine(
        Path(tmp) / "sport-map.json", clock=lambda: now
    )


def add(registry: ProviderSportMappingRegistry, **overrides):
    values = dict(
        provider_namespace="betfair",
        provider_sport_id="1",
        canonical_sport="football",
        valid_from=T0,
        valid_until=None,
        evidence_available_at=T1,
        source_snapshot_sha256=SHA_A,
    )
    values.update(overrides)
    return registry.register(**values)


def test_round_trip_resolution_binds_provider_snapshot_and_registry_digest():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        binding = add(reg)
        result = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2)
        assert result.canonical_sport == "football"
        assert result.binding_id == binding.binding_id
        assert result.source_snapshot_sha256 == SHA_A
        assert result.registry_sha256 == reg.registry_sha256
        reopened = ProviderSportMappingRegistry(Path(tmp) / "sport-map.json")
        assert reopened.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2) == result


def test_unknown_or_not_yet_product_recorded_mapping_fails_closed():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp, now=T2)
        add(reg, evidence_available_at=T1)
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="other", as_of=T3)
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T1)
        assert reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2).canonical_sport == "football"


def test_half_open_validity_boundary_and_successor_mapping():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        reg = ProviderSportMappingRegistry.initialize_pristine(path, clock=lambda: T1)
        add(reg, valid_until=T2)
        reg.clock = lambda: T2
        add(
            reg,
            canonical_sport="soccer",
            valid_from=T2,
            evidence_available_at=T2,
            source_snapshot_sha256=SHA_B,
        )
        assert reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T1).canonical_sport == "football"
        assert reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2).canonical_sport == "soccer"


def test_overlapping_conflicting_mapping_is_rejected():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, valid_until=T3)
        reg.clock = lambda: T2
        with pytest.raises(ProviderSportMappingError, match="overlapping"):
            add(
                reg,
                canonical_sport="tennis",
                valid_from=T2,
                evidence_available_at=T2,
                source_snapshot_sha256=SHA_B,
            )


def test_overlapping_same_label_with_different_snapshot_is_still_ambiguous():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, valid_until=T3)
        reg.clock = lambda: T2
        with pytest.raises(ProviderSportMappingError, match="overlapping"):
            add(
                reg,
                valid_from=T2,
                evidence_available_at=T2,
                source_snapshot_sha256=SHA_B,
            )


def test_exact_registration_retry_is_idempotent_and_preserves_original_record_time():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        first = add(reg)
        before = reg.path.read_bytes()
        reg.clock = lambda: T3
        second = add(reg)
        assert second == first
        assert second.recorded_at == T1
        assert reg.path.read_bytes() == before
        assert len(reg.bindings) == 1


def test_provider_namespace_is_nfkc_representation_normalized_but_opaque_sport_id_is_not():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, provider_namespace="ｂｅｔｆａｉｒ", provider_sport_id="ＴＥＮＮＩＳ")
        result = reg.resolve(provider_namespace="betfair", provider_sport_id="ＴＥＮＮＩＳ", as_of=T2)
        assert result.provider_namespace == "betfair"
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="TENNIS", as_of=T2)


def test_cross_provider_same_provider_sport_id_stays_distinct():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        add(reg, provider_namespace="betfair", provider_sport_id="1", canonical_sport="football")
        add(reg, provider_namespace="matchbook", provider_sport_id="1", canonical_sport="tennis")
        assert reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2).canonical_sport == "football"
        assert reg.resolve(provider_namespace="matchbook", provider_sport_id="1", as_of=T2).canonical_sport == "tennis"


@pytest.mark.parametrize("field,value", [
    ("provider_namespace", ""),
    ("provider_namespace", " betfair"),
    ("provider_sport_id", ""),
    ("provider_sport_id", " 1"),
    ("canonical_sport", "Tennis"),
    ("canonical_sport", "unknown"),
    ("canonical_sport", "ice hockey"),
])
def test_invalid_identity_fields_fail_closed(field, value):
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        kwargs = {field: value}
        with pytest.raises(ProviderSportMappingError):
            add(reg, **kwargs)


def test_invalid_sha_and_temporal_order_fail_closed():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError, match="SHA-256"):
            add(reg, source_snapshot_sha256="ABC")
        with pytest.raises(ProviderSportMappingError, match="available before"):
            add(reg, valid_from=T2, evidence_available_at=T1)
        reg.clock = lambda: T1
        with pytest.raises(ProviderSportMappingError, match="recorded before"):
            add(reg, evidence_available_at=T2)
        with pytest.raises(ProviderSportMappingError, match="after valid_from"):
            add(reg, valid_until=T0)


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
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2)


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
        import hashlib
        payload["registry_sha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
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
        ProviderSportMappingRegistry.initialize_pristine(path, clock=lambda: T1)
        first = ProviderSportMappingRegistry(path, clock=lambda: T1)
        second = ProviderSportMappingRegistry(path, clock=lambda: T1)
        add(first, provider_sport_id="1", canonical_sport="football")
        add(second, provider_sport_id="2", canonical_sport="tennis")
        reopened = ProviderSportMappingRegistry(path)
        assert len(reopened.bindings) == 2
        assert reopened.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2).canonical_sport == "football"
        assert reopened.resolve(provider_namespace="betfair", provider_sport_id="2", as_of=T2).canonical_sport == "tennis"


def test_unrelated_registry_extension_changes_registry_version_not_binding_identity():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        first = add(reg)
        before = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2)
        add(reg, provider_sport_id="2", canonical_sport="tennis")
        after = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=T2)
        assert after.binding_id == first.binding_id == before.binding_id
        assert after.source_snapshot_sha256 == before.source_snapshot_sha256
        assert after.registry_sha256 != before.registry_sha256
