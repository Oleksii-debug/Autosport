"""Fail-closed authenticated-transport origin binding for Betfair discovery.

The canonical #1137 discovery lineage is intentionally transport-free.  Exact
request/response bytes are useful acquisition evidence, but caller-supplied
bytes are not proof that Autosport obtained them through an authenticated
Betfair transport.  This module keeps those two facts separate.

A positive transport-origin assessment requires a product-owned issuance
boundary at the authenticated I/O layer.  This transport-free lineage has no
such issuer.  Module-private Python objects are deliberately not treated as a
security or authority boundary: an ordinary caller can introspect/import them.
Until a canonical authenticated transport composes with this seam, even an
otherwise exact structural receipt candidate remains non-authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .betfair_discovery_provenance import (
    BetfairDiscoveryAcquisitionEvidence,
    BetfairDiscoveryExchange,
    BetfairDiscoveryProvenanceError,
)


_TRANSPORT_RECEIPT_ISSUER_TOKEN = object()


@dataclass(frozen=True, slots=True)
class BetfairDiscoveryTransportOriginReceipt:
    """Structural receipt candidate for one exact transport exchange.

    The constructor token is only an internal shape guard.  It is not a
    product-owned trust root and therefore cannot by itself grant authenticated
    transport-origin authority.
    """

    transport_authority_ref: str
    method: str
    request_sha256: str
    raw_response_sha256: str
    observed_at_utc: str
    _issuer_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer_token is not _TRANSPORT_RECEIPT_ISSUER_TOKEN:
            raise BetfairDiscoveryProvenanceError(
                "transport-origin receipts are product-issued only"
            )
        for field_name in (
            "transport_authority_ref",
            "method",
            "request_sha256",
            "raw_response_sha256",
            "observed_at_utc",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value or value != value.strip():
                raise BetfairDiscoveryProvenanceError(
                    f"{field_name} must be a non-empty trimmed string"
                )
        for digest_name in ("request_sha256", "raw_response_sha256"):
            digest = getattr(self, digest_name)
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise BetfairDiscoveryProvenanceError(
                    f"{digest_name} must be lowercase SHA-256 hex"
                )


@dataclass(frozen=True, slots=True)
class BetfairDiscoveryTransportOriginAssessment:
    """Truthful authority projection for one canonical discovery acquisition."""

    grants_authenticated_transport_origin_authority: bool
    reason: str
    exchange_count: int
    bound_exchange_count: int
    exchange_raw_response_sha256: tuple[str, ...]

    def projection(self) -> dict[str, object]:
        return {
            "grants_authenticated_transport_origin_authority": (
                self.grants_authenticated_transport_origin_authority
            ),
            "reason": self.reason,
            "exchange_count": self.exchange_count,
            "bound_exchange_count": self.bound_exchange_count,
            "exchange_raw_response_sha256": list(
                self.exchange_raw_response_sha256
            ),
        }


def assess_betfair_discovery_transport_origin(
    evidence: BetfairDiscoveryAcquisitionEvidence,
    *,
    receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
) -> BetfairDiscoveryTransportOriginAssessment:
    """Assess whether every exact discovery exchange has transport-origin proof.

    Plain ``BetfairDiscoveryExchange`` values prove byte/request/time binding,
    not authenticated provider origin.  Positive authority is therefore
    impossible without capability-gated transport receipts matching every exact
    exchange.
    """
    if type(evidence) is not BetfairDiscoveryAcquisitionEvidence:
        raise BetfairDiscoveryProvenanceError(
            "evidence must be exact BetfairDiscoveryAcquisitionEvidence"
        )
    if any(type(item) is not BetfairDiscoveryTransportOriginReceipt for item in receipts):
        raise BetfairDiscoveryProvenanceError(
            "receipts must contain exact product-issued transport-origin receipts"
        )

    exchanges = _ordered_exchanges(evidence)
    fingerprints = tuple(item.raw_response_sha256 for item in exchanges)
    if not receipts:
        return BetfairDiscoveryTransportOriginAssessment(
            grants_authenticated_transport_origin_authority=False,
            reason="NO_AUTHENTICATED_TRANSPORT_RECEIPTS",
            exchange_count=len(exchanges),
            bound_exchange_count=0,
            exchange_raw_response_sha256=fingerprints,
        )

    receipt_by_key: dict[tuple[str, str], BetfairDiscoveryTransportOriginReceipt] = {}
    for receipt in receipts:
        key = (receipt.method, receipt.request_sha256)
        if key in receipt_by_key:
            raise BetfairDiscoveryProvenanceError(
                "duplicate transport-origin receipt for canonical request"
            )
        receipt_by_key[key] = receipt

    bound = 0
    for exchange in exchanges:
        receipt = receipt_by_key.get((exchange.method, exchange.request_sha256))
        if receipt is None:
            return BetfairDiscoveryTransportOriginAssessment(
                False,
                "MISSING_AUTHENTICATED_TRANSPORT_RECEIPT",
                len(exchanges),
                bound,
                fingerprints,
            )
        if (
            receipt.raw_response_sha256 != exchange.raw_response_sha256
            or receipt.observed_at_utc != exchange.observed_at_utc
        ):
            return BetfairDiscoveryTransportOriginAssessment(
                False,
                "TRANSPORT_RECEIPT_DOES_NOT_BIND_EXACT_EXCHANGE",
                len(exchanges),
                bound,
                fingerprints,
            )
        bound += 1

    if len(receipt_by_key) != len(exchanges):
        return BetfairDiscoveryTransportOriginAssessment(
            False,
            "UNEXPECTED_TRANSPORT_RECEIPT",
            len(exchanges),
            bound,
            fingerprints,
        )

    # A module-private Python token is introspectable/importable by ordinary
    # in-process callers and therefore cannot be the trust root for provider
    # origin.  The current #1137 lineage is transport-free, so exact candidate
    # receipts prove structural co-binding only.  A future canonical
    # authenticated transport must add a product-owned issuance/composition
    # boundary before this authority can become true.
    return BetfairDiscoveryTransportOriginAssessment(
        False,
        "NO_PRODUCT_OWNED_TRANSPORT_RECEIPT_ISSUER",
        len(exchanges),
        bound,
        fingerprints,
    )


def require_betfair_authenticated_transport_origin(
    evidence: BetfairDiscoveryAcquisitionEvidence,
    *,
    receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
) -> None:
    """Fail closed unless the acquisition is bound to authenticated I/O receipts."""
    assessment = assess_betfair_discovery_transport_origin(
        evidence,
        receipts=receipts,
    )
    if not assessment.grants_authenticated_transport_origin_authority:
        raise BetfairDiscoveryProvenanceError(
            "authenticated Betfair transport origin is not proven: "
            + assessment.reason
        )


def _ordered_exchanges(
    evidence: BetfairDiscoveryAcquisitionEvidence,
) -> tuple[BetfairDiscoveryExchange, ...]:
    if evidence.competition_exchange is None:
        return (evidence.event_type_exchange, evidence.market_type_exchange)
    return (
        evidence.event_type_exchange,
        evidence.competition_exchange,
        evidence.market_type_exchange,
    )
