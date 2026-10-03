from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import math
import time
from typing import Any, Callable
from urllib.parse import urlencode

from .matchbook_provider import (
    MatchbookHttpJsonResponse,
    MatchbookTransportError,
    _default_transport,
    _optional_sha256,
    _runtime_float,
    _session_token,
)

BASE = "https://api.matchbook.com"
TRANSACTIONS_PATH = "/edge/rest/reports/v1/transactions"
BALANCE_PATH = "/edge/rest/account/balance"
MAX_PAGE = 500
MAX_ATTEMPTS = 5


class MatchbookWalletEvidenceError(ValueError):
    """Matchbook wallet evidence is malformed, ambiguous, or incomplete."""


class TransactionType(StrEnum):
    PAYOUT = "payout"
    COMMISSION = "commission"
    TRANSFER = "transfer"
    CANCEL = "cancel"
    MANUAL = "manual"
    BONUS = "bonus"


@dataclass(frozen=True, slots=True)
class WalletTransaction:
    transaction_id: str
    occurred_at: str
    transaction_type: TransactionType
    product: str
    detail: str
    debit: Decimal
    credit: Decimal
    balance: Decimal
    currency: str
    third_party_transaction_id: str | None
    row_sha256: str

    @property
    def cash_delta(self) -> Decimal:
        return self.debit + self.credit


@dataclass(frozen=True, slots=True)
class WalletPage:
    offset: int
    per_page: int
    rows: tuple[WalletTransaction, ...]
    observed_at: str
    response_sha256: str
    page_sha256: str


@dataclass(frozen=True, slots=True)
class WalletWindow:
    after: str
    before: str
    transaction_types: tuple[TransactionType, ...]
    per_page: int
    pages: tuple[WalletPage, ...]
    transactions: tuple[WalletTransaction, ...]
    total_cash_delta: Decimal
    payout_cashflow: Decimal
    commission_cashflow: Decimal
    transfer_cashflow: Decimal
    cancel_cashflow: Decimal
    manual_cashflow: Decimal
    bonus_cashflow: Decimal
    observed_at: str
    evidence_sha256: str
    pagination_complete: bool = True
    strategy_net_economic_pnl: None = None
    provider_api_cost_cashflow: None = None
    positive_net_edge_proven: bool = False


@dataclass(frozen=True, slots=True)
class BalanceEvidence:
    account_id: str
    balance: Decimal
    exposure: Decimal
    commission_reserve: Decimal
    free_funds: Decimal
    observed_at: str
    response_sha256: str
    evidence_sha256: str
    transaction_attribution_authorized: bool = False
    strategy_reward_authorized: bool = False


Transport = Callable[[str, Mapping[str, str], float], MatchbookHttpJsonResponse]
Clock = Callable[[], str]


