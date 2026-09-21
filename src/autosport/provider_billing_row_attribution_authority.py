"""Product-owned verification boundary for provider billing row identity.

``ProviderBillingRowAttributionEvidence`` is descriptive: its payload and checksum
are deterministic. Downstream product code must not treat that object alone as
provider authority. This module re-resolves the exact authenticated provider
observation and row id, then issues a closure-gated witness only when supplied
evidence is semantically identical to the product resolver result.

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

    @dataclass(frozen=True, slots=True, init=False)
    class VerifiedProviderBillingRowAuthority:
        """Opaque proof that row evidence re-resolved from canonical provider input."""

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

    def verify(
        source: BetfairProviderBillingInputsObservation,
        evidence: ProviderBillingRowAttributionEvidence,
        row_ref_id: str,
    ) -> VerifiedProviderBillingRowAuthority:
        """Re-resolve source+row and issue an opaque witness on exact equality only."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        if type(evidence) is not evidence_cls:
            raise TypeError(
                "evidence must be exact ProviderBillingRowAttributionEvidence"
            )

        expected = resolve_fn(source, row_ref_id)
        if evidence != expected:
            raise error_cls(
                "provider billing row evidence does not match canonical source re-resolution"
            )

        return VerifiedProviderBillingRowAuthority(
            source_evidence_sha256=expected.source_evidence_sha256,
            row_ref_id=expected.row_ref_id,
            evidence_sha256=expected.evidence_sha256,
            _issuer=issuer,
        )

    def resolve_verified(
        source: BetfairProviderBillingInputsObservation,
        row_ref_id: str,
    ) -> tuple[
        ProviderBillingRowAttributionEvidence,
        VerifiedProviderBillingRowAuthority,
    ]:
        """Resolve descriptive evidence and its product-issued verification witness."""

        evidence = resolve_fn(source, row_ref_id)
        authority = verify(source, evidence, row_ref_id)
        return evidence, authority

    return verify, resolve_verified, VerifiedProviderBillingRowAuthority


(
    verify_provider_billing_row_attribution,
    resolve_verified_provider_billing_row_attribution,
    VerifiedProviderBillingRowAuthority,
) = _build_authority_capability()
