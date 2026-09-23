"""Acquisition provenance for canonical Betfair multi-sport discovery.

This module does not perform provider I/O and does not decide freshness.  It binds
an already-issued canonical discovery request, exact response bytes, observation
time, and the parsed provider-native inventory into deterministic evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping, Sequence

from .betfair_multisport_catalog import (
    LIST_COMPETITIONS,
    LIST_EVENT_TYPES,
    LIST_MARKET_TYPES,
    BetfairCatalogRequest,
    BetfairCompetition,
    BetfairEventType,
    BetfairMarketType,
)


SCHEMA_VERSION = 1
PROVIDER_ID = "BETFAIR"


class BetfairDiscoveryProvenanceError(ValueError):
    """Discovery acquisition evidence is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class BetfairDiscoveryVisibilityScope:
    """Non-secret product references for the provider-visible catalogue domain."""

    account_scope_ref: str
    application_scope_ref: str
    key_class: str
    jurisdiction: str

    def __post_init__(self) -> None:
        _token(self.account_scope_ref, "account_scope_ref")
        _token(self.application_scope_ref, "application_scope_ref")
        _token(self.key_class, "key_class")
        _token(self.jurisdiction, "jurisdiction")

    def projection(self) -> dict[str, str]:
        return {
            "account_scope_ref": self.account_scope_ref,
            "application_scope_ref": self.application_scope_ref,
            "key_class": self.key_class,
            "jurisdiction": self.jurisdiction,
        }


@dataclass(frozen=True, slots=True)
class BetfairDiscoveryExchange:
    """One exact read-only provider request/response observation."""

    request: BetfairCatalogRequest
    raw_response: bytes
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.request, BetfairCatalogRequest):
            raise BetfairDiscoveryProvenanceError(
                "request must be a canonical BetfairCatalogRequest"
            )
        if not isinstance(self.raw_response, bytes):
            raise BetfairDiscoveryProvenanceError("raw_response must be immutable bytes")
        if not self.raw_response:
            raise BetfairDiscoveryProvenanceError("raw_response must not be empty")
        _require_utc(self.observed_at, "observed_at")
        _canonical_json(self.request.rpc_params(), "request.params")

    @property
    def method(self) -> str:
        return self.request.method

    @property
    def canonical_request_json(self) -> str:
        return _canonical_json(
            {"method": self.request.method, "params": self.request.rpc_params()},
            "request",
        )

    @property
    def canonical_filter_json(self) -> str:
        market_filter = self.request.rpc_params().get("filter")
        if not isinstance(market_filter, Mapping):
            raise BetfairDiscoveryProvenanceError(
                "canonical discovery request is missing filter mapping"
            )
        return _canonical_json(market_filter, "request.filter")

    @property
    def request_sha256(self) -> str:
        return _sha256_text(self.canonical_request_json)

    @property
    def filter_sha256(self) -> str:
        return _sha256_text(self.canonical_filter_json)

    @property
    def raw_response_sha256(self) -> str:
        return sha256(self.raw_response).hexdigest()

    @property
    def raw_response_size_bytes(self) -> int:
        return len(self.raw_response)

    @property
    def observed_at_utc(self) -> str:
        return _utc_text(self.observed_at)

    def evidence_projection(self) -> dict[str, object]:
        return {
            "method": self.method,
            "canonical_request_json": self.canonical_request_json,
            "request_sha256": self.request_sha256,
            "canonical_filter_json": self.canonical_filter_json,
            "filter_sha256": self.filter_sha256,
            "raw_response_sha256": self.raw_response_sha256,
            "raw_response_size_bytes": self.raw_response_size_bytes,
            "observed_at_utc": self.observed_at_utc,
        }


