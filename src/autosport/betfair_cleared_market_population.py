from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json
from typing import Any, Mapping
from weakref import ref

from . import _betfair_market_commission_origin_binding as _commission_origin
from . import betfair_account_readonly as _betfair
from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
)
from .betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)


SOURCE_FAMILY = "betfair.cleared-market-population.v1"
_STATUSES = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
_FIXED_VENUE_ID = "betfair"


class BetfairClearedMarketPopulationError(RuntimeError):
    """Raised when complete provider BET-population authority cannot be proven."""


@dataclass(frozen=True, slots=True)
class ClearedMarketBetRow:
    bet_id: str
    market_id: str
    selection_id: int
    side: str
    bet_status: str
    placed_date: str
    settled_date: str
    price_requested: Decimal
    price_matched: Decimal
    size_settled: Decimal
    profit: Decimal
    customer_order_ref: str | None
    customer_strategy_ref: str | None
    event_id: str | None

    @classmethod
    def from_observation(
        cls, value: BetfairClearedOrderObservation
    ) -> "ClearedMarketBetRow":
        if type(value) is not BetfairClearedOrderObservation:
            raise BetfairClearedMarketPopulationError(
                "cleared BET row must be canonical Betfair provider observation"
            )
        return cls(
            bet_id=value.bet_id,
            market_id=value.market_id,
            selection_id=value.selection_id,
            side=value.side,
            bet_status=value.bet_status,
            placed_date=value.placed_date,
            settled_date=value.settled_date,
            price_requested=value.price_requested,
            price_matched=value.price_matched,
            size_settled=value.size_settled,
            profit=value.profit,
            customer_order_ref=value.customer_order_ref,
            customer_strategy_ref=value.customer_strategy_ref,
            event_id=value.event_id,
        )

    def payload(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "bet_status": self.bet_status,
            "placed_date": self.placed_date,
            "settled_date": self.settled_date,
            "price_requested": _decimal_text(self.price_requested),
            "price_matched": _decimal_text(self.price_matched),
            "size_settled": _decimal_text(self.size_settled),
            "profit": _decimal_text(self.profit),
            "customer_order_ref": self.customer_order_ref,
            "customer_strategy_ref": self.customer_strategy_ref,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class ClearedMarketPageWitness:
    pass_index: int
    bet_status: str
    from_record: int
    row_count: int
    more_available: bool
    response_sha256: str
    observed_at: str

    def payload(self) -> dict[str, object]:
        return {
            "pass_index": self.pass_index,
            "bet_status": self.bet_status,
            "from_record": self.from_record,
            "row_count": self.row_count,
            "more_available": self.more_available,
            "response_sha256": self.response_sha256,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ClearedMarketRollupWitness:
    bet_count: int
    profit: Decimal
    commission: Decimal
    settled_date: str
    response_sha256: str
    observed_at: str

    def semantic_payload(self) -> dict[str, object]:
        return {
            "bet_count": self.bet_count,
            "profit": _decimal_text(self.profit),
            "commission": _decimal_text(self.commission),
            "settled_date": self.settled_date,
        }

    def payload(self) -> dict[str, object]:
        return {
            **self.semantic_payload(),
            "response_sha256": self.response_sha256,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairClearedMarketPopulation:
    source_family: str
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    market_id: str
    currency: str
    commission_receipt_id: str
    commission_record_sha256: str
    commission_request_scope_sha256: str
    settled_from: str | None
    settled_to: str | None
    statuses: tuple[str, ...]
    page_size: int
    rows: tuple[ClearedMarketBetRow, ...]
    first_pass_pages: tuple[ClearedMarketPageWitness, ...]
    second_pass_pages: tuple[ClearedMarketPageWitness, ...]
    market_rollup_witness: ClearedMarketRollupWitness
    source_interval_start: str
    source_interval_end: str
    request_scope_sha256: str
    population_sha256: str
    evidence_sha256: str
    bounded_revalidation_proven: bool = True
    economic_scope_coextensive_proven: bool = True
    cross_call_atomicity_proven: bool = False
    permanent_finality_proven: bool = False
    grants_execution_authority: bool = False

    @property
    def bet_ids(self) -> tuple[str, ...]:
        return tuple(sorted({row.bet_id for row in self.rows}))

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_cleared_market_population",
            "schema_version": 2,
            "source_family": self.source_family,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "market_id": self.market_id,
            "currency": self.currency,
            "commission_receipt_id": self.commission_receipt_id,
            "commission_record_sha256": self.commission_record_sha256,
            "commission_request_scope_sha256": self.commission_request_scope_sha256,
            "settled_from": self.settled_from,
            "settled_to": self.settled_to,
            "statuses": list(self.statuses),
            "page_size": self.page_size,
            "rows": [row.payload() for row in self.rows],
            "first_pass_pages": [page.payload() for page in self.first_pass_pages],
            "second_pass_pages": [page.payload() for page in self.second_pass_pages],
            "market_rollup_witness": self.market_rollup_witness.payload(),
            "source_interval_start": self.source_interval_start,
            "source_interval_end": self.source_interval_end,
            "request_scope_sha256": self.request_scope_sha256,
            "population_sha256": self.population_sha256,
            "bounded_revalidation_proven": self.bounded_revalidation_proven,
            "economic_scope_coextensive_proven": self.economic_scope_coextensive_proven,
            "cross_call_atomicity_proven": self.cross_call_atomicity_proven,
            "permanent_finality_proven": self.permanent_finality_proven,
            "grants_execution_authority": self.grants_execution_authority,
            "evidence_sha256": self.evidence_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        """Return deterministic integrity evidence; this does not mint source authority."""
        return self.payload()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BetfairClearedMarketPopulation":
        """Reopen durable integrity evidence without recreating provider-origin authority."""
        expected = {
            "schema",
            "schema_version",
            "source_family",
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "market_id",
            "currency",
            "commission_receipt_id",
            "commission_record_sha256",
            "commission_request_scope_sha256",
            "settled_from",
            "settled_to",
            "statuses",
            "page_size",
            "rows",
            "first_pass_pages",
            "second_pass_pages",
            "market_rollup_witness",
            "source_interval_start",
            "source_interval_end",
            "request_scope_sha256",
            "population_sha256",
            "bounded_revalidation_proven",
            "economic_scope_coextensive_proven",
            "cross_call_atomicity_proven",
            "permanent_finality_proven",
            "grants_execution_authority",
            "evidence_sha256",
        }
        _exact_keys(raw, expected, "population")
        if raw["schema"] != "autosport.betfair_cleared_market_population":
            raise BetfairClearedMarketPopulationError("unsupported population schema")
        if raw["schema_version"] != 2:
            raise BetfairClearedMarketPopulationError("unsupported population schema version")
        statuses_raw = _json_list(raw["statuses"], "statuses")
        rows_raw = _json_list(raw["rows"], "rows")
        first_pages_raw = _json_list(raw["first_pass_pages"], "first_pass_pages")
        second_pages_raw = _json_list(raw["second_pass_pages"], "second_pass_pages")
        value = cls(
            source_family=_required_text(raw["source_family"], "source_family"),
            venue_id=_required_text(raw["venue_id"], "venue_id"),
            account_id=_required_text(raw["account_id"], "account_id"),
            adapter_id=_required_text(raw["adapter_id"], "adapter_id"),
            adapter_version=_required_text(raw["adapter_version"], "adapter_version"),
            market_id=_required_text(raw["market_id"], "market_id"),
            currency=_required_text(raw["currency"], "currency"),
            commission_receipt_id=_required_text(
                raw["commission_receipt_id"], "commission_receipt_id"
            ),
            commission_record_sha256=_required_text(
                raw["commission_record_sha256"], "commission_record_sha256"
            ),
            commission_request_scope_sha256=_required_text(
                raw["commission_request_scope_sha256"],
                "commission_request_scope_sha256",
            ),
            settled_from=_optional_text(raw["settled_from"], "settled_from"),
            settled_to=_optional_text(raw["settled_to"], "settled_to"),
            statuses=tuple(_required_text(item, "status") for item in statuses_raw),
            page_size=_json_int(raw["page_size"], "page_size"),
            rows=tuple(_row_from_payload(item) for item in rows_raw),
            first_pass_pages=tuple(
                _page_from_payload(item) for item in first_pages_raw
            ),
            second_pass_pages=tuple(
                _page_from_payload(item) for item in second_pages_raw
            ),
            market_rollup_witness=_market_rollup_from_payload(
                raw["market_rollup_witness"]
            ),
            source_interval_start=_required_text(
                raw["source_interval_start"], "source_interval_start"
            ),
            source_interval_end=_required_text(
                raw["source_interval_end"], "source_interval_end"
            ),
            request_scope_sha256=_required_text(
                raw["request_scope_sha256"], "request_scope_sha256"
            ),
            population_sha256=_required_text(
                raw["population_sha256"], "population_sha256"
            ),
            evidence_sha256=_required_text(raw["evidence_sha256"], "evidence_sha256"),
            bounded_revalidation_proven=_json_bool(
                raw["bounded_revalidation_proven"], "bounded_revalidation_proven"
            ),
            economic_scope_coextensive_proven=_json_bool(
                raw["economic_scope_coextensive_proven"],
                "economic_scope_coextensive_proven",
            ),
            cross_call_atomicity_proven=_json_bool(
                raw["cross_call_atomicity_proven"], "cross_call_atomicity_proven"
            ),
            permanent_finality_proven=_json_bool(
                raw["permanent_finality_proven"], "permanent_finality_proven"
            ),
            grants_execution_authority=_json_bool(
                raw["grants_execution_authority"], "grants_execution_authority"
            ),
        )
        _validate_population(value)
        return value

    def assert_integrity(self) -> None:
        """Validate durable self-integrity only; source authority is process-local."""
        _validate_population(self)


_ISSUED: dict[int, tuple[object, str]] = {}


class BetfairClearedMarketPopulationAuthority:
    """Acquire a complete whole-market BET population for one commission receipt.

    This composes the existing authenticated MARKET-commission source with the
    existing strict read-only Betfair transport. It never filters by local order,
    strategy or execution identity. Positive authority requires two consecutive
    complete semantic passes over every supported terminal status. That proves a
    bounded stable reread only; it does not claim provider cross-call atomicity or
    permanent settlement finality.
    """

    def __init__(self, source: BetfairMarketCommissionAuthority) -> None:
        if type(source) is not BetfairMarketCommissionAuthority:
            raise TypeError("source must be exact BetfairMarketCommissionAuthority")
        self._source = source

    def capture(
        self,
        *,
        commission_receipt_id: str,
        commission_record_sha256: str,
        settled_from: str | None = None,
        settled_to: str | None = None,
        page_size: int = 1000,
        max_pages_per_status: int = 100,
    ) -> BetfairClearedMarketPopulation:
        _sha256_hex(commission_receipt_id, "commission_receipt_id")
        _sha256_hex(commission_record_sha256, "commission_record_sha256")
        if type(page_size) is not int or page_size <= 0 or page_size > 1000:
            raise BetfairClearedMarketPopulationError(
                "page_size must be an integer in [1, 1000]"
            )
        if type(max_pages_per_status) is not int or max_pages_per_status <= 0:
            raise BetfairClearedMarketPopulationError(
                "max_pages_per_status must be a positive integer"
            )
        date_range = _canonical_date_range(settled_from, settled_to)

        start_as_of = _now_utc()
        try:
            receipt, client = _commission_origin.resolve_bound_receipt(
                self._source,
                receipt_id=commission_receipt_id,
                record_sha256=commission_record_sha256,
                as_of=start_as_of,
            )
        except Exception as exc:
            raise BetfairClearedMarketPopulationError(
                "commission receipt lacks current authenticated source authority"
            ) from exc
        if type(receipt) is not BetfairMarketCommissionReceipt:
            raise BetfairClearedMarketPopulationError(
                "commission source returned non-canonical receipt"
            )
        if type(client) is not BetfairReadOnlyClient:
            raise BetfairClearedMarketPopulationError(
                "commission source returned non-canonical Betfair client"
            )
        _assert_commission_scope_matches(receipt, date_range)

        request_scope = _population_request_scope(
            receipt=receipt,
            date_range=date_range,
            page_size=page_size,
        )
        request_scope_sha256 = _digest(request_scope)
        first_rows, first_pages = _capture_pass(
            client=client,
            market_id=receipt.market_id,
            date_range=date_range,
            page_size=page_size,
            max_pages_per_status=max_pages_per_status,
            pass_index=1,
        )
        second_rows, second_pages = _capture_pass(
            client=client,
            market_id=receipt.market_id,
            date_range=date_range,
            page_size=page_size,
            max_pages_per_status=max_pages_per_status,
            pass_index=2,
        )
        if first_rows != second_rows:
            raise BetfairClearedMarketPopulationError(
                "whole-market cleared BET population changed during bounded revalidation"
            )

        market_rollup_witness = _read_market_rollup(
            client=client,
            market_id=receipt.market_id,
            date_range=date_range,
        )
        if (
            market_rollup_witness.profit != receipt.profit
            or market_rollup_witness.commission != receipt.commission
            or _parse_instant(market_rollup_witness.settled_date)
            != receipt.settled_at
        ):
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET rollup economic revision changed from target commission receipt"
            )
        settled_rows = tuple(
            row for row in first_rows if row.bet_status == "SETTLED"
        )
        if market_rollup_witness.bet_count != len(settled_rows):
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET betCount proves BET settlement-range population is not coextensive"
            )
        if not _exact_decimal_sum_equals(
            (row.profit for row in settled_rows),
            market_rollup_witness.profit,
        ):
            raise BetfairClearedMarketPopulationError(
                "complete SETTLED BET gross profit does not conserve fresh MARKET profit"
            )

        # Re-resolve the exact commission at the end. If the source learned a
        # superseding commission receipt during acquisition, this old pairing is
        # no longer current and must fail closed rather than mixing revisions.
        try:
            end_receipt, end_client = _commission_origin.resolve_bound_receipt(
                self._source,
                receipt_id=commission_receipt_id,
                record_sha256=commission_record_sha256,
                as_of=_now_utc(),
            )
        except Exception as exc:
            raise BetfairClearedMarketPopulationError(
                "commission receipt changed during population acquisition"
            ) from exc
        if end_receipt != receipt or end_client is not client:
            raise BetfairClearedMarketPopulationError(
                "commission source identity changed during population acquisition"
            )

        all_pages = (*first_pages, *second_pages)
        if not all_pages:
            raise BetfairClearedMarketPopulationError(
                "population acquisition produced no page evidence"
            )
        page_observed = tuple(
            _parse_instant(page.observed_at) for page in all_pages
        )
        market_observed = _parse_instant(market_rollup_witness.observed_at)
        if market_observed < max(page_observed):
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET witness cannot predate completed BET revalidation"
            )
        observed = (*page_observed, market_observed)
        source_interval_start = _instant_text(min(observed))
        source_interval_end = _instant_text(max(observed))
        population_sha256 = _digest(
            {
                "schema": "autosport.betfair_cleared_market_population.semantic",
                "schema_version": 2,
                "request_scope_sha256": request_scope_sha256,
                "rows": [row.payload() for row in first_rows],
                "market_rollup": market_rollup_witness.semantic_payload(),
            }
        )
        evidence_sha256 = _digest(
            {
                "schema": "autosport.betfair_cleared_market_population.evidence",
                "schema_version": 2,
                "request_scope_sha256": request_scope_sha256,
                "population_sha256": population_sha256,
                "first_pass_pages": [page.payload() for page in first_pages],
                "second_pass_pages": [page.payload() for page in second_pages],
                "market_rollup_witness": market_rollup_witness.payload(),
                "source_interval_start": source_interval_start,
                "source_interval_end": source_interval_end,
            }
        )
        value = BetfairClearedMarketPopulation(
            source_family=SOURCE_FAMILY,
            venue_id=receipt.venue_id,
            account_id=receipt.account_id,
            adapter_id=receipt.adapter_id,
            adapter_version=receipt.adapter_version,
            market_id=receipt.market_id,
            currency=receipt.currency,
            commission_receipt_id=receipt.receipt_id,
            commission_record_sha256=receipt.record_sha256,
            commission_request_scope_sha256=receipt.request_scope_sha256,
            settled_from=date_range.get("from"),
            settled_to=date_range.get("to"),
            statuses=_STATUSES,
            page_size=page_size,
            rows=first_rows,
            first_pass_pages=first_pages,
            second_pass_pages=second_pages,
            market_rollup_witness=market_rollup_witness,
            source_interval_start=source_interval_start,
            source_interval_end=source_interval_end,
            request_scope_sha256=request_scope_sha256,
            population_sha256=population_sha256,
            evidence_sha256=evidence_sha256,
        )
        _validate_population(value)
        key = id(value)

        def forget(_weakref: object, *, issued_key: int = key) -> None:
            _ISSUED.pop(issued_key, None)

        _ISSUED[key] = (ref(value, forget), value.evidence_sha256)
        return value


