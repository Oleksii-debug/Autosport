"""Product-owned verification boundary for provider billing row identity.

``ProviderBillingRowAttributionEvidence`` is descriptive: its payload and checksum
are deterministic. Downstream product code must not treat that object alone as
provider authority. This module re-resolves the exact authenticated provider
observation and row id, then issues a closure-gated witness only when supplied
evidence is semantically identical to the product resolver result.

Possession, exact type and public fields of the witness are not sufficient
authority either. Supported consumers must pass it through the closure-private
issuance validator below; this prevents ``object.__new__``/``object.__setattr__``
from manufacturing or mutating a bearer capability from public fields.

The witness carries no cost class, allocation, known-zero, applicability or
real-money authority. It proves only that descriptive row evidence was re-derived
from the supplied canonical provider observation.
"""
from __future__ import annotations

from dataclasses import dataclass

from .betfair_provider_billing_inputs import BetfairProviderBillingInputsObservation
from .provider_billing_row_attribution import (
    ProviderBillingAttributionError,
    ProviderBillingRowAttributionEvidence,
    resolve_provider_billing_row_attribution,
)


class ProviderBillingRowAuthorityError(ProviderBillingAttributionError):
    """Raised when provider row identity is not product-verifiable."""


def _build_authority_capability():
    source_cls = BetfairProviderBillingInputsObservation
    evidence_cls = ProviderBillingRowAttributionEvidence
    resolve_fn = resolve_provider_billing_row_attribution
    error_cls = ProviderBillingRowAuthorityError
    issuer = object()
    set_attr = object.__setattr__
    get_attr = object.__getattribute__
    evidence_field_names = (
        "venue_id",
        "provider_owner",
        "app_id",
        "app_version_id",
        "currency_code",
        "source_observed_at",
        "source_evidence_sha256",
        "statement_request_scope_sha256",
        "statement_source_payload_sha256",
        "statement_more_available",
        "row_ref_id",
        "row_item_date",
        "row_amount",
        "row_amount_sign",
        "row_item_class",
        "row_item_class_data_sha256",
        "attribution_state",
        "missing_authorities",
        "evidence_sha256",
    )

    def evidence_projection(value: ProviderBillingRowAttributionEvidence) -> tuple[object, ...]:
        return tuple(get_attr(value, name) for name in evidence_field_names)

    @dataclass(frozen=True, slots=True, init=False)
    class VerifiedProviderBillingRowAuthority:
        """Opaque handle whose authority exists only through closure validation."""

        source_evidence_sha256: str
        row_ref_id: str
        evidence_sha256: str

        def __init__(
            self,
            *,
            source_evidence_sha256: str,
            row_ref_id: str,
            evidence_sha256: str,
            _issuer: object | None = None,
        ) -> None:
            if _issuer is not issuer:
                raise error_cls(
                    "provider billing row authority must be product-issued"
                )
            set_attr(self, "source_evidence_sha256", source_evidence_sha256)
            set_attr(self, "row_ref_id", row_ref_id)
            set_attr(self, "evidence_sha256", evidence_sha256)

    authority_field_names = (
        "source_evidence_sha256",
        "row_ref_id",
        "evidence_sha256",
    )
    issued_authorities: dict[int, tuple[object, tuple[object, ...]]] = {}

    def authority_projection(value: VerifiedProviderBillingRowAuthority) -> tuple[object, ...]:
        return tuple(get_attr(value, name) for name in authority_field_names)

    def validate_authority(
        authority: VerifiedProviderBillingRowAuthority,
    ) -> VerifiedProviderBillingRowAuthority:
        """Accept only the exact untampered object registered by this issuer closure."""

        if type(authority) is not VerifiedProviderBillingRowAuthority:
            raise TypeError(
                "authority must be exact VerifiedProviderBillingRowAuthority"
            )
        issued = issued_authorities.get(id(authority))
        if issued is None or issued[0] is not authority:
            raise error_cls(
                "provider billing row authority must be product-issued and registered"
            )
        if authority_projection(authority) != issued[1]:
            raise error_cls(
                "provider billing row authority no longer matches issued identity"
            )
        return authority

    def verify(
        source: BetfairProviderBillingInputsObservation,
        evidence: ProviderBillingRowAttributionEvidence,
        row_ref_id: str,
    ) -> VerifiedProviderBillingRowAuthority:
        """Re-resolve source+row and register an exact closure-private witness."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        if type(evidence) is not evidence_cls:
            raise TypeError(
                "evidence must be exact ProviderBillingRowAttributionEvidence"
            )

        expected = resolve_fn(source, row_ref_id)
        if evidence_projection(evidence) != evidence_projection(expected):
            raise error_cls(
                "provider billing row evidence does not match canonical source re-resolution"
            )

        authority = VerifiedProviderBillingRowAuthority(
            source_evidence_sha256=expected.source_evidence_sha256,
            row_ref_id=expected.row_ref_id,
            evidence_sha256=expected.evidence_sha256,
            _issuer=issuer,
        )
        issued_authorities[id(authority)] = (
            authority,
            authority_projection(authority),
        )
        return authority

    def resolve_verified(
        source: BetfairProviderBillingInputsObservation,
        row_ref_id: str,
    ) -> tuple[
        ProviderBillingRowAttributionEvidence,
        VerifiedProviderBillingRowAuthority,
    ]:
        """Resolve descriptive evidence and its registered verification witness."""

        evidence = resolve_fn(source, row_ref_id)
        authority = verify(source, evidence, row_ref_id)
        return evidence, authority

    return (
        verify,
        resolve_verified,
        validate_authority,
        VerifiedProviderBillingRowAuthority,
    )


(
    verify_provider_billing_row_attribution,
    resolve_verified_provider_billing_row_attribution,
    validate_verified_provider_billing_row_authority,
    VerifiedProviderBillingRowAuthority,
) = _build_authority_capability()