@dataclass(frozen=True, slots=True)
class BetfairDiscoveryAcquisitionEvidence:
    """Deterministic evidence for one event-type -> market-type discovery slice.

    ``semantic_identity_sha256`` intentionally ignores counts, response bytes,
    timestamps and freshness policy.  ``acquisition_evidence_sha256`` includes
    all of those observations.  A locale/display-name change therefore cannot
    silently replace provider-native identity, while changed acquisition facts
    necessarily produce new evidence.
    """

    discovery_run_id: str
    visibility_scope: BetfairDiscoveryVisibilityScope
    event_type_exchange: BetfairDiscoveryExchange
    event_types: tuple[BetfairEventType, ...]
    selected_event_type_id: str
    market_type_exchange: BetfairDiscoveryExchange
    market_types: tuple[BetfairMarketType, ...]
    max_age_seconds: int
    competition_exchange: BetfairDiscoveryExchange | None = None
    competitions: tuple[BetfairCompetition, ...] = ()
    selected_competition_id: str | None = None

    def __post_init__(self) -> None:
        _token(self.discovery_run_id, "discovery_run_id")
        if not isinstance(self.visibility_scope, BetfairDiscoveryVisibilityScope):
            raise BetfairDiscoveryProvenanceError(
                "visibility_scope must be BetfairDiscoveryVisibilityScope"
            )
        if not isinstance(self.event_type_exchange, BetfairDiscoveryExchange):
            raise BetfairDiscoveryProvenanceError(
                "event_type_exchange must be BetfairDiscoveryExchange"
            )
        if not isinstance(self.market_type_exchange, BetfairDiscoveryExchange):
            raise BetfairDiscoveryProvenanceError(
                "market_type_exchange must be BetfairDiscoveryExchange"
            )
        if self.event_type_exchange.method != LIST_EVENT_TYPES:
            raise BetfairDiscoveryProvenanceError(
                "event_type_exchange must capture listEventTypes"
            )
        if self.market_type_exchange.method != LIST_MARKET_TYPES:
            raise BetfairDiscoveryProvenanceError(
                "market_type_exchange must capture listMarketTypes"
            )
        if self.market_type_exchange.observed_at < self.event_type_exchange.observed_at:
            raise BetfairDiscoveryProvenanceError(
                "market-type observation cannot precede event-type observation"
            )
        if not isinstance(self.event_types, tuple):
            raise BetfairDiscoveryProvenanceError("event_types must be a tuple")
        if not isinstance(self.market_types, tuple):
            raise BetfairDiscoveryProvenanceError("market_types must be a tuple")
        if not isinstance(self.competitions, tuple):
            raise BetfairDiscoveryProvenanceError("competitions must be a tuple")
        if any(not isinstance(item, BetfairEventType) for item in self.event_types):
            raise BetfairDiscoveryProvenanceError(
                "event_types must contain canonical BetfairEventType values"
            )
        if any(not isinstance(item, BetfairMarketType) for item in self.market_types):
            raise BetfairDiscoveryProvenanceError(
                "market_types must contain canonical BetfairMarketType values"
            )
        if any(not isinstance(item, BetfairCompetition) for item in self.competitions):
            raise BetfairDiscoveryProvenanceError(
                "competitions must contain canonical BetfairCompetition values"
            )
        if not self.event_types:
            raise BetfairDiscoveryProvenanceError(
                "event_types must retain the observed provider inventory"
            )
        event_ids = tuple(item.event_type_id for item in self.event_types)
        _unique(event_ids, "event_type_id")
        _token(self.selected_event_type_id, "selected_event_type_id")
        if self.selected_event_type_id not in event_ids:
            raise BetfairDiscoveryProvenanceError(
                "selected_event_type_id is absent from observed event types"
            )
        market_codes = tuple(item.market_type_code for item in self.market_types)
        _unique(market_codes, "market_type_code")
        if (
            not isinstance(self.max_age_seconds, int)
            or isinstance(self.max_age_seconds, bool)
            or self.max_age_seconds <= 0
        ):
            raise BetfairDiscoveryProvenanceError(
                "max_age_seconds must be a positive integer policy value"
            )
        market_filter = _decoded_filter(self.market_type_exchange)
        scoped_ids = market_filter.get("eventTypeIds")
        if not isinstance(scoped_ids, list) or scoped_ids != [self.selected_event_type_id]:
            raise BetfairDiscoveryProvenanceError(
                "listMarketTypes request must be scoped to exactly the selected eventTypeId"
            )

        market_competition_ids = market_filter.get("competitionIds")
        if self.competition_exchange is None:
            if self.competitions or self.selected_competition_id is not None:
                raise BetfairDiscoveryProvenanceError(
                    "competition inventory/selection requires listCompetitions exchange evidence"
                )
            if market_competition_ids is not None:
                raise BetfairDiscoveryProvenanceError(
                    "competition-scoped listMarketTypes requires listCompetitions acquisition evidence"
                )
        else:
            if not isinstance(self.competition_exchange, BetfairDiscoveryExchange):
                raise BetfairDiscoveryProvenanceError(
                    "competition_exchange must be BetfairDiscoveryExchange"
                )
            if self.competition_exchange.method != LIST_COMPETITIONS:
                raise BetfairDiscoveryProvenanceError(
                    "competition_exchange must capture listCompetitions"
                )
            if not (
                self.event_type_exchange.observed_at
                <= self.competition_exchange.observed_at
                <= self.market_type_exchange.observed_at
            ):
                raise BetfairDiscoveryProvenanceError(
                    "competition observation must be between event-type and market-type observations"
                )
            competition_filter = _decoded_filter(self.competition_exchange)
            competition_event_ids = competition_filter.get("eventTypeIds")
            if (
                not isinstance(competition_event_ids, list)
                or competition_event_ids != [self.selected_event_type_id]
            ):
                raise BetfairDiscoveryProvenanceError(
                    "listCompetitions request must be scoped to exactly the selected eventTypeId"
                )
            competition_ids = tuple(
                item.competition_id for item in self.competitions
            )
            _unique(competition_ids, "competition_id")
            if self.selected_competition_id is None:
                if market_competition_ids is not None:
                    raise BetfairDiscoveryProvenanceError(
                        "competition-scoped listMarketTypes requires selected_competition_id"
                    )
            else:
                _token(self.selected_competition_id, "selected_competition_id")
                if self.selected_competition_id not in competition_ids:
                    raise BetfairDiscoveryProvenanceError(
                        "selected_competition_id is absent from observed competitions"
                    )
                if (
                    not isinstance(market_competition_ids, list)
                    or market_competition_ids != [self.selected_competition_id]
                ):
                    raise BetfairDiscoveryProvenanceError(
                        "listMarketTypes request must be scoped to exactly the selected competitionId"
                    )

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def selected_event_type(self) -> BetfairEventType:
        return next(
            item for item in self.event_types
            if item.event_type_id == self.selected_event_type_id
        )

    def event_type_inventory_projection(self) -> list[dict[str, object]]:
        """Provider-native event-type identity/count inventory bound into this acquisition."""
        return [
            {"event_type_id": item.event_type_id, "market_count": item.market_count}
            for item in sorted(self.event_types, key=lambda x: x.event_type_id)
        ]

    def market_type_inventory_projection(self) -> list[dict[str, object]]:
        """Provider-native market-type identity/count inventory bound into this acquisition."""
        return [
            {"market_type_code": item.market_type_code, "market_count": item.market_count}
            for item in sorted(self.market_types, key=lambda x: x.market_type_code)
        ]

    def competition_inventory_projection(self) -> list[dict[str, object]]:
        """Provider-native competition identity/count inventory bound into this acquisition."""
        return [
            {"competition_id": item.competition_id, "market_count": item.market_count}
            for item in sorted(self.competitions, key=lambda x: x.competition_id)
        ]

    @property
    def semantic_identity_sha256(self) -> str:
        """Provider-native identity only; not evidence that a current acquisition occurred."""
        payload = {
            "provider_id": self.provider_id,
            "selected_event_type_id": self.selected_event_type_id,
            "market_type_codes": sorted(
                item.market_type_code for item in self.market_types
            ),
        }
        if self.selected_competition_id is not None:
            payload["selected_competition_id"] = self.selected_competition_id
        return _sha256_text(_canonical_json(payload, "semantic_identity"))

    @property
    def acquisition_evidence_sha256(self) -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "discovery_run_id": self.discovery_run_id,
            "visibility_scope": self.visibility_scope.projection(),
            "event_type_exchange": self.event_type_exchange.evidence_projection(),
            "event_types": self.event_type_inventory_projection(),
            "selected_event_type_id": self.selected_event_type_id,
            "market_type_exchange": self.market_type_exchange.evidence_projection(),
            "market_types": self.market_type_inventory_projection(),
            "max_age_seconds": self.max_age_seconds,
        }
        if self.competition_exchange is not None:
            payload["competition_exchange"] = self.competition_exchange.evidence_projection()
            payload["competitions"] = self.competition_inventory_projection()
            payload["selected_competition_id"] = self.selected_competition_id
        return _sha256_text(_canonical_json(payload, "acquisition_evidence"))

    def projection(self) -> dict[str, object]:
        """Durable compact projection; raw provider bytes remain external evidence."""
        projection: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "discovery_run_id": self.discovery_run_id,
            "visibility_scope": self.visibility_scope.projection(),
            "selected_event_type_id": self.selected_event_type_id,
            "semantic_identity_sha256": self.semantic_identity_sha256,
            "acquisition_evidence_sha256": self.acquisition_evidence_sha256,
            "event_type_inventory": self.event_type_inventory_projection(),
            "market_type_inventory": self.market_type_inventory_projection(),
            "event_type_exchange": self.event_type_exchange.evidence_projection(),
            "market_type_exchange": self.market_type_exchange.evidence_projection(),
            "max_age_seconds": self.max_age_seconds,
            "grants_freshness_authority": False,
            "grants_execution_authority": False,
        }
        if self.competition_exchange is not None:
            projection["competition_inventory"] = self.competition_inventory_projection()
            projection["competition_exchange"] = (
                self.competition_exchange.evidence_projection()
            )
            projection["selected_competition_id"] = self.selected_competition_id
        return projection