def assert_betfair_cleared_market_population_authoritative(
    value: BetfairClearedMarketPopulation,
) -> None:
    """Require exact current-process issuance plus all fail-closed truth flags."""

    if type(value) is not BetfairClearedMarketPopulation:
        raise BetfairClearedMarketPopulationError(
            "population must be exact BetfairClearedMarketPopulation"
        )
    issued = _ISSUED.get(id(value))
    if issued is None or issued[0]() is not value:
        raise BetfairClearedMarketPopulationError(
            "population was not issued by canonical authenticated acquisition"
        )
    if issued[1] != value.evidence_sha256:
        raise BetfairClearedMarketPopulationError(
            "issued population evidence changed after acquisition"
        )
    _validate_population(value)


def _capture_pass(
    *,
    client: BetfairReadOnlyClient,
    market_id: str,
    date_range: Mapping[str, str],
    page_size: int,
    max_pages_per_status: int,
    pass_index: int,
) -> tuple[tuple[ClearedMarketBetRow, ...], tuple[ClearedMarketPageWitness, ...]]:
    rows: list[ClearedMarketBetRow] = []
    pages: list[ClearedMarketPageWitness] = []
    for status in _STATUSES:
        offset = 0
        seen_status_bets: set[str] = set()
        terminal = False
        for _ in range(max_pages_per_status):
            page = _read_page(
                client=client,
                market_id=market_id,
                bet_status=status,
                date_range=date_range,
                from_record=offset,
                record_count=page_size,
            )
            if type(page) is not BetfairClearedOrderPage:
                raise BetfairClearedMarketPopulationError(
                    "cleared-order page is not canonical"
                )
            if page.from_record != offset or page.record_count != page_size:
                raise BetfairClearedMarketPopulationError(
                    "cleared-order pagination boundary changed unexpectedly"
                )
            pages.append(
                ClearedMarketPageWitness(
                    pass_index=pass_index,
                    bet_status=status,
                    from_record=page.from_record,
                    row_count=len(page.orders),
                    more_available=page.more_available,
                    response_sha256=page.evidence.source_payload_sha256,
                    observed_at=page.evidence.observed_at,
                )
            )
            for observation in page.orders:
                if observation.market_id != market_id:
                    raise BetfairClearedMarketPopulationError(
                        "provider returned a cleared BET for a different market"
                    )
                if observation.bet_status != status:
                    raise BetfairClearedMarketPopulationError(
                        "provider cleared BET status does not match request status"
                    )
                if observation.bet_id in seen_status_bets:
                    raise BetfairClearedMarketPopulationError(
                        "cleared BET repeated within one status pagination pass"
                    )
                seen_status_bets.add(observation.bet_id)
                rows.append(ClearedMarketBetRow.from_observation(observation))
            if not page.more_available:
                terminal = True
                break
            if not page.orders:
                raise BetfairClearedMarketPopulationError(
                    "cleared BET pagination reported moreAvailable with empty page"
                )
            offset += len(page.orders)
        if not terminal:
            raise BetfairClearedMarketPopulationError(
                "cleared BET pagination exceeded max_pages_per_status before exhaustion"
            )
    result = tuple(sorted(rows, key=_row_sort_key))
    _validate_cross_status_rows(result)
    return result, tuple(pages)


