from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Final
from urllib.parse import parse_qsl
from weakref import ref

from .smarkets_session_context import (
    SmarketsAuthenticatedSession,
    SmarketsSessionAuthenticatedRead,
    SmarketsSessionContextError,
)


class SmarketsAccountActivityError(RuntimeError):
    """Authenticated Smarkets account-activity evidence is malformed or incomplete."""


_ACTIVITY_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "admin.credit",
        "admin.debit",
        "bonus.credit",
        "bonus.debit",
        "commission.update",
        "contract.reduce",
        "contract.resettle",
        "contract.settle",
        "contract.unsettle",
        "contract.void",
        "contract.unreduce",
        "deposit",
        "deposit.approve",
        "deposit.deny",
        "deposit.request",
        "execution.reduce",
        "execution.settle",
        "execution.unsettle",
        "execution.unvoid",
        "execution.void",
        "market.partial_unsettle",
        "market.resettle",
        "market.settle",
        "market.unsettle",
        "market.unvoid",
        "market.void",
        "order.book.accept",
        "order.book.cancel",
        "order.book.reduce_qty",
        "order.book.reject",
        "order.cancel.reject",
        "order.cancel_replace.book.accept",
        "order.cancel_replace.book.reject",
        "order.create",
        "order.execute",
        "order.execute.confirm",
        "order.execute.void",
        "order.pending.replace",
        "order.reduce",
        "order.reject.account_suspended",
        "order.reject.insufficient_funds",
        "order.reject.invalid_quantity",
        "order.reject.limit_exceeded",
        "order.reject.market_not_found",
        "order.reject.stake_limit_exceeded",
        "order.settle",
        "order.unsettle",
        "order.unvoid",
        "order.void",
        "withdraw",
        "withdrawal.approve",
        "withdrawal.deny",
        "withdrawal.request",
        "cash_out.complete",
        "cash_out.request",
    }
)

_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "bet_token_type",
        "bonus_bet",
        "bonus_change",
        "commission",
        "contract_id",
        "event_id",
        "exposure",
        "extra",
        "label",
        "market_id",
        "money",
        "money_change",
        "order_id",
        "price",
        "quantity",
        "quantity_change",
        "quantity_user_currency",
        "quantity_user_currency_change",
        "seq",
        "side",
        "source",
        "subseq",
        "timestamp",
    }
)
_ROOT_FIELDS: Final[frozenset[str]] = frozenset(
    {"account_activity", "contracts", "events", "markets", "pagination"}
)
_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "timestamp_max",
        "timestamp_min",
        "limit",
        "market_id",
        "order_id",
        "pagination_last_seq",
        "pagination_last_subseq",
        "sort",
        "source",
        "event_info",
    }
)
_MULTI_QUERY_KEYS: Final[frozenset[str]] = frozenset({"market_id", "order_id", "source"})
_PROVIDER_ID = re.compile(r"^[0-9]+$")
_ISSUED: dict[int, tuple[object, str]] = {}


def _strict_json(raw: bytes) -> object:
    if type(raw) is not bytes or not raw:
        raise SmarketsAccountActivityError("account-activity payload must be non-empty bytes")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SmarketsAccountActivityError(
                    f"duplicate JSON object key in account-activity payload: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SmarketsAccountActivityError(
                    f"non-finite JSON constant in account-activity payload: {value}"
                )
            ),
        )
    except SmarketsAccountActivityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmarketsAccountActivityError(
            "account-activity payload is not strict UTF-8 JSON"
        ) from exc