class MatchbookWalletEvidenceClient:
    """Authenticated GET-only Matchbook cash evidence.

    Wallet cash is descriptive provider evidence. It never authorizes campaign
    allocation, settled-bet attribution, strategy reward, API-fee attribution,
    real-money execution, or positive net edge.
    """

    def __init__(
        self,
        session_token: str,
        *,
        transport: Transport = _default_transport,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_backoff_seconds: float = 2.0,
    ) -> None:
        self._token = _session_token(session_token)
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._sleeper = sleeper
        self._timeout = _runtime_float(
            timeout_seconds, field="timeout_seconds", positive=True
        )
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise MatchbookWalletEvidenceError("max_attempts is out of range")
        self._max_attempts = max_attempts
        self._max_backoff = _runtime_float(
            max_backoff_seconds,
            field="max_backoff_seconds",
            positive=False,
        )

    def read_transaction_window(
        self,
        *,
        after: str,
        before: str,
        transaction_types: tuple[TransactionType, ...] = tuple(TransactionType),
        per_page: int = 100,
        max_pages: int = 1000,
    ) -> WalletWindow:
        after, before = _dt(after, "after"), _dt(before, "before")
        if _parse(after) >= _parse(before):
            raise MatchbookWalletEvidenceError("after must precede before")
        types = _types(transaction_types)
        if type(per_page) is not int or not 1 <= per_page <= MAX_PAGE:
            raise MatchbookWalletEvidenceError("per_page is out of range")
        if type(max_pages) is not int or max_pages <= 0:
            raise MatchbookWalletEvidenceError("max_pages must be positive")

        pages: list[WalletPage] = []
        rows: list[WalletTransaction] = []
        seen: dict[str, WalletTransaction] = {}
        offset = 0

        for _ in range(max_pages):
            observed = _dt(self._clock(), "observed_at")
            response = self._request(
                self._transactions_url(after, before, types, offset, per_page)
            )
            body_hash = _optional_sha256(response.body_sha256)
            if body_hash is None:
                raise MatchbookWalletEvidenceError(
                    "transaction response lacks body SHA-256"
                )
            raw_rows = _rows(response.payload)
            if len(raw_rows) > per_page:
                raise MatchbookWalletEvidenceError(
                    "provider exceeded requested page size"
                )
            parsed = tuple(_transaction(row) for row in raw_rows)
            for row in parsed:
                when = _parse(row.occurred_at)
                if not (_parse(after) < when < _parse(before)):
                    raise MatchbookWalletEvidenceError(
                        "transaction outside query window"
                    )
                previous = seen.get(row.transaction_id)
                if previous is not None:
                    if previous != row:
                        raise MatchbookWalletEvidenceError(
                            "transaction id changed in page walk"
                        )
                    raise MatchbookWalletEvidenceError(
                        "duplicate transaction id in page walk"
                    )
                seen[row.transaction_id] = row
                rows.append(row)

            page_payload = {
                "offset": offset,
                "per_page": per_page,
                "observed_at": observed,
                "response_sha256": body_hash,
                "rows": [row.row_sha256 for row in parsed],
            }
            pages.append(
                WalletPage(
                    offset,
                    per_page,
                    parsed,
                    observed,
                    body_hash,
                    _digest(page_payload),
                )
            )
            if len(parsed) < per_page:
                break
            offset += len(parsed)
        else:
            raise MatchbookWalletEvidenceError("pagination exceeded max_pages")

        txs = tuple(rows)
        sums = {
            kind: sum(
                (row.cash_delta for row in txs if row.transaction_type is kind),
                Decimal("0"),
            )
            for kind in TransactionType
        }
        total = sum((row.cash_delta for row in txs), Decimal("0"))
        observed = max(page.observed_at for page in pages)
        payload = _window_payload(
            after=after,
            before=before,
            transaction_types=types,
            per_page=per_page,
            pages=tuple(pages),
            transactions=txs,
            sums=sums,
            total=total,
            observed_at=observed,
        )
        return WalletWindow(
            after=after,
            before=before,
            transaction_types=types,
            per_page=per_page,
            pages=tuple(pages),
            transactions=txs,
            total_cash_delta=total,
            payout_cashflow=sums[TransactionType.PAYOUT],
            commission_cashflow=sums[TransactionType.COMMISSION],
            transfer_cashflow=sums[TransactionType.TRANSFER],
            cancel_cashflow=sums[TransactionType.CANCEL],
            manual_cashflow=sums[TransactionType.MANUAL],
            bonus_cashflow=sums[TransactionType.BONUS],
            observed_at=observed,
            evidence_sha256=_digest(payload),
        )

    def read_balance(self) -> BalanceEvidence:
        observed = _dt(self._clock(), "observed_at")
        response = self._request(f"{BASE}{BALANCE_PATH}")
        body_hash = _optional_sha256(response.body_sha256)
        if body_hash is None:
            raise MatchbookWalletEvidenceError(
                "balance response lacks body SHA-256"
            )
        raw = response.payload
        if not isinstance(raw, dict):
            raise MatchbookWalletEvidenceError(
                "balance response must be an object"
            )
        account_id = _id(raw.get("id"), "balance.id")
        balance = _decimal(raw.get("balance"), "balance")
        exposure = _decimal(raw.get("exposure"), "exposure")
        reserve = _decimal(
            raw.get("commission-reserve"), "commission-reserve"
        )
        free = _decimal(raw.get("free-funds"), "free-funds")
        if min(balance, exposure, reserve, free) < 0:
            raise MatchbookWalletEvidenceError(
                "balance components must be non-negative"
            )
        payload = _balance_payload(
            account_id,
            balance,
            exposure,
            reserve,
            free,
            observed,
            body_hash,
        )
        return BalanceEvidence(
            account_id=account_id,
            balance=balance,
            exposure=exposure,
            commission_reserve=reserve,
            free_funds=free,
            observed_at=observed,
            response_sha256=body_hash,
            evidence_sha256=_digest(payload),
        )

    @staticmethod
    def _transactions_url(
        after: str,
        before: str,
        types: tuple[TransactionType, ...],
        offset: int,
        per_page: int,
    ) -> str:
        query = [
            ("offset", str(offset)),
            ("per-page", str(per_page)),
            ("transaction-type", ",".join(kind.value for kind in types)),
            ("after", after),
            ("before", before),
        ]
        return f"{BASE}{TRANSACTIONS_PATH}?{urlencode(query)}"

    def _request(self, url: str) -> MatchbookHttpJsonResponse:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Autosport/0.1 matchbook-wallet-evidence",
            "session-token": self._token,
        }
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = self._transport(url, headers, self._timeout)
                if type(result) is not MatchbookHttpJsonResponse:
                    raise MatchbookWalletEvidenceError(
                        "transport returned wrong type"
                    )
                if type(result.status_code) is not int:
                    raise MatchbookWalletEvidenceError(
                        "status_code must be integer"
                    )
                if not 200 <= result.status_code < 300:
                    raise MatchbookTransportError(
                        f"Matchbook HTTP {result.status_code}",
                        status_code=result.status_code,
                    )
                return result
            except MatchbookTransportError as exc:
                retryable = exc.status_code == 429 or (
                    exc.status_code is not None and exc.status_code >= 500
                )
                if not retryable or attempt >= self._max_attempts:
                    raise
                delay: object = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else 0.25 * attempt
                )
                if (
                    isinstance(delay, bool)
                    or not isinstance(delay, (int, float))
                    or not math.isfinite(float(delay))
                    or float(delay) < 0
                ):
                    delay = 0.25 * attempt
                self._sleeper(
                    min(float(delay), self._max_backoff)
                )
        raise AssertionError("unreachable")


