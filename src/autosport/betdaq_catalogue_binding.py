from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import hashlib
import json
from typing import Mapping, Sequence

from .betdaq_event_tree_request_wire import (
    BETDAQ_PROVIDER_LONG_MAX,
    BetdaqEventSubTreeRequest,
)
from .betdaq_event_tree_wire import (
    BetdaqDiscoveryMarket,
    BetdaqEventClassifier,
    parse_get_event_subtree_no_selections_response,
)
from .betdaq_rate_governor import BetdaqRateAdmission
from .betdaq_readonly_market_wire import BetdaqSoapProtocolError
from .betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqResolvedMarketBinding,
    BetdaqTransientTransportError,
    Clock,
    _time,
)
from .providers import ProviderUnavailableError


@dataclass(frozen=True, slots=True)
class BetdaqCatalogueEvidence:
    requested_event_classifier_ids: tuple[int, ...]
    received_at: str
    request_fingerprint: str
    response_sha256: str
    provider_call_id: str | None
    provider_created_at: str | None
    rate_admission_receipts: tuple[str, ...]
    provider_origin_verified: bool = False
    grants_execution_authority: bool = False
    grants_write_permission: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.requested_event_classifier_ids) is not tuple
            or not self.requested_event_classifier_ids
            or any(
                type(value) is not int or value < 0
                for value in self.requested_event_classifier_ids
            )
            or len(set(self.requested_event_classifier_ids))
            != len(self.requested_event_classifier_ids)
        ):
            raise ValueError(
                "requested_event_classifier_ids must be canonical provider ids"
            )
        _time(self.received_at, "catalogue received_at")
        for value, field in (
            (self.request_fingerprint, "catalogue request_fingerprint"),
            (self.response_sha256, "catalogue response_sha256"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{field} must be lowercase SHA-256")
        if (
            type(self.rate_admission_receipts) is not tuple
            or not self.rate_admission_receipts
            or any(
                type(value) is not str
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
                for value in self.rate_admission_receipts
            )
        ):
            raise ValueError(
                "catalogue rate_admission_receipts must be non-empty SHA-256 tuple"
            )
        if self.provider_origin_verified is not False:
            raise ValueError("catalogue evidence cannot claim provider-origin verification")
        if self.grants_execution_authority is not False:
            raise ValueError("catalogue evidence cannot grant execution authority")
        if self.grants_write_permission is not False:
            raise ValueError("catalogue evidence cannot grant write permission")


@dataclass(frozen=True, slots=True)
class _LocatedMarket:
    market: BetdaqDiscoveryMarket
    event_path_ids: tuple[int, ...]
    event_path_names: tuple[str, ...]


def betdaq_event_scope_id(value: str) -> int:
    if type(value) is not str or not value or not value.isascii() or not value.isdigit():
        raise ValueError(
            "live BETDAQ provider_event_id scope assertion must be a decimal event-classifier id"
        )
    result = int(value, 10)
    if str(result) != value or result > BETDAQ_PROVIDER_LONG_MAX:
        raise ValueError(
            "live BETDAQ provider_event_id event-classifier id must use canonical provider long text"
        )
    return result


def _provider_event_id(value: int) -> int:
    try:
        return betdaq_event_scope_id(str(value))
    except ValueError as exc:
        raise BetdaqSoapProtocolError(
            "BETDAQ catalogue event identity is outside provider long domain"
        ) from exc


def _flatten_events(
    events: tuple[BetdaqEventClassifier, ...],
) -> dict[int, _LocatedMarket]:
    located: dict[int, _LocatedMarket] = {}

    def visit(
        event: BetdaqEventClassifier,
        path_ids: tuple[int, ...],
        path_names: tuple[str, ...],
    ) -> None:
        event_id = _provider_event_id(event.event_classifier_id)
        ids = (*path_ids, event_id)
        names = (*path_names, event.name)
        for market in event.markets:
            _provider_event_id(market.event_classifier_id)
            if market.market_id in located:
                raise ValueError("BETDAQ catalogue contains duplicate market identity")
            located[market.market_id] = _LocatedMarket(market, ids, names)
        for child in event.children:
            visit(child, ids, names)

    for event in events:
        visit(event, (), ())
    return located


class BetdaqLiveCatalogueResolver:
    """Resolve caller market scope against one provider-returned event-tree snapshot.

    Caller bindings choose requested scope only. Canonical event identity and the
    provider market-type assertion are derived from the returned catalogue tree.
    Canonical sport and product MarketType remain deliberately unmapped here.
    """

    def __init__(
        self,
        *,
        transport: object,
        timeout_seconds: float,
        clock: Clock,
        max_attempts: int,
        max_message_age_seconds: int | None = None,
    ) -> None:
        if not callable(getattr(transport, "get_event_subtree_no_selections", None)):
            raise TypeError(
                "transport must expose get_event_subtree_no_selections"
            )
        if type(max_attempts) is not int or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_message_age_seconds is not None and (
            type(max_message_age_seconds) is not int
            or max_message_age_seconds <= 0
        ):
            raise ValueError("max_message_age_seconds must be positive or None")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._max_attempts = max_attempts
        self._max_message_age_seconds = max_message_age_seconds

    def resolve(
        self,
        bindings: Sequence[BetdaqMarketBinding],
    ) -> tuple[Mapping[int, BetdaqResolvedMarketBinding], BetdaqCatalogueEvidence]:
        values = tuple(bindings)
        if not values or any(type(item) is not BetdaqMarketBinding for item in values):
            raise ValueError("bindings must be non-empty BetdaqMarketBinding values")

        scope_by_market = {
            item.market_id: betdaq_event_scope_id(item.provider_event_id)
            for item in values
        }
        requested_roots = tuple(sorted(set(scope_by_market.values())))
        request = BetdaqEventSubTreeRequest(
            requested_roots,
            want_direct_descendents_only=False,
            want_play_markets=True,
        )
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "operation": "GetEventSubTreeNoSelections",
                    "event_classifier_ids": request.event_classifier_ids,
                    "want_direct_descendents_only": (
                        request.want_direct_descendents_only
                    ),
                    "want_play_markets": request.want_play_markets,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipts: list[str] = []
        payload: bytes | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                candidate = self._transport.get_event_subtree_no_selections(
                    request,
                    timeout_seconds=self._timeout_seconds,
                )
            except (
                BetdaqTransientTransportError,
                TimeoutError,
                ConnectionError,
            ) as exc:
                admission = getattr(self._transport, "last_rate_admission", None)
                if type(admission) is BetdaqRateAdmission:
                    if admission.method != "GetEventSubTreeNoSelections":
                        raise ValueError(
                            "BETDAQ event-tree acquisition bound wrong rate admission"
                        ) from exc
                    if (
                        not receipts
                        or receipts[-1] != admission.receipt_sha256
                    ):
                        receipts.append(admission.receipt_sha256)
                if attempt == self._max_attempts:
                    raise ProviderUnavailableError(
                        "BETDAQ event catalogue unavailable after "
                        f"{attempt} bounded attempts"
                    ) from exc
                continue

            if type(candidate) is not bytes:
                raise TypeError("BETDAQ event-tree transport must return bytes")
            admission = getattr(self._transport, "last_rate_admission", None)
            if type(admission) is not BetdaqRateAdmission:
                raise TypeError(
                    "BETDAQ event-tree acquisition requires canonical BetdaqRateAdmission"
                )
            if admission.method != "GetEventSubTreeNoSelections":
                raise ValueError(
                    "BETDAQ event-tree acquisition bound wrong rate admission"
                )
            if not receipts or receipts[-1] != admission.receipt_sha256:
                receipts.append(admission.receipt_sha256)
            payload = candidate
            break

        if payload is None:
            raise AssertionError("event-tree retry loop terminated without payload")

        received, received_at = _time(
            self._clock(),
            "catalogue_received_at",
        )
        parsed = parse_get_event_subtree_no_selections_response(payload)
        if parsed.provider_created_at is not None:
            age = (
                received.astimezone(timezone.utc)
                - parsed.provider_created_at.astimezone(timezone.utc)
            ).total_seconds()
            if age < 0:
                raise BetdaqSoapProtocolError(
                    "BETDAQ catalogue WS-Security Created is in the future"
                )
            if (
                self._max_message_age_seconds is not None
                and age > self._max_message_age_seconds
            ):
                raise BetdaqSoapProtocolError(
                    "BETDAQ catalogue WS-Security message envelope is stale"
                )
        located = _flatten_events(parsed.event_classifiers)
        response_sha256 = hashlib.sha256(payload).hexdigest()

        resolved: dict[int, BetdaqResolvedMarketBinding] = {}
        for binding in values:
            located_market = located.get(binding.market_id)
            if located_market is None:
                raise ValueError(
                    f"BETDAQ catalogue did not contain requested market {binding.market_id}"
                )
            expected_root = scope_by_market[binding.market_id]
            if expected_root not in located_market.event_path_ids:
                raise ValueError(
                    "BETDAQ catalogue market is outside caller-requested event scope"
                )
            market = located_market.market
            resolved[binding.market_id] = BetdaqResolvedMarketBinding(
                market_id=binding.market_id,
                provider_event_id=str(market.event_classifier_id),
                provider_market_type_code=market.market_type_code,
                event_path_ids=located_market.event_path_ids,
                event_path_names=located_market.event_path_names,
                catalogue_response_sha256=response_sha256,
            )

        evidence = BetdaqCatalogueEvidence(
            requested_event_classifier_ids=requested_roots,
            received_at=received_at,
            request_fingerprint=request_fingerprint,
            response_sha256=response_sha256,
            provider_call_id=parsed.call_id,
            provider_created_at=parsed.provider_created_at_text,
            rate_admission_receipts=tuple(receipts),
        )
        return resolved, evidence