def _object(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise SmarketsAccountActivityError(f"{name} must be a JSON object")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise SmarketsAccountActivityError(f"{name} must be non-empty text or null")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SmarketsAccountActivityError(f"{name} contains control characters")
    return value


def _provider_id(value: object, name: str) -> str | None:
    text = _optional_text(value, name)
    if text is not None and _PROVIDER_ID.fullmatch(text) is None:
        raise SmarketsAccountActivityError(f"{name} must be a numeric provider id or null")
    return text


def _decimal_text(value: object, name: str) -> Decimal | None:
    text = _optional_text(value, name)
    if text is None:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SmarketsAccountActivityError(
            f"{name} must be an exact decimal string or null"
        ) from exc
    if not number.is_finite():
        raise SmarketsAccountActivityError(
            f"{name} must be a finite exact decimal string or null"
        )
    return number


def _integer(value: object, name: str, *, nonnegative: bool = False) -> int:
    if type(value) is not int:
        raise SmarketsAccountActivityError(f"{name} must be an integer")
    if nonnegative and value < 0:
        raise SmarketsAccountActivityError(f"{name} must be non-negative")
    return value


def _optional_integer(
    value: object,
    name: str,
    *,
    nonnegative: bool = False,
) -> int | None:
    if value is None:
        return None
    return _integer(value, name, nonnegative=nonnegative)


def _provider_timestamp(value: object) -> str:
    if type(value) is not str or not value:
        raise SmarketsAccountActivityError("timestamp must be provider date-time text")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SmarketsAccountActivityError(
            "timestamp must be a valid provider date-time"
        ) from exc
    return value


def _canonical_json_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SmarketsAccountActivityRow:
    provider_account_id: str
    provider_currency: str
    seq: int
    subseq: int
    source: str
    provider_timestamp: str
    amount: Decimal | None
    commission: Decimal | None
    bonus_change: Decimal | None
    exposure: Decimal | None
    money: Decimal | None
    money_change: Decimal | None
    contract_id: str | None
    event_id: str | None
    market_id: str | None
    order_id: str | None
    price: int | None
    quantity: int | None
    quantity_change: int | None
    quantity_user_currency: int | None
    quantity_user_currency_change: int | None
    side: str | None
    label: str | None
    extra: str | None
    bet_token_type: str | None
    bonus_bet: bool | None
    row_sha256: str

    @property
    def provider_row_id(self) -> tuple[int, int]:
        return (self.seq, self.subseq)

    @property
    def attribution_scope(self) -> str:
        if self.order_id is not None:
            return "order"
        if self.market_id is not None:
            return "market"
        return "account"

    def to_canonical_dict(self) -> dict[str, object]:
        def decimal(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "provider_account_id": self.provider_account_id,
            "provider_currency": self.provider_currency,
            "seq": self.seq,
            "subseq": self.subseq,
            "source": self.source,
            "provider_timestamp": self.provider_timestamp,
            "amount": decimal(self.amount),
            "commission": decimal(self.commission),
            "bonus_change": decimal(self.bonus_change),
            "exposure": decimal(self.exposure),
            "money": decimal(self.money),
            "money_change": decimal(self.money_change),
            "contract_id": self.contract_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "order_id": self.order_id,
            "price": self.price,
            "quantity": self.quantity,
            "quantity_change": self.quantity_change,
            "quantity_user_currency": self.quantity_user_currency,
            "quantity_user_currency_change": self.quantity_user_currency_change,
            "side": self.side,
            "label": self.label,
            "extra": self.extra,
            "bet_token_type": self.bet_token_type,
            "bonus_bet": self.bonus_bet,
            "row_sha256": self.row_sha256,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SmarketsAccountActivityPageEvidence:
    session_generation_id: str
    account_context_sha256: str
    provider_account_id: str
    provider_currency: str
    endpoint: str
    request_query: str
    provider_date: str
    product_available_at: str
    source_payload_sha256: str
    source_read_evidence_sha256: str
    rows: tuple[SmarketsAccountActivityRow, ...]
    pagination_present: bool
    next_page_query: str | None
    query_exhausted: bool
    evidence_sha256: str

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.smarkets.account-activity.page",
            "schema_version": 1,
            "session_generation_id": self.session_generation_id,
            "account_context_sha256": self.account_context_sha256,
            "provider_account_id": self.provider_account_id,
            "provider_currency": self.provider_currency,
            "endpoint": self.endpoint,
            "request_query": self.request_query,
            "provider_date": self.provider_date,
            "product_available_at": self.product_available_at,
            "source_payload_sha256": self.source_payload_sha256,
            "source_read_evidence_sha256": self.source_read_evidence_sha256,
            "rows": [row.to_canonical_dict() for row in self.rows],
            "pagination_present": self.pagination_present,
            "next_page_query": self.next_page_query,
            "query_exhausted": self.query_exhausted,
        }


def _row(
    raw_value: object,
    *,
    provider_account_id: str,
    provider_currency: str,
) -> SmarketsAccountActivityRow:
    raw = _object(raw_value, "account_activity row")
    unknown = set(raw) - _ROW_FIELDS
    if unknown:
        raise SmarketsAccountActivityError(
            "account-activity row contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    for required in ("seq", "subseq", "source", "timestamp"):
        if required not in raw:
            raise SmarketsAccountActivityError(
                f"account-activity row lacks required evidence field: {required}"
            )

    seq = _integer(raw["seq"], "seq", nonnegative=True)
    subseq = _integer(raw["subseq"], "subseq", nonnegative=True)
    source = raw["source"]
    if type(source) is not str or source not in _ACTIVITY_SOURCES:
        raise SmarketsAccountActivityError("source is not a documented account-activity source")
    provider_timestamp = _provider_timestamp(raw["timestamp"])

    side = raw.get("side")
    if side is not None and side not in {"buy", "sell"}:
        raise SmarketsAccountActivityError("side must be buy, sell, or null")

    bet_token_type = raw.get("bet_token_type")
    if bet_token_type not in {None, "free_bet", "boost_bet"}:
        raise SmarketsAccountActivityError(
            "bet_token_type must be free_bet, boost_bet, or null"
        )

    bonus_bet = raw.get("bonus_bet")
    if bonus_bet is not None and type(bonus_bet) is not bool:
        raise SmarketsAccountActivityError("bonus_bet must be bool when present")

    row_sha256 = _canonical_json_sha256(raw)
    return SmarketsAccountActivityRow(
        provider_account_id=provider_account_id,
        provider_currency=provider_currency,
        seq=seq,
        subseq=subseq,
        source=source,
        provider_timestamp=provider_timestamp,
        amount=_decimal_text(raw.get("amount"), "amount"),
        commission=_decimal_text(raw.get("commission"), "commission"),
        bonus_change=_decimal_text(raw.get("bonus_change"), "bonus_change"),
        exposure=_decimal_text(raw.get("exposure"), "exposure"),
        money=_decimal_text(raw.get("money"), "money"),
        money_change=_decimal_text(raw.get("money_change"), "money_change"),
        contract_id=_provider_id(raw.get("contract_id"), "contract_id"),
        event_id=_provider_id(raw.get("event_id"), "event_id"),
        market_id=_provider_id(raw.get("market_id"), "market_id"),
        order_id=_provider_id(raw.get("order_id"), "order_id"),
        price=_optional_integer(raw.get("price"), "price", nonnegative=True),
        quantity=_optional_integer(raw.get("quantity"), "quantity", nonnegative=True),
        quantity_change=_optional_integer(raw.get("quantity_change"), "quantity_change"),
        quantity_user_currency=_optional_integer(
            raw.get("quantity_user_currency"),
            "quantity_user_currency",
            nonnegative=True,
        ),
        quantity_user_currency_change=_optional_integer(
            raw.get("quantity_user_currency_change"),
            "quantity_user_currency_change",
        ),
        side=side,
        label=_optional_text(raw.get("label"), "label"),
        extra=_optional_text(raw.get("extra"), "extra"),
        bet_token_type=bet_token_type,
        bonus_bet=bonus_bet,
        row_sha256=row_sha256,
    )


def _query_pairs(value: str, *, field: str) -> list[tuple[str, str]]:
    if type(value) is not str:
        raise SmarketsAccountActivityError(f"{field} must be text")
    try:
        pairs = parse_qsl(
            value[1:] if value.startswith("?") else value,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise SmarketsAccountActivityError(f"{field} is not a valid query string") from exc
    if any(key not in _QUERY_KEYS for key, _ in pairs):
        raise SmarketsAccountActivityError(f"{field} contains unsupported query keys")
    counts: dict[str, int] = {}
    for key, _ in pairs:
        counts[key] = counts.get(key, 0) + 1
    if any(counts.get(key, 0) > 1 for key in _QUERY_KEYS - _MULTI_QUERY_KEYS):
        raise SmarketsAccountActivityError(f"{field} repeats a scalar query key")
    return pairs


def _pair_values(pairs: list[tuple[str, str]], key: str) -> tuple[str, ...]:
    return tuple(value for pair_key, value in pairs if pair_key == key)


def _query_limit(request_pairs: list[tuple[str, str]]) -> int:
    values = _pair_values(request_pairs, "limit")
    if not values:
        return 20
    try:
        limit = int(values[0])
    except ValueError as exc:
        raise SmarketsAccountActivityError("request_query limit is not an integer") from exc
    if not 0 <= limit <= 500 or str(limit) != values[0]:
        raise SmarketsAccountActivityError("request_query limit is not canonical")
    return limit


def _effective_sort(request_pairs: list[tuple[str, str]]) -> str:
    values = _pair_values(request_pairs, "sort")
    if not values:
        return "-seq,-subseq"
    if values[0] not in {"seq,subseq", "-seq,-subseq"}:
        raise SmarketsAccountActivityError("request_query sort is unsupported")
    return values[0]


def _validate_row_order(
    rows: tuple[SmarketsAccountActivityRow, ...],
    request_pairs: list[tuple[str, str]],
) -> None:
    keys = [row.provider_row_id for row in rows]
    if len(set(keys)) != len(keys):
        raise SmarketsAccountActivityError(
            "account-activity page repeats provider seq/subseq identity"
        )
    if len(keys) < 2:
        return
    ascending = _effective_sort(request_pairs) == "seq,subseq"
    for previous, current in zip(keys, keys[1:]):
        if ascending and not previous < current:
            raise SmarketsAccountActivityError(
                "account-activity rows violate ascending seq/subseq order"
            )
        if not ascending and not previous > current:
            raise SmarketsAccountActivityError(
                "account-activity rows violate descending seq/subseq order"
            )


def _validate_next_page(
    next_page: str,
    *,
    request_pairs: list[tuple[str, str]],
    rows: tuple[SmarketsAccountActivityRow, ...],
) -> str:
    if type(next_page) is not str or not next_page.startswith("?"):
        raise SmarketsAccountActivityError(
            "pagination.next_page must be a provider query string or null"
        )
    if "#" in next_page or next_page.count("?") != 1:
        raise SmarketsAccountActivityError("pagination.next_page is not a pure query string")
    if not rows:
        raise SmarketsAccountActivityError("pagination.next_page cannot advance an empty page")

    next_pairs = _query_pairs(next_page, field="pagination.next_page")
    seq_values = _pair_values(next_pairs, "pagination_last_seq")
    subseq_values = _pair_values(next_pairs, "pagination_last_subseq")
    if len(seq_values) != 1 or len(subseq_values) != 1:
        raise SmarketsAccountActivityError(
            "pagination.next_page requires exactly one seq/subseq cursor"
        )
    last = rows[-1]
    if seq_values[0] != str(last.seq) or subseq_values[0] != str(last.subseq):
        raise SmarketsAccountActivityError(
            "pagination.next_page cursor does not match the last provider row"
        )

    current_seq = _pair_values(request_pairs, "pagination_last_seq")
    current_subseq = _pair_values(request_pairs, "pagination_last_subseq")
    if current_seq and (
        current_seq[0] == seq_values[0] and current_subseq[0] == subseq_values[0]
    ):
        raise SmarketsAccountActivityError("pagination.next_page does not advance")

    for key in _QUERY_KEYS - {
        "pagination_last_seq",
        "pagination_last_subseq",
        "limit",
        "sort",
        "event_info",
    }:
        if sorted(_pair_values(next_pairs, key)) != sorted(
            _pair_values(request_pairs, key)
        ):
            raise SmarketsAccountActivityError(
                f"pagination.next_page changes request scope: {key}"
            )

    defaults = {"limit": "20", "sort": "-seq,-subseq", "event_info": "false"}
    for key, default in defaults.items():
        current = _pair_values(request_pairs, key)
        successor = _pair_values(next_pairs, key)
        if current:
            if successor != current:
                raise SmarketsAccountActivityError(
                    f"pagination.next_page changes request scope: {key}"
                )
        elif successor and successor != (default,):
            raise SmarketsAccountActivityError(
                f"pagination.next_page invents non-default request scope: {key}"
            )
    return next_page


def _page_digest(page: SmarketsAccountActivityPageEvidence) -> str:
    return _canonical_json_sha256(page.to_canonical_dict())


def parse_smarkets_account_activity_page(
    session: SmarketsAuthenticatedSession,
    read: SmarketsSessionAuthenticatedRead,
) -> SmarketsAccountActivityPageEvidence:
    """Issue retrospective economic evidence from one exact session-bound page.

    This function does not infer tariff, final market settlement, per-order allocation,
    campaign ownership, or prospective decision-time knowledge.  It only preserves
    provider statement facts exposed by the exact account-activity response.
    """

    if type(session) is not SmarketsAuthenticatedSession:
        raise SmarketsAccountActivityError(
            "account-activity parsing requires canonical authenticated session"
        )
    try:
        resolved = session.resolve_account_activity_read(read)
    except SmarketsSessionContextError as exc:
        raise SmarketsAccountActivityError(
            "account-activity read is not authoritative for this live session"
        ) from exc
    if type(resolved) is not SmarketsSessionAuthenticatedRead:
        raise SmarketsAccountActivityError("resolved account-activity read is not canonical")
    if sha256(resolved.payload).hexdigest() != resolved.payload_sha256:
        raise SmarketsAccountActivityError(
            "account-activity payload bytes do not match the session-issued digest"
        )
    if len(resolved.payload) != resolved.payload_size:
        raise SmarketsAccountActivityError(
            "account-activity payload size does not match the session-issued evidence"
        )

    parsed = _object(_strict_json(resolved.payload), "account-activity response")
    unknown_root = set(parsed) - _ROOT_FIELDS
    if unknown_root:
        raise SmarketsAccountActivityError(
            "account-activity response contains unsupported fields: "
            + ", ".join(sorted(unknown_root))
        )
    if "account_activity" not in parsed or type(parsed["account_activity"]) is not list:
        raise SmarketsAccountActivityError(
            "account-activity response requires account_activity array"
        )
    for metadata_key in ("contracts", "events", "markets"):
        if metadata_key in parsed and type(parsed[metadata_key]) is not list:
            raise SmarketsAccountActivityError(
                f"{metadata_key} statement metadata must be an array"
            )

    rows = tuple(
        _row(
            item,
            provider_account_id=resolved.provider_account_id,
            provider_currency=resolved.provider_currency,
        )
        for item in parsed["account_activity"]
    )
    request_pairs = _query_pairs(resolved.request_query, field="request_query")
    limit = _query_limit(request_pairs)
    if len(rows) > limit:
        raise SmarketsAccountActivityError(
            "account-activity response exceeds the requested/default page limit"
        )
    if limit == 0 and rows:
        raise SmarketsAccountActivityError(
            "zero-limit account-activity query returned economic rows"
        )
    _validate_row_order(rows, request_pairs)

    pagination_present = "pagination" in parsed
    next_page_query: str | None = None
    query_exhausted = False
    if pagination_present:
        pagination = _object(parsed["pagination"], "pagination")
        if set(pagination) != {"next_page"}:
            raise SmarketsAccountActivityError(
                "pagination must contain exactly the provider next_page field"
            )
        next_page = pagination["next_page"]
        if next_page is None:
            query_exhausted = limit != 0
        else:
            next_page_query = _validate_next_page(
                next_page,
                request_pairs=request_pairs,
                rows=rows,
            )

    provisional = SmarketsAccountActivityPageEvidence(
        session_generation_id=resolved.session_generation_id,
        account_context_sha256=resolved.account_context_sha256,
        provider_account_id=resolved.provider_account_id,
        provider_currency=resolved.provider_currency,
        endpoint=resolved.endpoint,
        request_query=resolved.request_query,
        provider_date=resolved.provider_date,
        product_available_at=resolved.product_available_at,
        source_payload_sha256=resolved.payload_sha256,
        source_read_evidence_sha256=resolved.evidence_sha256,
        rows=rows,
        pagination_present=pagination_present,
        next_page_query=next_page_query,
        query_exhausted=query_exhausted,
        evidence_sha256="",
    )
    evidence_sha256 = _page_digest(provisional)
    page = SmarketsAccountActivityPageEvidence(
        session_generation_id=provisional.session_generation_id,
        account_context_sha256=provisional.account_context_sha256,
        provider_account_id=provisional.provider_account_id,
        provider_currency=provisional.provider_currency,
        endpoint=provisional.endpoint,
        request_query=provisional.request_query,
        provider_date=provisional.provider_date,
        product_available_at=provisional.product_available_at,
        source_payload_sha256=provisional.source_payload_sha256,
        source_read_evidence_sha256=provisional.source_read_evidence_sha256,
        rows=provisional.rows,
        pagination_present=provisional.pagination_present,
        next_page_query=provisional.next_page_query,
        query_exhausted=provisional.query_exhausted,
        evidence_sha256=evidence_sha256,
    )

    page_id = id(page)

    def forget(_weakref: object, *, key: int = page_id) -> None:
        _ISSUED.pop(key, None)

    _ISSUED[page_id] = (ref(page, forget), evidence_sha256)
    return page


def assert_smarkets_account_activity_page_authoritative(
    page: SmarketsAccountActivityPageEvidence,
) -> None:
    """Require exact in-process issuance and unchanged evidence fields."""

    if type(page) is not SmarketsAccountActivityPageEvidence:
        raise SmarketsAccountActivityError(
            "account-activity economic evidence type is not canonical"
        )
    record = _ISSUED.get(id(page))
    if record is None or record[0]() is not page:
        raise SmarketsAccountActivityError(
            "account-activity economic evidence was not issued by canonical parser"
        )
    if record[1] != page.evidence_sha256:
        raise SmarketsAccountActivityError(
            "account-activity economic evidence identity changed after issuance"
        )

    canonical = page.to_canonical_dict()
    expected = canonical.copy()
    expected["evidence_sha256"] = ""
    # evidence_sha256 is not part of to_canonical_dict; recompute over the exact
    # authority-bearing fields and reject object.__setattr__ mutation.
    if _page_digest(page) != page.evidence_sha256:
        raise SmarketsAccountActivityError(
            "account-activity economic evidence fields changed after issuance"
        )