def deduplicate_wallet_windows(
    windows: tuple[WalletWindow, ...],
) -> tuple[WalletTransaction, ...]:
    """Collapse exact restart overlap; conflicting same-id history fails closed."""
    if type(windows) is not tuple or not windows:
        raise MatchbookWalletEvidenceError(
            "windows must be a non-empty tuple"
        )
    seen: dict[str, WalletTransaction] = {}
    for window in windows:
        if type(window) is not WalletWindow:
            raise MatchbookWalletEvidenceError(
                "windows require exact WalletWindow"
            )
        for row in window.transactions:
            old = seen.get(row.transaction_id)
            if old is not None and old != row:
                raise MatchbookWalletEvidenceError(
                    "transaction id changed across reread"
                )
            seen[row.transaction_id] = row
    return tuple(
        sorted(
            seen.values(),
            key=lambda row: (_parse(row.occurred_at), row.transaction_id),
        )
    )


def validate_wallet_window(value: WalletWindow) -> bool:
    """Revalidate cached evidence before a later consumer uses it."""
    if type(value) is not WalletWindow:
        return False
    if (
        value.pagination_complete is not True
        or value.strategy_net_economic_pnl is not None
        or value.provider_api_cost_cashflow is not None
        or value.positive_net_edge_proven is not False
        or not value.pages
    ):
        return False
    try:
        if _dt(value.after, "after") != value.after:
            return False
        if _dt(value.before, "before") != value.before:
            return False
        _types(value.transaction_types)
    except (MatchbookWalletEvidenceError, ValueError, TypeError):
        return False
    if tuple(row for page in value.pages for row in page.rows) != value.transactions:
        return False

    expected_offset = 0
    for index, page in enumerate(value.pages):
        if (
            type(page) is not WalletPage
            or page.offset != expected_offset
            or page.per_page != value.per_page
        ):
            return False
        if index < len(value.pages) - 1 and len(page.rows) != value.per_page:
            return False
        if not all(_valid_row(row) for row in page.rows):
            return False
        page_payload = {
            "offset": page.offset,
            "per_page": page.per_page,
            "observed_at": page.observed_at,
            "response_sha256": page.response_sha256,
            "rows": [row.row_sha256 for row in page.rows],
        }
        if page.page_sha256 != _digest(page_payload):
            return False
        expected_offset += len(page.rows)
    if len(value.pages[-1].rows) >= value.per_page:
        return False
    if len({row.transaction_id for row in value.transactions}) != len(
        value.transactions
    ):
        return False

    sums = {
        kind: sum(
            (
                row.cash_delta
                for row in value.transactions
                if row.transaction_type is kind
            ),
            Decimal("0"),
        )
        for kind in TransactionType
    }
    total = sum(
        (row.cash_delta for row in value.transactions),
        Decimal("0"),
    )
    if (
        value.total_cash_delta != total
        or value.payout_cashflow != sums[TransactionType.PAYOUT]
        or value.commission_cashflow != sums[TransactionType.COMMISSION]
        or value.transfer_cashflow != sums[TransactionType.TRANSFER]
        or value.cancel_cashflow != sums[TransactionType.CANCEL]
        or value.manual_cashflow != sums[TransactionType.MANUAL]
        or value.bonus_cashflow != sums[TransactionType.BONUS]
        or value.observed_at != max(page.observed_at for page in value.pages)
    ):
        return False
    payload = _window_payload(
        after=value.after,
        before=value.before,
        transaction_types=value.transaction_types,
        per_page=value.per_page,
        pages=value.pages,
        transactions=value.transactions,
        sums=sums,
        total=total,
        observed_at=value.observed_at,
    )
    return value.evidence_sha256 == _digest(payload)


