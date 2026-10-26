"""Fail-closed Betfair portfolio P&L coverage proof.

The canonical HTTP/RPC authority remains :mod:`autosport.betfair_account_readonly`.
This module only partitions a declared market universe, asks that authority for
coverage evidence, and proves that no market silently disappeared.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Mapping, Sequence

from .betfair_account_readonly import (
    BetfairClearedMarketPnlCoveragePage,
    BetfairMarketPnlCoverageBatch,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
)


MAX_OPEN_PNL_MARKETS_PER_REQUEST = 50


class BetfairPnlCoverageError(ValueError):
    """Raised when provider evidence cannot prove complete P&L coverage."""


class MarketStatus(str, Enum):
    INACTIVE = "INACTIVE"
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class CoverageAuthority(str, Enum):
    OPEN_ODDS_PNL = "listMarketProfitAndLoss"
    CLOSED_CLEARED_ORDERS = "listClearedOrders"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class MarketDescriptor:
    market_id: str
    status: MarketStatus
    betting_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.market_id, str) or not self.market_id or self.market_id != self.market_id.strip():
            raise BetfairPnlCoverageError("market_id must be a non-empty trimmed string")
        if not isinstance(self.status, MarketStatus):
            raise BetfairPnlCoverageError("status must be MarketStatus")
        if not isinstance(self.betting_type, str) or not self.betting_type or self.betting_type != self.betting_type.strip():
            raise BetfairPnlCoverageError("betting_type must be a non-empty trimmed string")

    @property
    def normalized_betting_type(self) -> str:
        return self.betting_type.upper()

    @property
    def authority(self) -> CoverageAuthority:
        if self.status is MarketStatus.CLOSED:
            return CoverageAuthority.CLOSED_CLEARED_ORDERS
        if self.status is MarketStatus.OPEN and self.normalized_betting_type == "ODDS":
            return CoverageAuthority.OPEN_ODDS_PNL
        return CoverageAuthority.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class ScopeIdentity:
    provider: str
    account_id_hash: str
    evidence_not_before_utc: str
    include_settled_bets: bool
    include_bsp_bets: bool
    net_of_commission: bool

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or self.provider.strip().upper() != "BETFAIR":
            raise BetfairPnlCoverageError("provider-specific coverage requires BETFAIR")
        if (
            not isinstance(self.account_id_hash, str)
            or len(self.account_id_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.account_id_hash)
        ):
            raise BetfairPnlCoverageError(
                "account_id_hash must be lowercase 64-character SHA-256"
            )
        try:
            parsed = datetime.fromisoformat(
                self.evidence_not_before_utc.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as exc:
            raise BetfairPnlCoverageError(
                "evidence_not_before_utc must be ISO-8601 UTC"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise BetfairPnlCoverageError(
                "evidence_not_before_utc must carry an explicit UTC offset"
            )
        for name in ("include_settled_bets", "include_bsp_bets", "net_of_commission"):
            if type(getattr(self, name)) is not bool:
                raise BetfairPnlCoverageError(f"{name} must be boolean")

    def canonical_payload(self, markets: Sequence[MarketDescriptor]) -> dict[str, object]:
        rows = [
            {
                "market_id": market.market_id,
                "status": market.status.value,
                "betting_type": market.normalized_betting_type,
                "authority": market.authority.value,
            }
            for market in sorted(
                markets,
                key=lambda item: (item.market_id, item.status.value, item.normalized_betting_type),
            )
        ]
        return {
            "provider": "BETFAIR",
            "account_id_hash": self.account_id_hash,
            "evidence_not_before_utc": self.evidence_not_before_utc,
            "include_settled_bets": self.include_settled_bets,
            "include_bsp_bets": self.include_bsp_bets,
            "net_of_commission": self.net_of_commission,
            "markets": rows,
        }

    def digest(self, markets: Sequence[MarketDescriptor]) -> str:
        raw = json.dumps(
            self.canonical_payload(markets),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class CoverageWitness:
    scope_digest: str
    evidence_digest: str
    open_odds_market_ids: tuple[str, ...]
    closed_market_ids: tuple[str, ...]
    unsupported_market_ids: tuple[str, ...]
    open_batches: tuple[BetfairMarketPnlCoverageBatch, ...]
    closed_pages: tuple[BetfairClearedMarketPnlCoveragePage, ...]

def _unique_market_map(markets: Sequence[MarketDescriptor]) -> Mapping[str, MarketDescriptor]:
    result: dict[str, MarketDescriptor] = {}
    for market in markets:
        if not isinstance(market, MarketDescriptor):
            raise BetfairPnlCoverageError("markets must contain MarketDescriptor values")
        if market.market_id in result:
            raise BetfairPnlCoverageError(f"duplicate market {market.market_id}")
        result[market.market_id] = market
    return result


def build_open_market_batches(markets: Sequence[MarketDescriptor]) -> tuple[tuple[str, ...], ...]:
    market_map = _unique_market_map(markets)
    ids = sorted(
        market.market_id
        for market in market_map.values()
        if market.authority is CoverageAuthority.OPEN_ODDS_PNL
    )
    return tuple(
        tuple(ids[index : index + MAX_OPEN_PNL_MARKETS_PER_REQUEST])
        for index in range(0, len(ids), MAX_OPEN_PNL_MARKETS_PER_REQUEST)
    )


def _validate_evidence_identity_and_time(
    *,
    account_id_hash: str,
    observed_at: str,
    scope: ScopeIdentity,
    field: str,
) -> None:
    if account_id_hash != scope.account_id_hash:
        raise BetfairPnlCoverageError(f"{field} account identity mismatch")
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    threshold = datetime.fromisoformat(
        scope.evidence_not_before_utc.replace("Z", "+00:00")
    )
    if observed < threshold:
        raise BetfairPnlCoverageError(
            f"{field} predates the evidence_not_before_utc freshness fence"
        )


def _validate_open_evidence(
    expected_batches: Sequence[Sequence[str]],
    batches: Sequence[BetfairMarketPnlCoverageBatch],
    scope: ScopeIdentity,
) -> None:
    if len(expected_batches) != len(batches):
        raise BetfairPnlCoverageError("open P&L batch count mismatch")
    seen: set[str] = set()
    for index, (expected, batch) in enumerate(zip(expected_batches, batches, strict=True)):
        expected_tuple = tuple(expected)
        if not isinstance(batch, BetfairMarketPnlCoverageBatch):
            raise BetfairPnlCoverageError("open evidence must be canonical Betfair coverage batches")
        try:
            batch.assert_authoritative()
        except BetfairReadOnlyError as exc:
            raise BetfairPnlCoverageError(
                "open evidence was not issued by canonical BetfairReadOnlyClient"
            ) from exc
        _validate_evidence_identity_and_time(
            account_id_hash=batch.account_id_hash,
            observed_at=batch.evidence.observed_at,
            scope=scope,
            field=f"open batch {index}",
        )
        if batch.requested_market_ids != expected_tuple:
            raise BetfairPnlCoverageError(f"open batch {index} request partition mismatch")
        if set(batch.returned_market_ids) != set(expected_tuple):
            raise BetfairPnlCoverageError(f"open batch {index} omitted or added markets")
        if (
            batch.include_settled_bets != scope.include_settled_bets
            or batch.include_bsp_bets != scope.include_bsp_bets
            or batch.net_of_commission != scope.net_of_commission
        ):
            raise BetfairPnlCoverageError(f"open batch {index} P&L view flags mismatch")
        overlap = seen.intersection(expected_tuple)
        if overlap:
            raise BetfairPnlCoverageError(f"open request batches overlap: {sorted(overlap)}")
        seen.update(expected_tuple)


def _validate_closed_evidence(
    closed_market_ids: Sequence[str],
    pages: Sequence[BetfairClearedMarketPnlCoveragePage],
    scope: ScopeIdentity,
) -> None:
    expected = set(closed_market_ids)
    if not expected:
        if pages:
            raise BetfairPnlCoverageError("closed pages present for empty CLOSED scope")
        return
    if not pages:
        raise BetfairPnlCoverageError("missing SETTLED/MARKET evidence for CLOSED scope")

    returned: set[str] = set()
    expected_from = 0
    terminal_seen = False
    expected_request = tuple(sorted(expected))
    for index, page in enumerate(pages):
        if not isinstance(page, BetfairClearedMarketPnlCoveragePage):
            raise BetfairPnlCoverageError("closed evidence must be canonical Betfair coverage pages")
        try:
            page.assert_authoritative()
        except BetfairReadOnlyError as exc:
            raise BetfairPnlCoverageError(
                "closed evidence was not issued by canonical BetfairReadOnlyClient"
            ) from exc
        _validate_evidence_identity_and_time(
            account_id_hash=page.account_id_hash,
            observed_at=page.evidence.observed_at,
            scope=scope,
            field=f"closed page {index}",
        )
        if terminal_seen:
            raise BetfairPnlCoverageError("closed page appears after terminal page")
        if tuple(sorted(page.requested_market_ids)) != expected_request:
            raise BetfairPnlCoverageError(f"closed page {index} request scope mismatch")
        if page.from_record != expected_from:
            raise BetfairPnlCoverageError(
                f"closed pagination gap/overlap: expected fromRecord={expected_from}, got {page.from_record}"
            )
        page_ids = set(page.returned_market_ids)
        duplicate = returned.intersection(page_ids)
        if duplicate:
            raise BetfairPnlCoverageError(
                f"duplicate MARKET roll-up row across closed pages: {sorted(duplicate)}"
            )
        returned.update(page_ids)
        expected_from += len(page.returned_market_ids)
        terminal_seen = not page.more_available

    if not terminal_seen:
        raise BetfairPnlCoverageError("closed pagination has no terminal page")
    missing = expected - returned
    if missing:
        raise BetfairPnlCoverageError(
            f"SETTLED/MARKET evidence omitted CLOSED markets: {sorted(missing)}"
        )


def _evidence_digest(
    scope_digest: str,
    open_batches: Sequence[BetfairMarketPnlCoverageBatch],
    closed_pages: Sequence[BetfairClearedMarketPnlCoveragePage],
) -> str:
    payload = {
        "scope_digest": scope_digest,
        "open_batches": [
            {
                "account_id_hash": batch.account_id_hash,
                "requested_market_ids": list(batch.requested_market_ids),
                "returned_market_ids": list(batch.returned_market_ids),
                "include_settled_bets": batch.include_settled_bets,
                "include_bsp_bets": batch.include_bsp_bets,
                "net_of_commission": batch.net_of_commission,
                "observed_at": batch.evidence.observed_at,
                "source_payload_sha256": batch.evidence.source_payload_sha256,
            }
            for batch in open_batches
        ],
        "closed_pages": [
            {
                "account_id_hash": page.account_id_hash,
                "requested_market_ids": list(page.requested_market_ids),
                "returned_market_ids": list(page.returned_market_ids),
                "from_record": page.from_record,
                "requested_record_count": page.requested_record_count,
                "more_available": page.more_available,
                "observed_at": page.evidence.observed_at,
                "source_payload_sha256": page.evidence.source_payload_sha256,
            }
            for page in closed_pages
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_coverage_witness(
    *,
    scope: ScopeIdentity,
    markets: Sequence[MarketDescriptor],
    open_batches: Sequence[BetfairMarketPnlCoverageBatch],
    closed_pages: Sequence[BetfairClearedMarketPnlCoveragePage],
) -> CoverageWitness:
    if not isinstance(scope, ScopeIdentity):
        raise BetfairPnlCoverageError("scope must be ScopeIdentity")
    market_map = _unique_market_map(markets)
    canonical_markets = tuple(market_map.values())
    scope_digest = scope.digest(canonical_markets)

    open_ids = tuple(
        sorted(
            market.market_id
            for market in market_map.values()
            if market.authority is CoverageAuthority.OPEN_ODDS_PNL
        )
    )
    closed_ids = tuple(
        sorted(
            market.market_id
            for market in market_map.values()
            if market.authority is CoverageAuthority.CLOSED_CLEARED_ORDERS
        )
    )
    unsupported_ids = tuple(
        sorted(
            market.market_id
            for market in market_map.values()
            if market.authority is CoverageAuthority.UNSUPPORTED
        )
    )

    _validate_open_evidence(build_open_market_batches(canonical_markets), open_batches, scope)
    _validate_closed_evidence(closed_ids, closed_pages, scope)

    return CoverageWitness(
        scope_digest=scope_digest,
        evidence_digest=_evidence_digest(scope_digest, open_batches, closed_pages),
        open_odds_market_ids=open_ids,
        closed_market_ids=closed_ids,
        unsupported_market_ids=unsupported_ids,
        open_batches=tuple(open_batches),
        closed_pages=tuple(closed_pages),
    )


def collect_coverage_witness(
    *,
    client: BetfairReadOnlyClient,
    scope: ScopeIdentity,
    markets: Sequence[MarketDescriptor],
    settled_from: str | None = None,
    closed_record_count: int = 1000,
    max_closed_pages: int = 100,
) -> CoverageWitness:
    """Collect one context-bound coverage witness through the canonical read-only client."""
    if not isinstance(client, BetfairReadOnlyClient):
        raise TypeError("client must be BetfairReadOnlyClient")
    if not isinstance(scope, ScopeIdentity):
        raise BetfairPnlCoverageError("scope must be ScopeIdentity")
    if not isinstance(max_closed_pages, int) or isinstance(max_closed_pages, bool) or max_closed_pages <= 0:
        raise BetfairPnlCoverageError("max_closed_pages must be a positive integer")

    market_map = _unique_market_map(markets)
    canonical_markets = tuple(market_map.values())
    open_batches = tuple(
        client.read_market_profit_and_loss_coverage(
            market_ids=batch,
            include_settled_bets=scope.include_settled_bets,
            include_bsp_bets=scope.include_bsp_bets,
            net_of_commission=scope.net_of_commission,
        )
        for batch in build_open_market_batches(canonical_markets)
    )

    closed_ids = tuple(
        sorted(
            market.market_id
            for market in canonical_markets
            if market.authority is CoverageAuthority.CLOSED_CLEARED_ORDERS
        )
    )
    closed_pages: list[BetfairClearedMarketPnlCoveragePage] = []
    if closed_ids:
        offset = 0
        for _ in range(max_closed_pages):
            page = client.read_cleared_market_profit_and_loss_coverage_page(
                market_ids=closed_ids,
                from_record=offset,
                record_count=closed_record_count,
                settled_from=settled_from,
            )
            closed_pages.append(page)
            if not page.more_available:
                break
            offset += len(page.returned_market_ids)
        else:
            raise BetfairPnlCoverageError(
                "closed P&L pagination exceeded max_closed_pages while more data remained"
            )

    return build_coverage_witness(
        scope=scope,
        markets=canonical_markets,
        open_batches=open_batches,
        closed_pages=tuple(closed_pages),
    )


def verify_coverage_witness(
    *,
    witness: CoverageWitness,
    scope: ScopeIdentity,
    markets: Sequence[MarketDescriptor],
) -> CoverageWitness:
    """Rebuild an untrusted in-memory witness against the exact external scope.

    Provider-issued DTO authority is process-local. Durable replay must first
    re-establish provider evidence through the product-owned evidence store.
    """
    if not isinstance(witness, CoverageWitness):
        raise BetfairPnlCoverageError("witness must be CoverageWitness")
    rebuilt = build_coverage_witness(
        scope=scope,
        markets=markets,
        open_batches=witness.open_batches,
        closed_pages=witness.closed_pages,
    )
    if rebuilt != witness:
        raise BetfairPnlCoverageError(
            "coverage witness fields do not match canonical rebuilt proof"
        )
    return rebuilt


def assert_complete(
    witness: CoverageWitness,
    *,
    scope: ScopeIdentity,
    markets: Sequence[MarketDescriptor],
) -> None:
    verified = verify_coverage_witness(witness=witness, scope=scope, markets=markets)
    if verified.unsupported_market_ids:
        raise BetfairPnlCoverageError(
            "provider cannot certify non-ODDS or non-OPEN market P&L via this authority; "
            f"unsupported markets: {list(verified.unsupported_market_ids)}"
        )