def _read_page(
    *,
    client: BetfairReadOnlyClient,
    market_id: str,
    bet_status: str,
    date_range: Mapping[str, str],
    from_record: int,
    record_count: int,
) -> BetfairClearedOrderPage:
    """Fixed whole-market BET read over the existing strict read-only transport."""

    if type(client) is not BetfairReadOnlyClient:
        raise BetfairClearedMarketPopulationError(
            "population acquisition requires canonical BetfairReadOnlyClient"
        )
    params: dict[str, object] = {
        "betStatus": bet_status,
        "groupBy": "BET",
        "marketIds": [market_id],
        "fromRecord": from_record,
        "recordCount": record_count,
    }
    if date_range:
        params["settledDateRange"] = dict(date_range)
    try:
        response = client._rpc(_betfair._LIST_CLEARED_ORDERS, params)
        report = _betfair._mapping(response.result, "listClearedOrders result")
        raw_orders = _betfair._sequence(
            report.get("clearedOrders"), "clearedOrders"
        )
        orders = tuple(
            _betfair._parse_cleared_order(raw, response.evidence, index, bet_status)
            for index, raw in enumerate(raw_orders)
        )
        _betfair._unique_bet_ids(orders, "clearedOrders")
        return BetfairClearedOrderPage(
            orders=orders,
            more_available=_betfair._provider_bool(report, "moreAvailable"),
            from_record=from_record,
            record_count=record_count,
            evidence=response.evidence,
        )
    except BetfairReadOnlyError as exc:
        raise BetfairClearedMarketPopulationError(
            "whole-market cleared BET page acquisition failed"
        ) from exc


