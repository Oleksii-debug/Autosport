"""Authenticated transport-origin binding for canonical Betfair discovery.

The #1137 discovery request/provenance contracts are transport-free by design.
This module composes them with the existing product-owned Betfair authenticated
client/session-context authority. Positive origin is process-local: persisted or
copied receipts remain integrity evidence only and must not recreate provider
origin after restart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Sequence
from weakref import ReferenceType, ref

from . import betfair_account_identity as _account_identity
from . import betfair_account_readonly as _readonly
from .betfair_account_identity import (
    BetfairAuthenticatedAccountIdentity,
    require_authoritative_betfair_account_identity,
    resolve_betfair_authenticated_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient, BetfairReadOnlyError
from .betfair_discovery_provenance import (
    BetfairDiscoveryAcquisitionEvidence,
    BetfairDiscoveryExchange,
    BetfairDiscoveryProvenanceError,
)
from .betfair_multisport_catalog import BetfairCatalogRequest


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class BetfairDiscoveryTransportOriginReceipt:
    """Ephemeral proof that one exact discovery exchange used canonical auth I/O."""

    transport_authority_ref: str
    method: str
    request_sha256: str
    raw_response_sha256: str
    observed_at_utc: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError(
            "BetfairDiscoveryTransportOriginReceipt is product-issued; "
            "use acquire_authenticated_betfair_discovery"
        )

    @classmethod
    def _issue(
        cls,
        *,
        transport_authority_ref: str,
        method: str,
        request_sha256: str,
        raw_response_sha256: str,
        observed_at_utc: str,
    ) -> "BetfairDiscoveryTransportOriginReceipt":
        instance = object.__new__(cls)
        for field_name, value in (
            ("transport_authority_ref", transport_authority_ref),
            ("method", method),
            ("request_sha256", request_sha256),
            ("raw_response_sha256", raw_response_sha256),
            ("observed_at_utc", observed_at_utc),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise BetfairDiscoveryProvenanceError(
                    f"{field_name} must be a non-empty trimmed string"
                )
            object.__setattr__(instance, field_name, value)
        for digest_name in ("request_sha256", "raw_response_sha256"):
            digest = getattr(instance, digest_name)
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise BetfairDiscoveryProvenanceError(
                    f"{digest_name} must be lowercase SHA-256 hex"
                )
        return instance


@dataclass(frozen=True, slots=True)
class BetfairAuthenticatedDiscoveryAcquisition:
    """One live canonical read plus its exact request/response origin receipt."""

    exchange: BetfairDiscoveryExchange
    result: object
    receipt: BetfairDiscoveryTransportOriginReceipt
    account_identity: BetfairAuthenticatedAccountIdentity


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


@dataclass(frozen=True, slots=True)
class _IssuedReceiptRecord:
    receipt_ref: ReferenceType[BetfairDiscoveryTransportOriginReceipt]
    receipt_fingerprint: str
    identity_ref: ReferenceType[BetfairAuthenticatedAccountIdentity]
    client_ref: ReferenceType[BetfairReadOnlyClient]


def _receipt_fingerprint(
    receipt: BetfairDiscoveryTransportOriginReceipt,
) -> str:
    payload = {
        "transport_authority_ref": receipt.transport_authority_ref,
        "method": receipt.method,
        "request_sha256": receipt.request_sha256,
        "raw_response_sha256": receipt.raw_response_sha256,
        "observed_at_utc": receipt.observed_at_utc,
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _make_transport_origin_authority():
    issued: dict[int, _IssuedReceiptRecord] = {}
    client_type = BetfairReadOnlyClient
    request_type = BetfairCatalogRequest
    exchange_type = BetfairDiscoveryExchange
    receipt_type = BetfairDiscoveryTransportOriginReceipt
    identity_type = BetfairAuthenticatedAccountIdentity
    rpc_result_type = _readonly._RpcResult
    canonical_rpc = client_type._rpc
    canonical_rpc_params = request_type.rpc_params
    resolve_identity = resolve_betfair_authenticated_account_identity
    require_identity = require_authoritative_betfair_account_identity

    def forget(receipt_id: int, dead: ReferenceType) -> None:
        current = issued.get(receipt_id)
        if current is not None and current.receipt_ref is dead:
            issued.pop(receipt_id, None)

    def class_dispatch_is_current() -> bool:
        return (
            client_type._rpc is canonical_rpc
            and request_type.rpc_params is canonical_rpc_params
            and _account_identity.resolve_betfair_authenticated_account_identity
            is resolve_identity
            and _account_identity.require_authoritative_betfair_account_identity
            is require_identity
            and _readonly._RpcResult is rpc_result_type
        )

    def issue(
        receipt: BetfairDiscoveryTransportOriginReceipt,
        *,
        identity: BetfairAuthenticatedAccountIdentity,
        client: BetfairReadOnlyClient,
    ) -> BetfairDiscoveryTransportOriginReceipt:
        receipt_id = id(receipt)
        receipt_ref = ref(
            receipt,
            lambda dead, receipt_id=receipt_id: forget(receipt_id, dead),
        )
        issued[receipt_id] = _IssuedReceiptRecord(
            receipt_ref=receipt_ref,
            receipt_fingerprint=_receipt_fingerprint(receipt),
            identity_ref=ref(identity),
            client_ref=ref(client),
        )
        return receipt

    def is_authoritative(
        receipt: object,
    ) -> bool:
        if type(receipt) is not receipt_type or not class_dispatch_is_current():
            return False
        record = issued.get(id(receipt))
        if record is None or record.receipt_ref() is not receipt:
            return False
        if record.receipt_fingerprint != _receipt_fingerprint(receipt):
            return False
        identity = record.identity_ref()
        client = record.client_ref()
        if type(identity) is not identity_type or type(client) is not client_type:
            return False
        if receipt.transport_authority_ref != identity.session_context_id:
            return False
        try:
            require_identity(identity, client=client)
        except Exception:
            return False
        return True

    def acquire(
        client: BetfairReadOnlyClient,
        request: BetfairCatalogRequest,
    ) -> BetfairAuthenticatedDiscoveryAcquisition:
        """Execute one exact #1137 request through canonical authenticated read-only I/O."""

        if type(client) is not client_type:
            raise BetfairDiscoveryProvenanceError(
                "authenticated discovery requires exact BetfairReadOnlyClient"
            )
        if type(request) is not request_type:
            raise BetfairDiscoveryProvenanceError(
                "authenticated discovery requires exact BetfairCatalogRequest"
            )
        if not class_dispatch_is_current():
            raise BetfairDiscoveryProvenanceError(
                "canonical Betfair discovery/authenticated dispatch changed"
            )
        state = getattr(client, "__dict__", None)
        if type(state) is not dict or "_rpc" in state:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair RPC dispatch is instance-rebound"
            )

        try:
            identity = resolve_identity(client)
            require_identity(identity, client=client)
            params = canonical_rpc_params(request)
            response = canonical_rpc(client, request.method, params)
            require_identity(identity, client=client)
        except (_account_identity.BetfairAccountIdentityError, BetfairReadOnlyError) as exc:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery acquisition failed"
            ) from exc

        if not class_dispatch_is_current() or type(response) is not rpc_result_type:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery returned non-canonical RPC evidence"
            )
        raw_response = response.raw_response
        evidence = response.evidence
        if type(raw_response) is not bytes or not raw_response:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery lacks immutable raw response bytes"
            )
        if sha256(raw_response).hexdigest() != evidence.source_payload_sha256:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery raw response digest mismatch"
            )
        try:
            observed_at = datetime.fromisoformat(
                evidence.observed_at.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as exc:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery observation time is invalid"
            ) from exc
        try:
            exchange = exchange_type(request, raw_response, observed_at)
        except (TypeError, ValueError) as exc:
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery cannot bind canonical exchange"
            ) from exc
        receipt = receipt_type._issue(
            transport_authority_ref=identity.session_context_id,
            method=exchange.method,
            request_sha256=exchange.request_sha256,
            raw_response_sha256=exchange.raw_response_sha256,
            observed_at_utc=exchange.observed_at_utc,
        )
        issue(receipt, identity=identity, client=client)
        if not is_authoritative(receipt):
            raise BetfairDiscoveryProvenanceError(
                "authenticated Betfair discovery receipt issuance failed closed"
            )
        return BetfairAuthenticatedDiscoveryAcquisition(
            exchange=exchange,
            result=response.result,
            receipt=receipt,
            account_identity=identity,
        )

    return acquire, is_authoritative


