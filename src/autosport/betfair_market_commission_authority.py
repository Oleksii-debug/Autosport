from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from . import betfair_account_readonly as _betfair
from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority


SCHEMA_VERSION = 1
SOURCE_FAMILY = "betfair.market-commission.v1"
_FIXED_VENUE_ID = "betfair"
_FIXED_CLIENT_ACCOUNT_SCOPE = "authenticated-account"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class BetfairMarketCommissionAuthorityError(RuntimeError):
    """Raised when authenticated Betfair commission evidence is not exact."""


@dataclass(frozen=True, slots=True)
class BetfairMarketCommissionReceipt:
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    market_id: str
    commission: Decimal
    profit: Decimal
    currency: str
    settled_at: datetime
    observed_at: datetime
    available_at: datetime
    account_details_sha256: str
    cleared_orders_sha256: str
    request_scope_sha256: str
    supersedes_receipt_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("venue_id", self.venue_id),
            ("account_id", self.account_id),
            ("adapter_id", self.adapter_id),
            ("market_id", self.market_id),
        ):
            _text(value, label)
        if self.venue_id != _FIXED_VENUE_ID:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair venue identity is production-owned"
            )
        if not self.account_id.startswith("betfair-account-evidence:"):
            raise BetfairMarketCommissionAuthorityError(
                "Betfair account identity must come from authenticated account evidence"
            )
        _sha256(
            self.account_id.removeprefix("betfair-account-evidence:"),
            "account evidence identity",
        )
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairMarketCommissionAuthorityError("Betfair adapter identity mismatch")
        if (
            not isinstance(self.commission, Decimal)
            or not self.commission.is_finite()
            or self.commission < 0
        ):
            raise BetfairMarketCommissionAuthorityError(
                "commission must be a finite non-negative Decimal"
            )
        if not isinstance(self.profit, Decimal) or not self.profit.is_finite():
            raise BetfairMarketCommissionAuthorityError(
                "profit must be a finite Decimal"
            )
        if _CURRENCY_RE.fullmatch(self.currency) is None:
            raise BetfairMarketCommissionAuthorityError(
                "currency must be uppercase three-letter code"
            )
        for label, value in (
            ("settled_at", self.settled_at),
            ("observed_at", self.observed_at),
            ("available_at", self.available_at),
        ):
            _utc(value, label)
        if self.settled_at > self.available_at or self.observed_at > self.available_at:
            raise BetfairMarketCommissionAuthorityError(
                "commission cannot be available before provider evidence"
            )
        for label, value in (
            ("account_details_sha256", self.account_details_sha256),
            ("cleared_orders_sha256", self.cleared_orders_sha256),
            ("request_scope_sha256", self.request_scope_sha256),
        ):
            _sha256(value, label)
        if self.account_id != _provider_account_identity(
            self.account_details_sha256
        ):
            raise BetfairMarketCommissionAuthorityError(
                "Betfair account identity is not bound to authenticated account evidence"
            )
        if self.supersedes_receipt_id is not None:
            _sha256(self.supersedes_receipt_id, "supersedes_receipt_id")

    @property
    def receipt_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.receipt_id

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_family": SOURCE_FAMILY,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "market_id": self.market_id,
            "commission": _decimal_text(self.commission),
            "profit": _decimal_text(self.profit),
            "currency": self.currency,
            "settled_at": _datetime_text(self.settled_at),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "account_details_sha256": self.account_details_sha256,
            "cleared_orders_sha256": self.cleared_orders_sha256,
            "request_scope_sha256": self.request_scope_sha256,
            "supersedes_receipt_id": self.supersedes_receipt_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["receipt_id"] = self.receipt_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "BetfairMarketCommissionReceipt":
        _keys(
            raw,
            {
                "schema_version",
                "source_family",
                "venue_id",
                "account_id",
                "adapter_id",
                "adapter_version",
                "market_id",
                "commission",
                "profit",
                "currency",
                "settled_at",
                "observed_at",
                "available_at",
                "account_details_sha256",
                "cleared_orders_sha256",
                "request_scope_sha256",
                "supersedes_receipt_id",
                "receipt_id",
                "record_sha256",
            },
            "Betfair commission receipt",
        )
        if (
            raw["schema_version"] != SCHEMA_VERSION
            or raw["source_family"] != SOURCE_FAMILY
        ):
            raise BetfairMarketCommissionAuthorityError(
                "unsupported Betfair commission receipt"
            )
        item = cls(
            venue_id=_string(raw["venue_id"], "venue_id"),
            account_id=_string(raw["account_id"], "account_id"),
            adapter_id=_string(raw["adapter_id"], "adapter_id"),
            adapter_version=_string(raw["adapter_version"], "adapter_version"),
            market_id=_string(raw["market_id"], "market_id"),
            commission=_parse_decimal(raw["commission"], "commission"),
            profit=_parse_decimal(raw["profit"], "profit"),
            currency=_string(raw["currency"], "currency"),
            settled_at=_parse_datetime(raw["settled_at"], "settled_at"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            account_details_sha256=_string(
                raw["account_details_sha256"], "account_details_sha256"
            ),
            cleared_orders_sha256=_string(
                raw["cleared_orders_sha256"], "cleared_orders_sha256"
            ),
            request_scope_sha256=_string(
                raw["request_scope_sha256"], "request_scope_sha256"
            ),
            supersedes_receipt_id=_optional_string(
                raw["supersedes_receipt_id"], "supersedes_receipt_id"
            ),
        )
        if (
            raw["receipt_id"] != item.receipt_id
            or raw["record_sha256"] != item.record_sha256
        ):
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission receipt digest mismatch"
            )
        return item