def _read_market_rollup(
    *,
    client: BetfairReadOnlyClient,
    market_id: str,
    date_range: Mapping[str, str],
) -> ClearedMarketRollupWitness:
    """Fresh MARKET reread used only to prove BET-population scope/economics."""

    if type(client) is not BetfairReadOnlyClient:
        raise BetfairClearedMarketPopulationError(
            "MARKET revalidation requires canonical BetfairReadOnlyClient"
        )
    params: dict[str, object] = {
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": [market_id],
        "fromRecord": 0,
        "recordCount": 1000,
    }
    if date_range:
        params["settledDateRange"] = dict(date_range)
    try:
        response = client._rpc(_betfair._LIST_CLEARED_ORDERS, params)
        report = _betfair._mapping(response.result, "listClearedOrders MARKET result")
        if _betfair._provider_bool(report, "moreAvailable"):
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET rollup revalidation is incomplete"
            )
        rows = _betfair._sequence(
            report.get("clearedOrders"), "MARKET clearedOrders"
        )
        if len(rows) != 1:
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET rollup must contain exactly one market"
            )
        row = _betfair._mapping(rows[0], "MARKET clearedOrders[0]")
        returned_market = _required_text(row.get("marketId"), "MARKET marketId")
        if returned_market != market_id:
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET rollup returned a different market"
            )
        bet_count = _provider_nonnegative_int(row.get("betCount"), "MARKET betCount")
        profit = _provider_decimal(row.get("profit"), "MARKET profit")
        commission = _provider_decimal(row.get("commission"), "MARKET commission")
        if commission < 0:
            raise BetfairClearedMarketPopulationError(
                "fresh MARKET commission cannot be negative"
            )
        settled_date = _provider_time_text(
            _required_text(row.get("settledDate"), "MARKET settledDate"),
            "MARKET settledDate",
        )
        return ClearedMarketRollupWitness(
            bet_count=bet_count,
            profit=profit,
            commission=commission,
            settled_date=settled_date,
            response_sha256=response.evidence.source_payload_sha256,
            observed_at=response.evidence.observed_at,
        )
    except BetfairReadOnlyError as exc:
        raise BetfairClearedMarketPopulationError(
            "fresh MARKET rollup acquisition failed"
        ) from exc


