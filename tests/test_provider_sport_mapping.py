import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from autosport.provider_sport_mapping import (
    CanonicalSportResolution,
    ProviderSportBinding,
    ProviderSportEvidence,
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)


CURATION_TIME = "2026-09-27T12:21:13Z"
FUTURE = "2099-01-01T00:00:00Z"


def curated_bytes(provider_sport_id: str = "1") -> bytes:
    sport = {"1": "football", "2": "tennis"}[provider_sport_id]
    payload = {
        "schema": "autosport.provider_sport_mapping_evidence",
        "schema_version": 1,
        "provider_namespace": "betfair",
        "provider_sport_id": provider_sport_id,
        "canonical_sport": sport,
        "valid_from": CURATION_TIME,
        "valid_until": None,
        "evidence_available_at": CURATION_TIME,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def registry(tmp: str) -> ProviderSportMappingRegistry:
    return ProviderSportMappingRegistry.initialize_pristine(Path(tmp) / "sport-map.json")


def add(reg: ProviderSportMappingRegistry, provider_sport_id: str = "1"):
    return reg.register_curated(
        provider_namespace="betfair",
        provider_sport_id=provider_sport_id,
    )


def just_before(instant: str) -> str:
    value = datetime.fromisoformat(instant.replace("Z", "+00:00")) - timedelta(microseconds=1)
    return value.isoformat().replace("+00:00", "Z")


def test_round_trip_resolution_binds_product_frozen_source_and_registry_digest():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        binding = add(reg)
        result = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        assert result.canonical_sport == "football"
        assert result.binding_id == binding.binding_id
        assert result.source_snapshot_sha256 == hashlib.sha256(curated_bytes()).hexdigest()
        assert result.registry_sha256 == reg.registry_sha256
        reopened = ProviderSportMappingRegistry(Path(tmp) / "sport-map.json")
        assert reopened.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE
        ) == result


def test_caller_cannot_mint_positive_authority_from_fields_or_exact_bytes():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        with pytest.raises(TypeError):
            ProviderSportMappingRegistry.initialize_pristine(path, clock=lambda: CURATION_TIME)
        reg = ProviderSportMappingRegistry.initialize_pristine(path)

        with pytest.raises(ProviderSportMappingError, match="not positive authority"):
            reg.register(
                provider_namespace="betfair",
                provider_sport_id="1",
                canonical_sport="football",
                valid_from=CURATION_TIME,
                valid_until=None,
                evidence_available_at=CURATION_TIME,
                source_snapshot_sha256="a" * 64,
                recorded_at=CURATION_TIME,
            )

        with pytest.raises(
            ProviderSportMappingError,
            match="caller-authored|positive|authority|register_curated",
        ):
            reg.register_evidence(curated_bytes())

        assert reg.bindings == ()


def test_resolution_is_not_backdated_before_product_curation_or_recording_time():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        binding = add(reg)
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=just_before(CURATION_TIME),
            )
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=just_before(binding.recorded_at),
            )
        assert reg.resolve(
            provider_namespace="betfair", provider_sport_id="1", as_of=binding.recorded_at
        ).canonical_sport == "football"


def test_product_frozen_records_have_distinct_exact_provenance():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        first = add(reg, "1")
        second = add(reg, "2")
        assert first.source_snapshot_sha256 == hashlib.sha256(curated_bytes("1")).hexdigest()
        assert second.source_snapshot_sha256 == hashlib.sha256(curated_bytes("2")).hexdigest()
        assert first.source_snapshot_sha256 != second.source_snapshot_sha256
        assert first.binding_id != second.binding_id


def test_structural_parser_still_rejects_malformed_source_documents():
    for malformed in (
        b'{"schema":"autosport.provider_sport_mapping_evidence"}',
        b'{"schema":"autosport.provider_sport_mapping_evidence","schema":"autosport.provider_sport_mapping_evidence"}',
        b"not json",
    ):
        with pytest.raises(ProviderSportMappingError):
            ProviderSportEvidence.from_exact_bytes(malformed)


def test_curated_publication_requires_exact_product_identity_but_resolution_normalizes_namespace():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError, match="curation authority"):
            reg.register_curated(provider_namespace="ｂｅｔｆａｉｒ", provider_sport_id="1")

        add(reg, "1")
        assert reg.resolve(
            provider_namespace="ｂｅｔｆａｉｒ", provider_sport_id="1", as_of=FUTURE
        ).canonical_sport == "football"
        with pytest.raises(ProviderSportMappingError, match="no causal canonical mapping"):
            reg.resolve(provider_namespace="betfair", provider_sport_id="１", as_of=FUTURE)


def test_uncurated_provider_or_id_cannot_publish():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        for namespace, sport_id in (("matchbook", "1"), ("betfair", "999")):
            with pytest.raises(ProviderSportMappingError, match="curation authority"):
                reg.register_curated(
                    provider_namespace=namespace,
                    provider_sport_id=sport_id,
                )
        assert reg.bindings == ()


