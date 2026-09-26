from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from autosport.collector_service import _load_source_factory
from autosport.parlayapi_provider import ParlayApiTableTennisProvider
from autosport.product_source import create_parlay_product_source
from autosport.operator_source_config import (
    OperatorSourceSelectionState,
    build_operator_source_config,
    parse_operator_source_config,
    resolve_operator_source_selection,
)
from autosport.operator_source_registry import (
    OperatorSourceRegistryError,
    ProductSourceRegistryEntry,
    list_product_source_entries,
    resolve_product_source_entry,
    resolve_product_source_factory_spec,
)


def test_registry_ids_round_trip_through_stacked_operator_config_contract() -> None:
    for entry in list_product_source_entries():
        config = build_operator_source_config(entry.source_id)
        reopened = parse_operator_source_config(config.to_json_bytes())
        selection = resolve_operator_source_selection(
            persisted_payload=reopened.to_json_bytes(),
            admin_override_source_id=None,
        )

        assert reopened.source_id == entry.source_id
        assert selection.state is OperatorSourceSelectionState.CONFIGURED
        assert selection.source_id == entry.source_id
        assert selection.runtime_authorized is False
        assert resolve_product_source_factory_spec(selection.source_id) == entry.factory_spec


def test_configured_but_unregistered_source_id_fails_closed_at_registry_boundary() -> None:
    config = build_operator_source_config("unknown-source")
    selection = resolve_operator_source_selection(
        persisted_payload=config.to_json_bytes(),
        admin_override_source_id=None,
    )

    assert selection.state is OperatorSourceSelectionState.CONFIGURED
    assert selection.source_id == "unknown-source"
    assert selection.runtime_authorized is False
    with pytest.raises(
        OperatorSourceRegistryError,
        match="not registered",
    ):
        resolve_product_source_entry(selection.source_id)


def test_registry_exposes_only_grounded_canonical_product_source() -> None:
    entries = list_product_source_entries()

    assert type(entries) is tuple
    assert entries == (
        ProductSourceRegistryEntry(
            source_id="parlayapi-table-tennis",
            factory_spec="autosport.product_source:create_parlay_product_source",
            expected_provider_source_id="parlayapi:table_tennis",
        ),
    )
    entry = entries[0]
    assert entry.expected_provider_source_id == ParlayApiTableTennisProvider.source_id
    assert entry.runtime_authorized is False


def test_registered_factory_spec_resolves_through_existing_canonical_loader() -> None:
    entry = resolve_product_source_entry("parlayapi-table-tennis")

    factory = _load_source_factory(entry.factory_spec)

    assert factory is create_parlay_product_source
    assert entry.runtime_authorized is False


def test_exact_source_id_resolution_has_no_alias_or_default_fallback() -> None:
    canonical = resolve_product_source_entry("parlayapi-table-tennis")

    assert canonical.source_id == "parlayapi-table-tennis"
    assert resolve_product_source_factory_spec(canonical.source_id) == canonical.factory_spec

    for value in (
        "Parlayapi-table-tennis",
        "parlayapi_table_tennis",
        "parlayapi:table_tennis",
        " parlayapi-table-tennis",
        "parlayapi-table-tennis ",
        "parlayapi--table-tennis",
        "parlayapi-table-tenniѕ",  # Cyrillic small letter dze.
        "autosport.product_source:create_parlay_product_source",
        "../parlayapi-table-tennis",
        "unknown-source",
        "",
    ):
        with pytest.raises(OperatorSourceRegistryError):
            resolve_product_source_entry(value)


@pytest.mark.parametrize("value", [None, True, 1, b"parlayapi-table-tennis"])
def test_non_text_source_id_fails_closed(value: object) -> None:
    with pytest.raises(OperatorSourceRegistryError):
        resolve_product_source_entry(value)  # type: ignore[arg-type]


def test_registry_entry_is_frozen_and_caller_entry_cannot_rebind_resolution() -> None:
    real = resolve_product_source_entry("parlayapi-table-tennis")
    forged = ProductSourceRegistryEntry(
        source_id="attacker-source",
        factory_spec="attacker.module:factory",
        expected_provider_source_id="attacker:source",
    )

    with pytest.raises(FrozenInstanceError):
        real.factory_spec = "attacker.module:factory"  # type: ignore[misc]

    assert forged.runtime_authorized is False
    assert resolve_product_source_entry("parlayapi-table-tennis") is real
    with pytest.raises(OperatorSourceRegistryError):
        resolve_product_source_entry(forged.source_id)


def test_resolution_api_accepts_no_caller_registry_or_factory_override() -> None:
    with pytest.raises(TypeError):
        resolve_product_source_entry(  # type: ignore[call-arg]
            "parlayapi-table-tennis",
            {"parlayapi-table-tennis": "attacker.module:factory"},
        )
    with pytest.raises(TypeError):
        resolve_product_source_factory_spec(  # type: ignore[call-arg]
            "parlayapi-table-tennis",
            factory_spec="attacker.module:factory",
        )


@pytest.mark.parametrize(
    ("source_id", "factory_spec", "provider_source_id"),
    [
        ("Bad", "autosport.product_source:create_parlay_product_source", "provider:source"),
        ("bad.id", "autosport.product_source:create_parlay_product_source", "provider:source"),
        ("bad", " autosport.product_source:create_parlay_product_source", "provider:source"),
        ("bad", "autosport.product_source:create_parlay_product_source ", "provider:source"),
        ("bad", "autosport.product_source", "provider:source"),
        ("bad", "autosport.product_source:create:extra", "provider:source"),
        ("bad", "autosport..product_source:create_source", "provider:source"),
        ("bad", "autosport.product-source:create_source", "provider:source"),
        ("bad", "autosport.product_source:create-source", "provider:source"),
        ("bad", "autosport.product_source:create_source", " provider:source"),
        ("bad", "autosport.product_source:create_source", "provider source"),
        ("bad", "autosport.product_source:create_source", "provider:ѕource"),
    ],
)
def test_entry_constructor_rejects_ambiguous_product_shipped_bindings(
    source_id: str,
    factory_spec: str,
    provider_source_id: str,
) -> None:
    with pytest.raises(OperatorSourceRegistryError):
        ProductSourceRegistryEntry(
            source_id=source_id,
            factory_spec=factory_spec,
            expected_provider_source_id=provider_source_id,
        )


def test_listing_is_stable_and_contains_no_duplicate_authority_keys() -> None:
    entries = list_product_source_entries()

    assert len(entries) == len({entry.source_id for entry in entries})
    assert len(entries) == len({entry.factory_spec for entry in entries})
    assert tuple(entry.source_id for entry in entries) == ("parlayapi-table-tennis",)
