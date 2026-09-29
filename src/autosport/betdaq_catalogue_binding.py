from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping, Sequence

from .betdaq_event_tree_request_wire import BetdaqEventSubTreeRequest
from .betdaq_event_tree_wire import (
    BetdaqDiscoveryMarket,
    BetdaqEventClassifier,
    parse_get_event_subtree_no_selections_response,
)
from .betdaq_rate_governor import BetdaqRateAdmission
from .betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqResolvedMarketBinding,
    Clock,
    _time,
)


@dataclass(frozen=True, slots=True)
class BetdaqCatalogueEvidence:
    requested_event_classifier_ids: tuple[int, ...]
    received_at: str
    response_sha256: str
    provider_call_id: str | None
    provider_created_at: str | None
    rate_admission_receipt: str
    provider_origin_verified: bool = False
    grants_execution_authority: bool = False
    grants_write_permission: bool = False


@dataclass(frozen=True, slots=True)
class _LocatedMarket:
    market: BetdaqDiscoveryMarket
    event_path_ids: tuple[int, ...]
    event_path_names: tuple[str, ...]


def _scope_event_id(value: str) -> int:
    if type(value) is not str or not value or not value.isascii() or not value.isdigit():
        raise ValueError(
            "live BETDAQ provider_event_id scope assertion must be a decimal event-classifier id"
        )
    result = int(value, 10)
    if str(result) != value:
        raise ValueError(
            "live BETDAQ provider_event_id scope assertion must use canonical decimal text"
        )
    return result


def _flatten_events(
    events: tuple[BetdaqEventClassifier, ...],
) -> dict[int, _LocatedMarket]:
    located: dict[int, _LocatedMarket] = {}

    def visit(
        event: BetdaqEventClassifier,
        path_ids: tuple[int, ...],
        path_names: tuple[str, ...],
    ) -> None:
        ids = (*path_ids, event.event_classifier_id)
        names = (*path_names, event.name)
        for market in event.markets:
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
    ) -> None:
        if not callable(getattr(transport, "get_event_subtree_no_selections", None)):
            raise TypeError(
                "transport must expose get_event_subtree_no_selections"
            )
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def resolve(
        self,
        bindings: Sequence[BetdaqMarketBinding],
    ) -> tuple[Mapping[int, BetdaqResolvedMarketBinding], BetdaqCatalogueEvidence]:
        values = tuple(bindings)
        if not values or any(type(item) is not BetdaqMarketBinding for item in values):
            raise ValueError("bindings must be non-empty BetdaqMarketBinding values")

        scope_by_market = {
            item.market_id: _scope_event_id(item.provider_event_id)
            for item in values
        }
        requested_roots = tuple(sorted(set(scope_by_market.values())))
        request = BetdaqEventSubTreeRequest(
            requested_roots,
            want_direct_descendents_only=False,
            want_play_markets=True,
        )
        payload = self._transport.get_event_subtree_no_selections(
            request,
            timeout_seconds=self._timeout_seconds,
        )
        if type(payload) is not bytes:
            raise TypeError("BETDAQ event-tree transport must return bytes")

        admission = getattr(self._transport, "last_rate_admission", None)
        if type(admission) is not BetdaqRateAdmission:
            raise TypeError(
                "BETDAQ event-tree acquisition requires canonical BetdaqRateAdmission"
            )
        if admission.method != "GetEventSubTreeNoSelections":
            raise ValueError("BETDAQ event-tree acquisition bound wrong rate admission")

        received_at = self._clock()
        _time(received_at, "catalogue_received_at")
        parsed = parse_get_event_subtree_no_selections_response(payload)
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
            response_sha256=response_sha256,
            provider_call_id=parsed.call_id,
            provider_created_at=parsed.provider_created_at_text,
            rate_admission_receipt=admission.receipt_sha256,
        )
        return resolved, evidence