def build_betfair_discovery_acquisition_evidence(
    *,
    discovery_run_id: str,
    visibility_scope: BetfairDiscoveryVisibilityScope,
    event_type_request: BetfairCatalogRequest,
    event_type_raw_response: bytes,
    event_type_observed_at: datetime,
    event_types: Sequence[BetfairEventType],
    selected_event_type_id: str,
    market_type_request: BetfairCatalogRequest,
    market_type_raw_response: bytes,
    market_type_observed_at: datetime,
    market_types: Sequence[BetfairMarketType],
    max_age_seconds: int,
    competition_request: BetfairCatalogRequest | None = None,
    competition_raw_response: bytes | None = None,
    competition_observed_at: datetime | None = None,
    competitions: Sequence[BetfairCompetition] = (),
    selected_competition_id: str | None = None,
) -> BetfairDiscoveryAcquisitionEvidence:
    """Capture immutable evidence from canonical #1137 request/parser outputs."""
    competition_parts = (
        competition_request is not None,
        competition_raw_response is not None,
        competition_observed_at is not None,
    )
    if any(competition_parts) and not all(competition_parts):
        raise BetfairDiscoveryProvenanceError(
            "competition request/raw response/observation time must be supplied together"
        )
    competition_exchange = (
        None
        if competition_request is None
        else BetfairDiscoveryExchange(
            competition_request,
            competition_raw_response,
            competition_observed_at,
        )
    )
    return BetfairDiscoveryAcquisitionEvidence(
        discovery_run_id=discovery_run_id,
        visibility_scope=visibility_scope,
        event_type_exchange=BetfairDiscoveryExchange(
            event_type_request, event_type_raw_response, event_type_observed_at
        ),
        event_types=tuple(event_types),
        selected_event_type_id=selected_event_type_id,
        market_type_exchange=BetfairDiscoveryExchange(
            market_type_request, market_type_raw_response, market_type_observed_at
        ),
        market_types=tuple(market_types),
        max_age_seconds=max_age_seconds,
        competition_exchange=competition_exchange,
        competitions=tuple(competitions),
        selected_competition_id=selected_competition_id,
    )


