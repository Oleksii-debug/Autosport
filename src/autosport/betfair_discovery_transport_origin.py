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
    BetfairDiscoveryVisibilityScope,
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


def _make_transport_origin_authority():
    issued: dict[
        int,
        tuple[
            ReferenceType[BetfairDiscoveryTransportOriginReceipt],
            tuple[str, str, str, str, str],
            ReferenceType[BetfairAuthenticatedAccountIdentity],
            ReferenceType[BetfairReadOnlyClient],
        ],
    ] = {}
    client_type = BetfairReadOnlyClient
    request_type = BetfairCatalogRequest
    exchange_type = BetfairDiscoveryExchange
    receipt_type = BetfairDiscoveryTransportOriginReceipt
    identity_type = BetfairAuthenticatedAccountIdentity
    evidence_type = BetfairDiscoveryAcquisitionEvidence
    visibility_scope_type = BetfairDiscoveryVisibilityScope
    assessment_type = BetfairDiscoveryTransportOriginAssessment
    provenance_error_type = BetfairDiscoveryProvenanceError
    rpc_result_type = _readonly._RpcResult

    canonical_rpc = client_type._rpc
    canonical_rpc_params = request_type.rpc_params
    canonical_exchange_init = exchange_type.__init__
    canonical_exchange_post_init = exchange_type.__post_init__
    canonical_receipt_issue = receipt_type._issue
    resolve_identity = resolve_betfair_authenticated_account_identity
    require_identity = require_authoritative_betfair_account_identity

    receipt_fields = tuple(
        receipt_type.__dict__[name]
        for name in (
            "transport_authority_ref",
            "method",
            "request_sha256",
            "raw_response_sha256",
            "observed_at_utc",
        )
    )
    evidence_fields = {
        name: evidence_type.__dict__[name]
        for name in (
            "visibility_scope",
            "event_type_exchange",
            "market_type_exchange",
            "competition_exchange",
        )
    }
    account_scope_field = visibility_scope_type.__dict__["account_scope_ref"]
    exchange_getters = {
        name: exchange_type.__dict__[name].fget
        for name in (
            "method",
            "request_sha256",
            "raw_response_sha256",
            "observed_at_utc",
        )
    }

    def receipt_snapshot(
        receipt: BetfairDiscoveryTransportOriginReceipt,
    ) -> tuple[str, str, str, str, str]:
        return tuple(
            descriptor.__get__(receipt, receipt_type)
            for descriptor in receipt_fields
        )

    def exchange_snapshot(
        exchange: BetfairDiscoveryExchange,
    ) -> tuple[str, str, str, str]:
        if type(exchange) is not exchange_type:
            raise provenance_error_type(
                "discovery evidence contains non-canonical exchange"
            )
        return (
            exchange_getters["method"](exchange),
            exchange_getters["request_sha256"](exchange),
            exchange_getters["raw_response_sha256"](exchange),
            exchange_getters["observed_at_utc"](exchange),
        )

    def forget(receipt_id: int, dead: ReferenceType) -> None:
        current = issued.get(receipt_id)
        if current is not None and current[0] is dead:
            issued.pop(receipt_id, None)

    def class_dispatch_is_current() -> bool:
        return (
            client_type._rpc is canonical_rpc
            and request_type.rpc_params is canonical_rpc_params
            and exchange_type.__init__ is canonical_exchange_init
            and exchange_type.__post_init__ is canonical_exchange_post_init
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
        issued[receipt_id] = (
            receipt_ref,
            receipt_snapshot(receipt),
            ref(identity),
            ref(client),
        )
        return receipt

    def is_authoritative(
        receipt: object,
    ) -> bool:
        if type(receipt) is not receipt_type or not class_dispatch_is_current():
            return False
        record = issued.get(id(receipt))
        if record is None or record[0]() is not receipt:
            return False
        if record[1] != receipt_snapshot(receipt):
            return False
        identity = record[2]()
        client = record[3]()
        if type(identity) is not identity_type or type(client) is not client_type:
            return False
        if record[1][0] != identity.session_context_id:
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
            raise provenance_error_type(
                "authenticated discovery requires exact BetfairReadOnlyClient"
            )
        if type(request) is not request_type:
            raise provenance_error_type(
                "authenticated discovery requires exact BetfairCatalogRequest"
            )
        if not class_dispatch_is_current():
            raise provenance_error_type(
                "canonical Betfair discovery/authenticated dispatch changed"
            )
        state = getattr(client, "__dict__", None)
        if type(state) is not dict or "_rpc" in state:
            raise provenance_error_type(
                "authenticated Betfair RPC dispatch is instance-rebound"
            )

        try:
            identity = resolve_identity(client)
            require_identity(identity, client=client)
            params = canonical_rpc_params(request)
            response = canonical_rpc(client, request.method, params)
            require_identity(identity, client=client)
        except (_account_identity.BetfairAccountIdentityError, BetfairReadOnlyError) as exc:
            raise provenance_error_type(
                "authenticated Betfair discovery acquisition failed"
            ) from exc

        if not class_dispatch_is_current() or type(response) is not rpc_result_type:
            raise provenance_error_type(
                "authenticated Betfair discovery returned non-canonical RPC evidence"
            )
        raw_response = response.raw_response
        evidence = response.evidence
        if type(raw_response) is not bytes or not raw_response:
            raise provenance_error_type(
                "authenticated Betfair discovery lacks immutable raw response bytes"
            )
        if sha256(raw_response).hexdigest() != evidence.source_payload_sha256:
            raise provenance_error_type(
                "authenticated Betfair discovery raw response digest mismatch"
            )
        try:
            observed_at = datetime.fromisoformat(
                evidence.observed_at.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as exc:
            raise provenance_error_type(
                "authenticated Betfair discovery observation time is invalid"
            ) from exc
        try:
            exchange = exchange_type(request, raw_response, observed_at)
        except (TypeError, ValueError) as exc:
            raise provenance_error_type(
                "authenticated Betfair discovery cannot bind canonical exchange"
            ) from exc
        if not class_dispatch_is_current():
            raise provenance_error_type(
                "canonical Betfair discovery/authenticated dispatch changed"
            )
        method, request_sha256, raw_response_sha256, observed_at_utc = (
            exchange_snapshot(exchange)
        )
        receipt = canonical_receipt_issue(
            transport_authority_ref=identity.session_context_id,
            method=method,
            request_sha256=request_sha256,
            raw_response_sha256=raw_response_sha256,
            observed_at_utc=observed_at_utc,
        )
        issue(receipt, identity=identity, client=client)
        if not is_authoritative(receipt):
            raise provenance_error_type(
                "authenticated Betfair discovery receipt issuance failed closed"
            )
        return BetfairAuthenticatedDiscoveryAcquisition(
            exchange=exchange,
            result=response.result,
            receipt=receipt,
            account_identity=identity,
        )

    def ordered_exchanges(
        evidence: BetfairDiscoveryAcquisitionEvidence,
    ) -> tuple[BetfairDiscoveryExchange, ...]:
        event_type_exchange = evidence_fields["event_type_exchange"].__get__(
            evidence, evidence_type
        )
        market_type_exchange = evidence_fields["market_type_exchange"].__get__(
            evidence, evidence_type
        )
        competition_exchange = evidence_fields["competition_exchange"].__get__(
            evidence, evidence_type
        )
        if competition_exchange is None:
            return (event_type_exchange, market_type_exchange)
        return (
            event_type_exchange,
            competition_exchange,
            market_type_exchange,
        )

    def assess_values(
        evidence: BetfairDiscoveryAcquisitionEvidence,
        *,
        receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
    ) -> tuple[bool, str, int, int, tuple[str, ...]]:
        if type(evidence) is not evidence_type:
            raise provenance_error_type(
                "evidence must be exact BetfairDiscoveryAcquisitionEvidence"
            )
        if any(type(item) is not receipt_type for item in receipts):
            raise provenance_error_type(
                "receipts must contain exact product-issued transport-origin receipts"
            )

        exchanges = ordered_exchanges(evidence)
        exchange_snapshots = tuple(exchange_snapshot(item) for item in exchanges)
        fingerprints = tuple(item[2] for item in exchange_snapshots)
        if not receipts:
            return (
                False,
                "NO_AUTHENTICATED_TRANSPORT_RECEIPTS",
                len(exchanges),
                0,
                fingerprints,
            )

        scope = evidence_fields["visibility_scope"].__get__(evidence, evidence_type)
        if type(scope) is not visibility_scope_type:
            raise provenance_error_type(
                "visibility_scope must be exact BetfairDiscoveryVisibilityScope"
            )
        account_scope_ref = account_scope_field.__get__(
            scope, visibility_scope_type
        )

        receipt_by_key: dict[
            tuple[str, str], tuple[str, str, str, str, str]
        ] = {}
        for receipt in receipts:
            if not is_authoritative(receipt):
                return (
                    False,
                    "UNISSUED_OR_STALE_AUTHENTICATED_TRANSPORT_RECEIPT",
                    len(exchanges),
                    0,
                    fingerprints,
                )
            receipt_values = receipt_snapshot(receipt)
            if receipt_values[0] != account_scope_ref:
                return (
                    False,
                    "AUTHENTICATED_TRANSPORT_ACCOUNT_SCOPE_MISMATCH",
                    len(exchanges),
                    0,
                    fingerprints,
                )
            key = (receipt_values[1], receipt_values[2])
            if key in receipt_by_key:
                raise provenance_error_type(
                    "duplicate transport-origin receipt for canonical request"
                )
            receipt_by_key[key] = receipt_values

        bound = 0
        for method, request_sha256, raw_response_sha256, observed_at_utc in (
            exchange_snapshots
        ):
            receipt_values = receipt_by_key.get((method, request_sha256))
            if receipt_values is None:
                return (
                    False,
                    "MISSING_AUTHENTICATED_TRANSPORT_RECEIPT",
                    len(exchanges),
                    bound,
                    fingerprints,
                )
            if (
                receipt_values[3] != raw_response_sha256
                or receipt_values[4] != observed_at_utc
            ):
                return (
                    False,
                    "TRANSPORT_RECEIPT_DOES_NOT_BIND_EXACT_EXCHANGE",
                    len(exchanges),
                    bound,
                    fingerprints,
                )
            bound += 1

        if len(receipt_by_key) != len(exchanges):
            return (
                False,
                "UNEXPECTED_AUTHENTICATED_TRANSPORT_RECEIPT",
                len(exchanges),
                bound,
                fingerprints,
            )

        return (
            True,
            "AUTHENTICATED_TRANSPORT_ORIGIN_PROVEN",
            len(exchanges),
            bound,
            fingerprints,
        )

    def assess(
        evidence: BetfairDiscoveryAcquisitionEvidence,
        *,
        receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
    ) -> BetfairDiscoveryTransportOriginAssessment:
        return assessment_type(*assess_values(evidence, receipts=receipts))

    def require(
        evidence: BetfairDiscoveryAcquisitionEvidence,
        *,
        receipts: Sequence[BetfairDiscoveryTransportOriginReceipt] = (),
    ) -> None:
        granted, reason, _, _, _ = assess_values(evidence, receipts=receipts)
        if not granted:
            raise provenance_error_type(
                "authenticated Betfair transport origin is not proven: " + reason
            )

    return acquire, is_authoritative, assess, require


(
    acquire_authenticated_betfair_discovery,
    is_authoritative_betfair_discovery_transport_receipt,
    assess_betfair_discovery_transport_origin,
    require_betfair_authenticated_transport_origin,
) = _make_transport_origin_authority()
del _make_transport_origin_authority