def validate_balance_evidence(value: BalanceEvidence) -> bool:
    if type(value) is not BalanceEvidence:
        return False
    if value.transaction_attribution_authorized or value.strategy_reward_authorized:
        return False
    if any(
        type(amount) is not Decimal
        or not amount.is_finite()
        or amount < 0
        for amount in (
            value.balance,
            value.exposure,
            value.commission_reserve,
            value.free_funds,
        )
    ):
        return False
    try:
        if _dt(value.observed_at, "observed_at") != value.observed_at:
            return False
        if _optional_sha256(value.response_sha256) is None:
            return False
    except (ValueError, TypeError, MatchbookWalletEvidenceError):
        return False
    payload = _balance_payload(
        value.account_id,
        value.balance,
        value.exposure,
        value.commission_reserve,
        value.free_funds,
        value.observed_at,
        value.response_sha256,
    )
    return value.evidence_sha256 == _digest(payload)


def _rows(payload: object) -> list[Mapping[str, Any]]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("transactions"), list)
    ):
        raise MatchbookWalletEvidenceError(
            "response requires transactions[]"
        )
    rows = payload["transactions"]
    if not all(isinstance(row, dict) for row in rows):
        raise MatchbookWalletEvidenceError(
            "transaction rows must be objects"
        )
    return rows


def _transaction(raw: Mapping[str, Any]) -> WalletTransaction:
    def alias(*keys: str) -> object:
        found = [key for key in keys if key in raw]
        if len(found) != 1:
            raise MatchbookWalletEvidenceError(
                "ambiguous transaction field aliases"
            )
        return raw[found[0]]

    transaction_id = _id(raw.get("id"), "transaction.id")
    occurred = _dt(
        alias("time", "time-settled"),
        "transaction.time",
    )
    raw_type = alias("transaction-type", "type")
    if not isinstance(raw_type, str):
        raise MatchbookWalletEvidenceError(
            "transaction type must be text"
        )
    try:
        kind = TransactionType(raw_type.strip().lower())
    except ValueError as exc:
        raise MatchbookWalletEvidenceError(
            "unsupported transaction type"
        ) from exc

    product = raw.get("product")
    detail = raw.get("detail", "")
    if (
        not isinstance(product, str)
        or not product
        or product != product.strip()
    ):
        raise MatchbookWalletEvidenceError(
            "product must be trimmed text"
        )
    if not isinstance(detail, str) or detail != detail.strip():
        raise MatchbookWalletEvidenceError(
            "detail must be trimmed text"
        )

    debit = _decimal(raw.get("debit"), "debit")
    credit = _decimal(raw.get("credit"), "credit")
    balance = _decimal(raw.get("balance"), "balance")
    if debit > 0 or credit < 0 or (
        debit != 0 and credit != 0
    ):
        raise MatchbookWalletEvidenceError(
            "debit/credit signs are incoherent"
        )
    currency = _currency(raw.get("currency"))
    third = raw.get("third-party-transaction-id")
    third_id = (
        None
        if third in (None, "")
        else _id(third, "third-party-transaction-id")
    )
    payload = {
        "id": transaction_id,
        "time": occurred,
        "transaction_type": kind.value,
        "product": product,
        "detail": detail,
        "debit": _d(debit),
        "credit": _d(credit),
        "balance": _d(balance),
        "currency": currency,
        "third_party_transaction_id": third_id,
    }
    return WalletTransaction(
        transaction_id=transaction_id,
        occurred_at=occurred,
        transaction_type=kind,
        product=product,
        detail=detail,
        debit=debit,
        credit=credit,
        balance=balance,
        currency=currency,
        third_party_transaction_id=third_id,
        row_sha256=_digest(payload),
    )