def _decoded_filter(exchange: BetfairDiscoveryExchange) -> dict[str, object]:
    decoded = json.loads(exchange.canonical_filter_json)
    if not isinstance(decoded, dict):
        raise BetfairDiscoveryProvenanceError("canonical filter must decode to an object")
    return decoded


def _canonical_json(value: object, field: str) -> str:
    _validate_json(value, field)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_json(value: object, field: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise BetfairDiscoveryProvenanceError(
                    f"{field} contains a non-string/empty object key"
                )
            _validate_json(item, f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, f"{field}[{index}]")
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise BetfairDiscoveryProvenanceError(
                f"{field} contains a non-finite JSON number"
            )
        return
    if value is None or isinstance(value, (str, int, bool)):
        return
    raise BetfairDiscoveryProvenanceError(
        f"{field} contains a non-JSON-compatible value"
    )


def _require_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise BetfairDiscoveryProvenanceError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise BetfairDiscoveryProvenanceError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise BetfairDiscoveryProvenanceError(f"{field} must use UTC offset +00:00")
    return value


def _utc_text(value: datetime) -> str:
    _require_utc(value, "observed_at")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _token(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
    ):
        raise BetfairDiscoveryProvenanceError(
            f"{field} must be a non-empty trimmed printable-ASCII token"
        )
    return value


def _unique(values: Sequence[str], field: str) -> None:
    if len(set(values)) != len(values):
        raise BetfairDiscoveryProvenanceError(f"duplicate {field}")


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
