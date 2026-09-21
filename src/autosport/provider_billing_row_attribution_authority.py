"""Product-owned verification boundary for provider billing row identity.

``ProviderBillingRowAttributionEvidence`` is descriptive: its payload and checksum
are deterministic. Downstream product code must not treat that object alone as
provider authority. This module re-resolves the exact authenticated provider
observation and row id, then issues a closure-gated witness only when supplied
evidence is semantically identical to the product resolver result.

The upstream provider observation is also descriptive data unless it was issued by
the canonical provider-read authority.  Positive row issuance therefore validates
the exact source object against that closure-private issuance relation before any
row re-resolution.  Caller-constructed checksum-valid observation DTOs cannot mint
a registered row authority.

Possession, exact type and public fields of the witness are not sufficient
authority either. Supported consumers must pass it through the closure-private
issuance validator below; this prevents ``object.__new__``/``object.__setattr__``
from manufacturing or mutating a bearer capability from public fields.

The witness carries no cost class, allocation, known-zero, applicability or
real-money authority. It proves only that descriptive row evidence was re-derived
from one product-issued provider observation.
"""
from __future__ import annotations

from dataclasses import dataclass

from .betfair_provider_billing_inputs import BetfairProviderBillingInputsObservation
from .betfair_provider_billing_inputs_authority import (
    BetfairProviderBillingInputsAuthorityError,
    validate_betfair_provider_billing_inputs_observation,
)
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
    validate_source_fn = validate_betfair_provider_billing_inputs_observation
    source_authority_error_cls = BetfairProviderBillingInputsAuthorityError
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

    def require_issued_source(source: BetfairProviderBillingInputsObservation) -> None:
        try:
            validate_source_fn(source)
        except source_authority_error_cls as exc:
            raise error_cls(
                "provider billing source must be issued by canonical provider read"
            ) from exc

    def verify(
        source: BetfairProviderBillingInputsObservation,
        evidence: ProviderBillingRowAttributionEvidence,
        row_ref_id: str,
    ) -> VerifiedProviderBillingRowAuthority:
        """Re-resolve one product-issued source row and register an exact witness."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        if type(evidence) is not evidence_cls:
            raise TypeError(
                "evidence must be exact ProviderBillingRowAttributionEvidence"
            )

        require_issued_source(source)
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
        """Resolve descriptive evidence from one product-issued source observation."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        require_issued_source(source)
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