def _assert_commission_scope_matches(
    receipt: BetfairMarketCommissionReceipt,
    date_range: Mapping[str, str],
) -> None:
    scope = {
        "method": "SportsAPING/v1.0/listClearedOrders",
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": [receipt.market_id],
        "settledDateRange": dict(date_range) or None,
        "fromRecord": 0,
        "recordCount": 1000,
        "venue_id": receipt.venue_id,
        "account_id": receipt.account_id,
        "adapter_id": receipt.adapter_id,
        "adapter_version": receipt.adapter_version,
    }
    if _digest(scope) != receipt.request_scope_sha256:
        raise BetfairClearedMarketPopulationError(
            "settlement range does not match the target MARKET commission receipt"
        )


def _population_request_scope(
    *,
    receipt: BetfairMarketCommissionReceipt,
    date_range: Mapping[str, str],
    page_size: int,
) -> dict[str, object]:
    return {
        "schema": "autosport.betfair_cleared_market_population.request",
        "schema_version": 2,
        "source_family": SOURCE_FAMILY,
        "venue_id": receipt.venue_id,
        "account_id": receipt.account_id,
        "adapter_id": receipt.adapter_id,
        "adapter_version": receipt.adapter_version,
        "market_id": receipt.market_id,
        "currency": receipt.currency,
        "commission_receipt_id": receipt.receipt_id,
        "commission_request_scope_sha256": receipt.request_scope_sha256,
        "method": "SportsAPING/v1.0/listClearedOrders",
        "groupBy": "BET",
        "statuses": list(_STATUSES),
        "settledDateRange": dict(date_range) or None,
        "marketIds": [receipt.market_id],
        "customerOrderRefs": None,
        "customerStrategyRefs": None,
        "betIds": None,
        "page_size": page_size,
    }


def _validate_cross_status_rows(rows: tuple[ClearedMarketBetRow, ...]) -> None:
    by_bet: dict[str, list[ClearedMarketBetRow]] = {}
    for row in rows:
        by_bet.setdefault(row.bet_id, []).append(row)
    for bet_id, group in by_bet.items():
        statuses = [row.bet_status for row in group]
        if len(statuses) != len(set(statuses)):
            raise BetfairClearedMarketPopulationError(
                f"cleared BET {bet_id} has conflicting duplicate status rows"
            )
        economic = [
            row
            for row in group
            if row.size_settled != Decimal("0") or row.profit != Decimal("0")
        ]
        if len(economic) > 1:
            raise BetfairClearedMarketPopulationError(
                f"cleared BET {bet_id} has multiple incompatible economic terminal rows"
            )