def test_overlap_rule_remains_half_open_and_rejects_overlap():
    first = ProviderSportBinding(
        provider_namespace="betfair",
        provider_sport_id="1",
        canonical_sport="football",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-02-01T00:00:00Z",
        evidence_available_at="2026-01-01T00:00:00Z",
        source_snapshot_sha256="a" * 64,
        recorded_at="2026-01-01T00:00:00Z",
    )
    successor = ProviderSportBinding(
        provider_namespace="betfair",
        provider_sport_id="1",
        canonical_sport="football",
        valid_from="2026-02-01T00:00:00Z",
        valid_until=None,
        evidence_available_at="2026-02-01T00:00:00Z",
        source_snapshot_sha256="b" * 64,
        recorded_at="2026-02-01T00:00:00Z",
    )
    ProviderSportMappingRegistry._assert_no_overlap(successor, [first])

    overlapping = ProviderSportBinding(
        provider_namespace="betfair",
        provider_sport_id="1",
        canonical_sport="football",
        valid_from="2026-01-15T00:00:00Z",
        valid_until=None,
        evidence_available_at="2026-01-15T00:00:00Z",
        source_snapshot_sha256="c" * 64,
        recorded_at="2026-01-15T00:00:00Z",
    )
    with pytest.raises(ProviderSportMappingError, match="overlapping"):
        ProviderSportMappingRegistry._assert_no_overlap(overlapping, [first])


def test_exact_curated_registration_retry_is_idempotent_and_preserves_record_time():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        first = add(reg)
        before = reg.path.read_bytes()
        second = add(reg)
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
        ("provider_sport_id", "１"),
    ],
)
def test_invalid_curated_identity_selection_fails_closed(field, value):
    kwargs = {"provider_namespace": "betfair", "provider_sport_id": "1"}
    kwargs[field] = value
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        with pytest.raises(ProviderSportMappingError, match="curation authority"):
            reg.register_curated(**kwargs)
        assert reg.bindings == ()


def test_parser_rejects_invalid_temporal_order_and_naive_timestamp():
    payload = json.loads(curated_bytes().decode("utf-8"))

    payload["valid_until"] = "2026-09-27T12:21:12Z"
    with pytest.raises(ProviderSportMappingError, match="after valid_from"):
        ProviderSportEvidence.from_exact_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    payload = json.loads(curated_bytes().decode("utf-8"))
    payload["evidence_available_at"] = "2026-09-27T12:21:12Z"
    with pytest.raises(ProviderSportMappingError, match="available before"):
        ProviderSportEvidence.from_exact_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    payload = json.loads(curated_bytes().decode("utf-8"))
    payload["valid_from"] = "2026-09-27T12:21:13"
    with pytest.raises(ProviderSportMappingError, match="timezone"):
        ProviderSportEvidence.from_exact_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )


def test_rebound_atomic_writer_fails_closed_before_publication():
    with TemporaryDirectory() as tmp:
        reg = registry(tmp)
        before = reg.path.read_bytes()
        with patch(
            "autosport.provider_sport_mapping.atomic_write_json",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(ProviderSportMappingError, match="writer dependency authority"):
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


def test_stale_instances_append_distinct_product_curated_records_without_loss():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        ProviderSportMappingRegistry.initialize_pristine(path)
        first = ProviderSportMappingRegistry(path)
        second = ProviderSportMappingRegistry(path)
        first.register_curated(provider_namespace="betfair", provider_sport_id="1")
        second.register_curated(provider_namespace="betfair", provider_sport_id="2")
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
        first = add(reg, "1")
        before = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        add(reg, "2")
        after = reg.resolve(provider_namespace="betfair", provider_sport_id="1", as_of=FUTURE)
        assert after.binding_id == first.binding_id == before.binding_id
        assert after.source_snapshot_sha256 == before.source_snapshot_sha256
        assert after.registry_sha256 != before.registry_sha256


def test_legacy_matching_tuple_without_exact_frozen_provenance_does_not_resolve():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry(tmp)
        forged = ProviderSportBinding(
            provider_namespace="betfair",
            provider_sport_id="1",
            canonical_sport="football",
            valid_from=CURATION_TIME,
            valid_until=None,
            evidence_available_at=CURATION_TIME,
            source_snapshot_sha256="a" * 64,
            recorded_at=CURATION_TIME,
        )
        unsigned = {
            "schema": "autosport.provider_sport_mapping",
            "schema_version": 2,
            "bindings": [forged.payload()],
        }
        payload = {
            **unsigned,
            "registry_sha256": hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        }
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        reopened = ProviderSportMappingRegistry(path)
        with pytest.raises(
            ProviderSportMappingError,
            match="curation|provenance|authority",
        ):
            reopened.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=FUTURE,
            )
