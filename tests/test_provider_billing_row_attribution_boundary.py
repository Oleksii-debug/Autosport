from __future__ import annotations

from dataclasses import fields

from autosport.provider_billing_row_attribution import (
    ProviderBillingRowAttributionEvidence,
)


def test_public_schema_cannot_expose_positive_cost_or_allocation_authority() -> None:
    field_names = {field.name for field in fields(ProviderBillingRowAttributionEvidence)}

    assert {
        "attribution_state",
        "missing_authorities",
        "source_evidence_sha256",
        "statement_request_scope_sha256",
        "statement_source_payload_sha256",
        "row_ref_id",
        "row_amount",
    } <= field_names
    assert field_names.isdisjoint(
        {
            "allocated_amount",
            "allocation_fraction",
            "allocation_weight",
            "cost_class",
            "cost_treatment",
            "known_amount",
            "known_zero",
            "not_applicable",
            "complete",
        }
    )


def test_public_evidence_type_is_slots_only() -> None:
    assert "__dict__" not in ProviderBillingRowAttributionEvidence.__slots__