def _validate_population(value: BetfairClearedMarketPopulation) -> None:
    if value.commission_record_sha256 != value.commission_receipt_id:
        raise BetfairClearedMarketPopulationError("commission record identity mismatch")
    if value.source_family != SOURCE_FAMILY:
        raise BetfairClearedMarketPopulationError("population source family mismatch")
    if value.venue_id != _FIXED_VENUE_ID:
        raise BetfairClearedMarketPopulationError("population venue mismatch")
    if value.adapter_id != ADAPTER_ID or value.adapter_version != ADAPTER_VERSION:
        raise BetfairClearedMarketPopulationError("population adapter identity mismatch")
    if value.statuses != _STATUSES:
        raise BetfairClearedMarketPopulationError("population status coverage incomplete")
    if (
        value.bounded_revalidation_proven is not True
        or value.economic_scope_coextensive_proven is not True
        or value.cross_call_atomicity_proven is not False
        or value.permanent_finality_proven is not False
        or value.grants_execution_authority is not False
    ):
        raise BetfairClearedMarketPopulationError(
            "population truth flags widen authority beyond observed provider evidence"
        )
    if type(value.page_size) is not int or value.page_size <= 0 or value.page_size > 1000:
        raise BetfairClearedMarketPopulationError("population page_size is invalid")
    for digest_value, label in (
        (value.commission_receipt_id, "commission_receipt_id"),
        (value.commission_record_sha256, "commission_record_sha256"),
        (value.commission_request_scope_sha256, "commission_request_scope_sha256"),
        (value.request_scope_sha256, "request_scope_sha256"),
        (value.population_sha256, "population_sha256"),
        (value.evidence_sha256, "evidence_sha256"),
    ):
        _sha256_hex(digest_value, label)
    canonical_range = _canonical_date_range(value.settled_from, value.settled_to)
    if value.settled_from != canonical_range.get("from") or value.settled_to != canonical_range.get("to"):
        raise BetfairClearedMarketPopulationError(
            "population settlement range is not canonical"
        )
    if tuple(sorted(value.rows, key=_row_sort_key)) != value.rows:
        raise BetfairClearedMarketPopulationError("population rows are not canonically sorted")
    for row in value.rows:
        _validate_row_against_scope(row, value.market_id)
    _validate_cross_status_rows(value.rows)
    _validate_market_rollup_witness(value.market_rollup_witness)
    settled_rows = tuple(row for row in value.rows if row.bet_status == "SETTLED")
    if value.market_rollup_witness.bet_count != len(settled_rows):
        raise BetfairClearedMarketPopulationError(
            "population MARKET betCount is not coextensive with SETTLED BET rows"
        )
    if not _exact_decimal_sum_equals(
        (row.profit for row in settled_rows),
        value.market_rollup_witness.profit,
    ):
        raise BetfairClearedMarketPopulationError(
            "population SETTLED BET gross profit does not conserve MARKET profit"
        )
    _validate_page_witnesses(
        value.first_pass_pages,
        rows=value.rows,
        pass_index=1,
        page_size=value.page_size,
    )
    _validate_page_witnesses(
        value.second_pass_pages,
        rows=value.rows,
        pass_index=2,
        page_size=value.page_size,
    )
    all_pages = (*value.first_pass_pages, *value.second_pass_pages)
    page_observed = tuple(
        _parse_instant(page.observed_at) for page in all_pages
    )
    market_observed = _parse_instant(value.market_rollup_witness.observed_at)
    if not page_observed or market_observed < max(page_observed):
        raise BetfairClearedMarketPopulationError(
            "MARKET witness does not causally follow BET revalidation"
        )
    observed = (*page_observed, market_observed)
    if (
        not observed
        or value.source_interval_start != _instant_text(min(observed))
        or value.source_interval_end != _instant_text(max(observed))
    ):
        raise BetfairClearedMarketPopulationError(
            "population source interval does not match page witnesses"
        )
    request_scope = {
        "schema": "autosport.betfair_cleared_market_population.request",
        "schema_version": 2,
        "source_family": SOURCE_FAMILY,
        "venue_id": value.venue_id,
        "account_id": value.account_id,
        "adapter_id": value.adapter_id,
        "adapter_version": value.adapter_version,
        "market_id": value.market_id,
        "currency": value.currency,
        "commission_receipt_id": value.commission_receipt_id,
        "commission_request_scope_sha256": value.commission_request_scope_sha256,
        "method": "SportsAPING/v1.0/listClearedOrders",
        "groupBy": "BET",
        "statuses": list(_STATUSES),
        "settledDateRange": (
            {k: v for k, v in (("from", value.settled_from), ("to", value.settled_to)) if v is not None}
            or None
        ),
        "marketIds": [value.market_id],
        "customerOrderRefs": None,
        "customerStrategyRefs": None,
        "betIds": None,
        "page_size": value.page_size,
    }
    if _digest(request_scope) != value.request_scope_sha256:
        raise BetfairClearedMarketPopulationError("population request-scope digest mismatch")
    expected_population = _digest(
        {
            "schema": "autosport.betfair_cleared_market_population.semantic",
            "schema_version": 2,
            "request_scope_sha256": value.request_scope_sha256,
            "rows": [row.payload() for row in value.rows],
            "market_rollup": value.market_rollup_witness.semantic_payload(),
        }
    )
    if expected_population != value.population_sha256:
        raise BetfairClearedMarketPopulationError("population semantic digest mismatch")
    expected_evidence = _digest(
        {
            "schema": "autosport.betfair_cleared_market_population.evidence",
            "schema_version": 2,
            "request_scope_sha256": value.request_scope_sha256,
            "population_sha256": value.population_sha256,
            "first_pass_pages": [page.payload() for page in value.first_pass_pages],
            "second_pass_pages": [page.payload() for page in value.second_pass_pages],
            "market_rollup_witness": value.market_rollup_witness.payload(),
            "source_interval_start": value.source_interval_start,
            "source_interval_end": value.source_interval_end,
        }
    )
    if expected_evidence != value.evidence_sha256:
        raise BetfairClearedMarketPopulationError("population evidence digest mismatch")