(
    acquire_authenticated_betfair_discovery,
    is_authoritative_betfair_discovery_transport_receipt,
) = _make_transport_origin_authority()
del _make_transport_origin_authority


def assess_betfair_discovery_transport_origin(
    evidence: BetfairDiscoveryAcquisitionEvidence,
    *,
    receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
) -> BetfairDiscoveryTransportOriginAssessment:
    """Assess whether every exact discovery exchange has live authenticated origin."""

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
            False,
            "NO_AUTHENTICATED_TRANSPORT_RECEIPTS",
            len(exchanges),
            0,
            fingerprints,
        )

    receipt_by_key: dict[
        tuple[str, str], BetfairDiscoveryTransportOriginReceipt
    ] = {}
    for receipt in receipts:
        if not is_authoritative_betfair_discovery_transport_receipt(receipt):
            return BetfairDiscoveryTransportOriginAssessment(
                False,
                "UNISSUED_OR_STALE_AUTHENTICATED_TRANSPORT_RECEIPT",
                len(exchanges),
                0,
                fingerprints,
            )
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
            "UNEXPECTED_AUTHENTICATED_TRANSPORT_RECEIPT",
            len(exchanges),
            bound,
            fingerprints,
        )

    return BetfairDiscoveryTransportOriginAssessment(
        True,
        "AUTHENTICATED_TRANSPORT_ORIGIN_PROVEN",
        len(exchanges),
        bound,
        fingerprints,
    )


def require_betfair_authenticated_transport_origin(
    evidence: BetfairDiscoveryAcquisitionEvidence,
    *,
    receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
) -> None:
    """Fail closed unless every exchange has current-process canonical auth origin."""

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