class BetfairMarketCommissionAuthority:
    """Production-owned read-only Betfair commission source.

    Positive source authority is process-local and is issued only after this object
    performs the fixed authenticated Betfair reads. Durable JSON plus the generic
    monotonic journal are integrity/rollback evidence; neither can recreate provider
    origin after restart. A restarted authority must reacquire the provider evidence
    before ``resolve`` may return a monetary source receipt.

    Caller-selected venue/account labels are not accepted. The persisted account
    scope is derived from the authenticated ``getAccountDetails`` payload digest,
    preventing the same provider bytes from being arbitrarily relabelled.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        credentials: BetfairSessionCredentials,
        *,
        authority_root: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 10.0,
        venue_id: str = _FIXED_VENUE_ID,
        account_id: str | None = None,
    ) -> None:
        if type(credentials) is not BetfairSessionCredentials:
            raise TypeError("credentials must be BetfairSessionCredentials")
        if venue_id != _FIXED_VENUE_ID or account_id is not None:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair venue/account identity is production-owned"
            )
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._venue_id = _FIXED_VENUE_ID
        self._client = BetfairReadOnlyClient(
            credentials,
            timeout_seconds=timeout_seconds,
            venue_id=self._venue_id,
            account_id=_FIXED_CLIENT_ACCOUNT_SCOPE,
        )
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.root,
            domain="autosport.betfair_market_commission.v1",
            key=f"{self._venue_id}:{_FIXED_CLIENT_ACCOUNT_SCOPE}",
            authority_root=authority_root,
        )
        self._issued_receipt_ids: set[str] = set()

    def capture_market(
        self,
        market_id: str,
        *,
        settled_from: str | None = None,
        settled_to: str | None = None,
    ) -> BetfairMarketCommissionReceipt:
        market = _canonical_text(market_id, "market_id")
        details = self._client.read_account_details()
        currency = _canonical_currency(details.currency_code)
        account_identity = _provider_account_identity(
            details.evidence.source_payload_sha256
        )
        params: dict[str, object] = {
            "betStatus": "SETTLED",
            "groupBy": "MARKET",
            "marketIds": [market],
            "fromRecord": 0,
            "recordCount": 1000,
        }
        date_range: dict[str, str] = {}
        if settled_from is not None:
            date_range["from"] = _canonical_provider_time(
                settled_from, "settled_from"
            )
        if settled_to is not None:
            date_range["to"] = _canonical_provider_time(
                settled_to, "settled_to"
            )
        if date_range:
            if "from" in date_range and "to" in date_range:
                if _parse_provider_time(
                    date_range["from"], "settled_from"
                ) > _parse_provider_time(date_range["to"], "settled_to"):
                    raise BetfairMarketCommissionAuthorityError(
                        "settled_from cannot be after settled_to"
                    )
            params["settledDateRange"] = date_range
        request_scope = {
            "method": "SportsAPING/v1.0/listClearedOrders",
            "betStatus": "SETTLED",
            "groupBy": "MARKET",
            "marketIds": [market],
            "settledDateRange": date_range or None,
            "fromRecord": 0,
            "recordCount": 1000,
            "venue_id": self._venue_id,
            "account_id": account_identity,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
        }
        try:
            response = self._client._rpc(_betfair._LIST_CLEARED_ORDERS, params)
        except BetfairReadOnlyError as exc:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair MARKET commission acquisition failed"
            ) from exc
        report = _mapping(response.result, "listClearedOrders result")
        more_available = report.get("moreAvailable")
        if type(more_available) is not bool or more_available:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair MARKET commission capture is incomplete"
            )
        rows = report.get("clearedOrders")
        if not isinstance(rows, list) or len(rows) != 1:
            raise BetfairMarketCommissionAuthorityError(
                "exactly one MARKET rollup is required"
            )
        row = _mapping(rows[0], "clearedOrders[0]")
        returned_market = _canonical_text(row.get("marketId"), "marketId")
        if returned_market != market:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair returned a different market"
            )
        commission = _provider_decimal(row.get("commission"), "commission")
        if commission < 0:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair MARKET commission cannot be negative"
            )
        profit = _provider_decimal(row.get("profit"), "profit")
        settled_at = _parse_provider_time(
            _string(row.get("settledDate"), "settledDate"), "settledDate"
        )
        account_observed = _parse_provider_time(
            details.evidence.observed_at, "account observed_at"
        )
        cleared_observed = _parse_provider_time(
            response.evidence.observed_at, "cleared observed_at"
        )
        observed_at = max(account_observed, cleared_observed)
        available_at = observed_at
        candidate = BetfairMarketCommissionReceipt(
            venue_id=self._venue_id,
            account_id=account_identity,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            market_id=market,
            commission=commission,
            profit=profit,
            currency=currency,
            settled_at=settled_at,
            observed_at=observed_at,
            available_at=available_at,
            account_details_sha256=details.evidence.source_payload_sha256,
            cleared_orders_sha256=response.evidence.source_payload_sha256,
            request_scope_sha256=_digest(request_scope),
            supersedes_receipt_id=None,
        )
        records = self._load_records()
        predecessors = [
            value
            for value in records
            if value.market_id == market
            and value.venue_id == self._venue_id
            and value.account_id == account_identity
        ]
        predecessor = max(
            predecessors, key=lambda value: value.available_at, default=None
        )
        if records and not self._issued_receipt_ids:
            if predecessor is not None and _same_source_evidence(
                predecessor, candidate
            ):
                self._issued_receipt_ids.add(predecessor.receipt_id)
                return predecessor
            raise BetfairMarketCommissionAuthorityError(
                "durable Betfair commission history requires exact authenticated "
                "reacquisition before it can regain source authority"
            )
        if predecessor is not None:
            if _same_source_evidence(predecessor, candidate):
                self._issued_receipt_ids.add(predecessor.receipt_id)
                return predecessor
            candidate = BetfairMarketCommissionReceipt(
                venue_id=candidate.venue_id,
                account_id=candidate.account_id,
                adapter_id=candidate.adapter_id,
                adapter_version=candidate.adapter_version,
                market_id=candidate.market_id,
                commission=candidate.commission,
                profit=candidate.profit,
                currency=candidate.currency,
                settled_at=candidate.settled_at,
                observed_at=candidate.observed_at,
                available_at=candidate.available_at,
                account_details_sha256=candidate.account_details_sha256,
                cleared_orders_sha256=candidate.cleared_orders_sha256,
                request_scope_sha256=candidate.request_scope_sha256,
                supersedes_receipt_id=predecessor.receipt_id,
            )
        self._publish(
            tuple(sorted((*records, candidate), key=lambda value: value.receipt_id))
        )
        self._issued_receipt_ids.add(candidate.receipt_id)
        return candidate

    def resolve(
        self,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> BetfairMarketCommissionReceipt:
        _sha256(receipt_id, "receipt_id")
        _sha256(record_sha256, "record_sha256")
        _utc(as_of, "as_of")
        if receipt_id not in self._issued_receipt_ids:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission source authority requires authenticated "
                "acquisition in the current process"
            )
        records = self._load_records()
        matches = [value for value in records if value.receipt_id == receipt_id]
        if len(matches) != 1:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission receipt is not uniquely committed"
            )
        receipt = matches[0]
        if receipt.record_sha256 != record_sha256:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission receipt digest mismatch"
            )
        if receipt.available_at > as_of:
            raise BetfairMarketCommissionAuthorityError(
                "future Betfair commission receipt cannot be backdated"
            )
        for successor in records:
            if (
                successor.supersedes_receipt_id == receipt.receipt_id
                and successor.available_at <= as_of
            ):
                raise BetfairMarketCommissionAuthorityError(
                    "Betfair commission receipt was superseded before as_of"
                )
        return receipt

    def verify(self) -> tuple[BetfairMarketCommissionReceipt, ...]:
        """Validate durable integrity only; this does not mint source authority."""
        return self._load_records()

    def _load_records(self) -> tuple[BetfairMarketCommissionReceipt, ...]:
        path = self._state_path()
        if not path.exists():
            self._authority.recover(observed_state_sha256=None)
            return ()
        raw = _strict_json(path.read_bytes(), "Betfair commission state")
        _keys(
            raw,
            {"schema_version", "records", "state_sha256"},
            "Betfair commission state",
        )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise BetfairMarketCommissionAuthorityError(
                "unsupported Betfair commission state schema"
            )
        records = tuple(
            BetfairMarketCommissionReceipt.from_dict(_mapping(value, "record"))
            for value in _list(raw["records"], "records")
        )
        if tuple(sorted(records, key=lambda value: value.receipt_id)) != records:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission receipts are not canonically sorted"
            )
        _validate_lineage(records)
        state_sha256 = _state_digest(records)
        if raw["state_sha256"] != state_sha256:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission state digest mismatch"
            )
        self._authority.recover(
            observed_state_sha256=state_sha256,
            tx_id=state_sha256,
            semantic_binding_sha256=_binding(state_sha256),
        )
        return records

    def _publish(
        self, records: tuple[BetfairMarketCommissionReceipt, ...]
    ) -> None:
        _validate_lineage(records)
        previous_records = self._load_records()
        previous = (
            None
            if not self._state_path().exists()
            else _state_digest(previous_records)
        )
        intended = _state_digest(records)
        if previous == intended:
            return
        binding = _binding(intended)
        prepared = self._authority.prepare(
            tx_id=intended,
            observed_state_sha256=previous,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        if (
            prepared.phase is not AuthorityPhase.PREPARE
            or prepared.intended_state_sha256 != intended
        ):
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission state did not acquire exact PREPARE"
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": [value.to_dict() for value in records],
            "state_sha256": intended,
        }
        _atomic_json(self._state_path(), payload)
        observed = _strict_json(
            self._state_path().read_bytes(), "Betfair commission state"
        )
        if observed.get("state_sha256") != intended:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission durable re-read failed"
            )
        self._authority.commit(
            tx_id=intended,
            observed_state_sha256=intended,
            semantic_binding_sha256=binding,
        )

    def _state_path(self) -> Path:
        return self.root / "betfair_market_commission" / "state.json"


def _provider_account_identity(account_details_sha256: str) -> str:
    _sha256(account_details_sha256, "account_details_sha256")
    return f"betfair-account-evidence:{account_details_sha256}"


def _same_source_evidence(
    prior: BetfairMarketCommissionReceipt,
    candidate: BetfairMarketCommissionReceipt,
) -> bool:
    return (
        prior.venue_id == candidate.venue_id
        and prior.account_id == candidate.account_id
        and prior.market_id == candidate.market_id
        and prior.cleared_orders_sha256 == candidate.cleared_orders_sha256
        and prior.account_details_sha256 == candidate.account_details_sha256
        and prior.request_scope_sha256 == candidate.request_scope_sha256
        and prior.commission == candidate.commission
        and prior.profit == candidate.profit
        and prior.currency == candidate.currency
        and prior.settled_at == candidate.settled_at
    )


def _validate_lineage(
    records: Sequence[BetfairMarketCommissionReceipt],
) -> None:
    by_id = {value.receipt_id: value for value in records}
    if len(by_id) != len(records):
        raise BetfairMarketCommissionAuthorityError(
            "duplicate Betfair commission receipt identity"
        )
    successors: dict[str, str] = {}
    for value in records:
        predecessor = value.supersedes_receipt_id
        if predecessor is None:
            continue
        prior = by_id.get(predecessor)
        if prior is None:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission predecessor is missing"
            )
        if predecessor in successors:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission receipt has multiple successors"
            )
        if (
            prior.venue_id != value.venue_id
            or prior.account_id != value.account_id
            or prior.market_id != value.market_id
            or prior.currency != value.currency
        ):
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission correction rewrites source identity"
            )
        if value.available_at < prior.available_at:
            raise BetfairMarketCommissionAuthorityError(
                "Betfair commission correction predates predecessor"
            )
        successors[predecessor] = value.receipt_id
    for start in by_id:
        seen: set[str] = set()
        cursor: str | None = start
        while cursor is not None:
            if cursor in seen:
                raise BetfairMarketCommissionAuthorityError(
                    "Betfair commission lineage contains a cycle"
                )
            seen.add(cursor)
            cursor = successors.get(cursor)


def _state_digest(
    records: Sequence[BetfairMarketCommissionReceipt],
) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "records": [value.to_dict() for value in records],
        }
    )


def _binding(state_sha256: str) -> str:
    return _digest(
        {
            "domain": "autosport.betfair_market_commission.v1",
            "state_sha256": state_sha256,
        }
    )


def _canonical_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be non-empty canonical text"
        )
    return value


def _canonical_currency(value: Any) -> str:
    text = _canonical_text(value, "currency")
    if _CURRENCY_RE.fullmatch(text) is None:
        raise BetfairMarketCommissionAuthorityError(
            "Betfair account currency is not canonical"
        )
    return text


def _canonical_provider_time(value: str, label: str) -> str:
    parsed = _parse_provider_time(value, label)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_provider_time(value: Any, label: str) -> datetime:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _provider_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, Decimal)
    ):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be provider numeric data"
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is invalid"
        ) from exc
    if not parsed.is_finite():
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be finite"
        )
    return parsed


def _text(value: str, label: str) -> None:
    _canonical_text(value, label)


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be lowercase SHA-256 hex"
        )


def _utc(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be timezone-aware"
        )
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be UTC"
        )


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise BetfairMarketCommissionAuthorityError("decimal must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _string(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is invalid"
        ) from exc
    if _decimal_text(parsed) != text:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is not canonical Decimal text"
        )
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must use UTC Z notation"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is invalid"
        ) from exc
    if _datetime_text(parsed) != text:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is not canonical"
        )
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} keys mismatch"
        )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be a string"
        )
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be an object"
        )
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must be an array"
        )
    return value


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is not UTF-8"
        ) from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BetfairMarketCommissionAuthorityError(
                    f"{label} contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} contains non-standard number {value}"
        )

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BetfairMarketCommissionAuthorityError(
            f"{label} is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise BetfairMarketCommissionAuthorityError(
            f"{label} must contain an object"
        )
    return parsed


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((_canonical_json(payload) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            directory = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