def _validate_row_against_scope(
    row: ClearedMarketBetRow, market_id: str
) -> None:
    if type(row) is not ClearedMarketBetRow:
        raise BetfairClearedMarketPopulationError(
            "population row must be exact ClearedMarketBetRow"
        )
    _required_text(row.bet_id, "bet_id")
    if row.market_id != market_id:
        raise BetfairClearedMarketPopulationError(
            "population row market does not match request scope"
        )
    if (
        not isinstance(row.selection_id, int)
        or isinstance(row.selection_id, bool)
        or row.selection_id <= 0
    ):
        raise BetfairClearedMarketPopulationError(
            "selection_id must be a positive integer"
        )
    if row.side not in {"BACK", "LAY"}:
        raise BetfairClearedMarketPopulationError("population row side is invalid")
    if row.bet_status not in _STATUSES:
        raise BetfairClearedMarketPopulationError(
            "population row terminal status is invalid"
        )
    _parse_instant(row.placed_date)
    _parse_instant(row.settled_date)
    for value, label in (
        (row.price_requested, "price_requested"),
        (row.price_matched, "price_matched"),
        (row.size_settled, "size_settled"),
        (row.profit, "profit"),
    ):
        _decimal_text(value)
        if label == "price_requested" and value <= 0:
            raise BetfairClearedMarketPopulationError(
                "price_requested must be positive"
            )
        if label in {"price_matched", "size_settled"} and value < 0:
            raise BetfairClearedMarketPopulationError(
                f"{label} must be non-negative"
            )
    _optional_text(row.customer_order_ref, "customer_order_ref")
    _optional_text(row.customer_strategy_ref, "customer_strategy_ref")
    _optional_text(row.event_id, "event_id")


def _validate_page_witnesses(
    pages: tuple[ClearedMarketPageWitness, ...],
    *,
    rows: tuple[ClearedMarketBetRow, ...],
    pass_index: int,
    page_size: int,
) -> None:
    if not isinstance(pages, tuple):
        raise BetfairClearedMarketPopulationError(
            "population page witnesses must be a tuple"
        )
    cursor = 0
    for status in _STATUSES:
        offset = 0
        total = 0
        saw_terminal = False
        saw_page = False
        while cursor < len(pages) and pages[cursor].bet_status == status:
            page = pages[cursor]
            saw_page = True
            if type(page) is not ClearedMarketPageWitness:
                raise BetfairClearedMarketPopulationError(
                    "population page witness must be canonical"
                )
            if page.pass_index != pass_index:
                raise BetfairClearedMarketPopulationError(
                    "population page witness pass index mismatch"
                )
            if page.from_record != offset:
                raise BetfairClearedMarketPopulationError(
                    "population page witness offset is not exhaustive"
                )
            if (
                not isinstance(page.row_count, int)
                or isinstance(page.row_count, bool)
                or page.row_count < 0
                or page.row_count > page_size
            ):
                raise BetfairClearedMarketPopulationError(
                    "population page witness row_count is invalid"
                )
            _sha256_hex(page.response_sha256, "response_sha256")
            _parse_instant(page.observed_at)
            total += page.row_count
            cursor += 1
            if page.more_available:
                if page.row_count == 0:
                    raise BetfairClearedMarketPopulationError(
                        "population witness has empty moreAvailable page"
                    )
                offset += page.row_count
                continue
            saw_terminal = True
            break
        if not saw_page or not saw_terminal:
            raise BetfairClearedMarketPopulationError(
                f"population witness did not exhaust {status}"
            )
        expected = sum(1 for row in rows if row.bet_status == status)
        if total != expected:
            raise BetfairClearedMarketPopulationError(
                f"population witness row count mismatch for {status}"
            )
    if cursor != len(pages):
        raise BetfairClearedMarketPopulationError(
            "population witness contains unexpected status/page ordering"
        )


def _validate_market_rollup_witness(
    value: ClearedMarketRollupWitness,
) -> None:
    if type(value) is not ClearedMarketRollupWitness:
        raise BetfairClearedMarketPopulationError(
            "MARKET rollup witness must be canonical"
        )
    if type(value.bet_count) is not int or value.bet_count < 0:
        raise BetfairClearedMarketPopulationError(
            "MARKET bet_count must be a non-negative integer"
        )
    _decimal_text(value.profit)
    _decimal_text(value.commission)
    if value.commission < 0:
        raise BetfairClearedMarketPopulationError(
            "MARKET commission cannot be negative"
        )
    _provider_time_text(value.settled_date, "MARKET settled_date")
    _sha256_hex(value.response_sha256, "MARKET response_sha256")
    _parse_instant(value.observed_at)


def _row_from_payload(value: object) -> ClearedMarketBetRow:
    raw = _json_mapping(value, "row")
    _exact_keys(
        raw,
        {
            "bet_id",
            "market_id",
            "selection_id",
            "side",
            "bet_status",
            "placed_date",
            "settled_date",
            "price_requested",
            "price_matched",
            "size_settled",
            "profit",
            "customer_order_ref",
            "customer_strategy_ref",
            "event_id",
        },
        "row",
    )
    return ClearedMarketBetRow(
        bet_id=_required_text(raw["bet_id"], "bet_id"),
        market_id=_required_text(raw["market_id"], "market_id"),
        selection_id=_json_int(raw["selection_id"], "selection_id"),
        side=_required_text(raw["side"], "side"),
        bet_status=_required_text(raw["bet_status"], "bet_status"),
        placed_date=_required_text(raw["placed_date"], "placed_date"),
        settled_date=_required_text(raw["settled_date"], "settled_date"),
        price_requested=_decimal_from_text(raw["price_requested"], "price_requested"),
        price_matched=_decimal_from_text(raw["price_matched"], "price_matched"),
        size_settled=_decimal_from_text(raw["size_settled"], "size_settled"),
        profit=_decimal_from_text(raw["profit"], "profit"),
        customer_order_ref=_optional_text(
            raw["customer_order_ref"], "customer_order_ref"
        ),
        customer_strategy_ref=_optional_text(
            raw["customer_strategy_ref"], "customer_strategy_ref"
        ),
        event_id=_optional_text(raw["event_id"], "event_id"),
    )