def _valid_row(value: WalletTransaction) -> bool:
    if (
        type(value) is not WalletTransaction
        or type(value.transaction_type) is not TransactionType
    ):
        return False
    payload = {
        "id": value.transaction_id,
        "time": value.occurred_at,
        "transaction_type": value.transaction_type.value,
        "product": value.product,
        "detail": value.detail,
        "debit": _d(value.debit),
        "credit": _d(value.credit),
        "balance": _d(value.balance),
        "currency": value.currency,
        "third_party_transaction_id": value.third_party_transaction_id,
    }
    return value.row_sha256 == _digest(payload)


def _types(values: object) -> tuple[TransactionType, ...]:
    if type(values) is not tuple or not values:
        raise MatchbookWalletEvidenceError(
            "transaction_types must be a non-empty tuple"
        )
    if any(type(value) is not TransactionType for value in values):
        raise MatchbookWalletEvidenceError(
            "transaction_types must use TransactionType"
        )
    if len(values) != len(set(values)):
        raise MatchbookWalletEvidenceError(
            "transaction_types must be unique"
        )
    canonical = tuple(kind for kind in TransactionType if kind in values)
    if values != canonical:
        raise MatchbookWalletEvidenceError(
            "transaction_types must use canonical order"
        )
    return values


def _window_payload(
    *,
    after: str,
    before: str,
    transaction_types: tuple[TransactionType, ...],
    per_page: int,
    pages: tuple[WalletPage, ...],
    transactions: tuple[WalletTransaction, ...],
    sums: dict[TransactionType, Decimal],
    total: Decimal,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "schema": "matchbook-wallet-window-v1",
        "after": after,
        "before": before,
        "transaction_types": [
            kind.value for kind in transaction_types
        ],
        "per_page": per_page,
        "pages": [page.page_sha256 for page in pages],
        "transactions": [
            row.row_sha256 for row in transactions
        ],
        "cashflow": {
            kind.value: _d(sums[kind])
            for kind in TransactionType
        },
        "total_cash_delta": _d(total),
        "observed_at": observed_at,
        "pagination_complete": True,
        "strategy_net_economic_pnl": None,
        "provider_api_cost_cashflow": None,
        "positive_net_edge_proven": False,
    }


def _balance_payload(
    account_id: str,
    balance: Decimal,
    exposure: Decimal,
    reserve: Decimal,
    free_funds: Decimal,
    observed_at: str,
    response_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": "matchbook-balance-v1",
        "account_id": account_id,
        "balance": _d(balance),
        "exposure": _d(exposure),
        "commission_reserve": _d(reserve),
        "free_funds": _d(free_funds),
        "observed_at": observed_at,
        "response_sha256": response_sha256,
        "transaction_attribution_authorized": False,
        "strategy_reward_authorized": False,
    }


def _dt(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise MatchbookWalletEvidenceError(
            f"{label} must be trimmed ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise MatchbookWalletEvidenceError(
            f"{label} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookWalletEvidenceError(
            f"{label} must be timezone-aware"
        )
    return (
        parsed.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(
        value,
        (int, str, Decimal),
    ):
        raise MatchbookWalletEvidenceError(
            f"{label} must use exact decimal data"
        )
    if isinstance(value, str) and (
        not value or value != value.strip()
    ):
        raise MatchbookWalletEvidenceError(
            f"{label} must be canonical decimal text"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MatchbookWalletEvidenceError(
            f"{label} is not decimal"
        ) from exc
    if not result.is_finite():
        raise MatchbookWalletEvidenceError(
            f"{label} must be finite"
        )
    return result


def _id(value: object, label: str) -> str:
    if type(value) is int and value > 0:
        return str(value)
    if (
        isinstance(value, str)
        and value
        and value == value.strip()
    ):
        return value
    raise MatchbookWalletEvidenceError(
        f"{label} must be a provider id"
    )


def _currency(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or value != value.upper()
        or not value.isalpha()
    ):
        raise MatchbookWalletEvidenceError(
            "currency must be uppercase ISO-like code"
        )
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _d(value: Decimal) -> str:
    return format(value, "f")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