def _page_from_payload(value: object) -> ClearedMarketPageWitness:
    raw = _json_mapping(value, "page witness")
    _exact_keys(
        raw,
        {
            "pass_index",
            "bet_status",
            "from_record",
            "row_count",
            "more_available",
            "response_sha256",
            "observed_at",
        },
        "page witness",
    )
    return ClearedMarketPageWitness(
        pass_index=_json_int(raw["pass_index"], "pass_index"),
        bet_status=_required_text(raw["bet_status"], "bet_status"),
        from_record=_json_int(raw["from_record"], "from_record"),
        row_count=_json_int(raw["row_count"], "row_count"),
        more_available=_json_bool(raw["more_available"], "more_available"),
        response_sha256=_required_text(raw["response_sha256"], "response_sha256"),
        observed_at=_required_text(raw["observed_at"], "observed_at"),
    )


def _market_rollup_from_payload(value: object) -> ClearedMarketRollupWitness:
    raw = _json_mapping(value, "MARKET rollup witness")
    _exact_keys(
        raw,
        {
            "bet_count",
            "profit",
            "commission",
            "settled_date",
            "response_sha256",
            "observed_at",
        },
        "MARKET rollup witness",
    )
    return ClearedMarketRollupWitness(
        bet_count=_json_int(raw["bet_count"], "MARKET bet_count"),
        profit=_decimal_from_text(raw["profit"], "MARKET profit"),
        commission=_decimal_from_text(raw["commission"], "MARKET commission"),
        settled_date=_required_text(raw["settled_date"], "MARKET settled_date"),
        response_sha256=_required_text(
            raw["response_sha256"], "MARKET response_sha256"
        ),
        observed_at=_required_text(raw["observed_at"], "MARKET observed_at"),
    )


def _json_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BetfairClearedMarketPopulationError(f"{label} must be a JSON object")
    return value


def _json_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise BetfairClearedMarketPopulationError(f"{label} must be a JSON array")
    return value


def _exact_keys(
    raw: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(raw) != expected:
        raise BetfairClearedMarketPopulationError(f"{label} has unexpected fields")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairClearedMarketPopulationError(
            f"{label} must be non-empty canonical text"
        )
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _required_text(value, label)


def _json_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BetfairClearedMarketPopulationError(
            f"{label} must be a non-negative integer"
        )
    return value


def _json_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise BetfairClearedMarketPopulationError(f"{label} must be bool")
    return value


def _decimal_from_text(value: object, label: str) -> Decimal:
    text = _required_text(value, label)
    try:
        parsed = Decimal(text)
    except Exception as exc:
        raise BetfairClearedMarketPopulationError(f"{label} is invalid") from exc
    if not parsed.is_finite() or _decimal_text(parsed) != text:
        raise BetfairClearedMarketPopulationError(
            f"{label} must be canonical finite Decimal text"
        )
    return parsed


def _provider_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise BetfairClearedMarketPopulationError(
            f"{label} must be a non-negative provider integer"
        )
    return value


def _provider_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise BetfairClearedMarketPopulationError(
            f"{label} must be provider numeric data"
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise BetfairClearedMarketPopulationError(f"{label} is invalid") from exc
    if not parsed.is_finite():
        raise BetfairClearedMarketPopulationError(f"{label} must be finite")
    return parsed


def _exact_decimal_sum_equals(values, expected: Decimal) -> bool:
    target = Fraction(*expected.as_integer_ratio())
    total = Fraction(0, 1)
    for value in values:
        if type(value) is not Decimal or not value.is_finite():
            raise BetfairClearedMarketPopulationError(
                "gross-profit conservation requires finite Decimal values"
            )
        total += Fraction(*value.as_integer_ratio())
    return total == target


def _canonical_date_range(
    settled_from: str | None,
    settled_to: str | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if settled_from is not None:
        result["from"] = _provider_time_text(settled_from, "settled_from")
    if settled_to is not None:
        result["to"] = _provider_time_text(settled_to, "settled_to")
    if "from" in result and "to" in result:
        if _parse_instant(result["from"]) > _parse_instant(result["to"]):
            raise BetfairClearedMarketPopulationError(
                "settled_from cannot be after settled_to"
            )
    return result


def _provider_time_text(value: str, label: str) -> str:
    try:
        parsed = _parse_instant(value)
    except BetfairClearedMarketPopulationError as exc:
        raise BetfairClearedMarketPopulationError(f"{label} is invalid") from exc
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairClearedMarketPopulationError("timestamp must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairClearedMarketPopulationError("timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairClearedMarketPopulationError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _instant_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetfairClearedMarketPopulationError("economic value must be finite Decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _row_sort_key(row: ClearedMarketBetRow) -> str:
    return json.dumps(row.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetfairClearedMarketPopulationError(
            f"{label} must be lowercase SHA-256 hex"
        )


def _digest(value: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairClearedMarketPopulationError(
            "population evidence is not canonical JSON"
        ) from exc
    return sha256(raw).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
