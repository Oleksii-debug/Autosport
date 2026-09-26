from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping


_SCHEMA_VERSION = 1
_POSITIONS_ENDPOINT = "/edge/rest/account/positions"
_INT64_MAX = (1 << 63) - 1
_HEX = frozenset("0123456789abcdef")


class MatchbookPositionEvidenceError(ValueError):
    """Fail-closed validation error for detached Matchbook position evidence."""


class MatchbookPositionPaginationError(MatchbookPositionEvidenceError):
    """The observed positions pagination walk is structurally incomplete or inconsistent."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MatchbookPositionEvidenceError(
            f"{name} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MatchbookPositionEvidenceError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise MatchbookPositionEvidenceError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookPositionEvidenceError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookPositionEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > _INT64_MAX:
        raise MatchbookPositionEvidenceError(
            f"{name} must be a non-negative signed int64"
        )
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0 or value > _INT64_MAX:
        raise MatchbookPositionEvidenceError(
            f"{name} must be a positive signed int64"
        )
    return value


def _id_tuple(values: object, name: str) -> tuple[int, ...]:
    if type(values) is not tuple:
        raise MatchbookPositionEvidenceError(f"{name} must be a tuple")
    normalized = tuple(_positive_int(value, f"{name}[]") for value in values)
    if len(set(normalized)) != len(normalized):
        raise MatchbookPositionEvidenceError(f"{name} must not contain duplicates")
    return tuple(sorted(normalized))


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookPositionEvidenceError(
            f"{name} must be an exact finite Decimal"
        )
    return value


def _optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, name)


def _decimal_text(value: Decimal) -> str:
    """Return a compact canonical numeric identity without fixed-point expansion."""

    if value.is_zero():
        return "0e0"

    sign, raw_digits, raw_exponent = value.as_tuple()
    digits = list(raw_digits)
    exponent = int(raw_exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits) or "0"
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{exponent}"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MatchbookPositionEvidenceError(
            "evidence payload is not canonical JSON"
        ) from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookPositionScope:
    """Non-secret request scope for GET /edge/rest/account/positions.

    Construction proves only structural request identity. The account/session fields are
    opaque non-secret product references and MUST NOT contain Matchbook credentials or the
    provider session token. A separate authenticated transport must establish provider
    origin and bind the actual request/response bytes.
    """

    account_context_id: str
    session_generation_id: str
    event_ids: tuple[int, ...] = ()
    market_ids: tuple[int, ...] = ()
    runner_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_context_id",
            _text(self.account_context_id, "account_context_id"),
        )
        object.__setattr__(
            self,
            "session_generation_id",
            _text(self.session_generation_id, "session_generation_id"),
        )
        object.__setattr__(self, "event_ids", _id_tuple(self.event_ids, "event_ids"))
        object.__setattr__(self, "market_ids", _id_tuple(self.market_ids, "market_ids"))
        object.__setattr__(self, "runner_ids", _id_tuple(self.runner_ids, "runner_ids"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "http_method": "GET",
            "endpoint": _POSITIONS_ENDPOINT,
            "account_context_id": self.account_context_id,
            "session_generation_id": self.session_generation_id,
            "event_ids": list(self.event_ids),
            "market_ids": list(self.market_ids),
            "runner_ids": list(self.runner_ids),
        }

    @property
    def scope_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookRunnerPositionObservation:
    """Provider-shaped potential P/L evidence for one Matchbook runner.

    Matchbook documents the positions endpoint as potential profit/loss per runner. These
    exact numbers are intentionally *not* renamed to stake, liability, gross return,
    realized P&L, settlement, bankroll, or execution truth.
    """

    provider_event_id: int
    provider_market_id: int
    provider_runner_id: int
    potential_profit: Decimal | None
    potential_loss: Decimal | None
    provider_row_sha256: str

    def __post_init__(self) -> None:
        for name in ("provider_event_id", "provider_market_id", "provider_runner_id"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        object.__setattr__(
            self,
            "potential_profit",
            _optional_decimal(self.potential_profit, "potential_profit"),
        )
        object.__setattr__(
            self,
            "potential_loss",
            _optional_decimal(self.potential_loss, "potential_loss"),
        )
        if self.potential_profit is None and self.potential_loss is None:
            raise MatchbookPositionEvidenceError(
                "position row must preserve potential_profit or potential_loss evidence"
            )
        object.__setattr__(
            self,
            "provider_row_sha256",
            _sha256(self.provider_row_sha256, "provider_row_sha256"),
        )

    @property
    def native_identity(self) -> tuple[int, int, int]:
        return (self.provider_event_id, self.provider_market_id, self.provider_runner_id)

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_event_id": self.provider_event_id,
            "provider_market_id": self.provider_market_id,
            "provider_runner_id": self.provider_runner_id,
            "potential_profit": (
                None if self.potential_profit is None else _decimal_text(self.potential_profit)
            ),
            "potential_loss": (
                None if self.potential_loss is None else _decimal_text(self.potential_loss)
            ),
            "provider_row_sha256": self.provider_row_sha256,
        }

    @property
    def observation_id(self) -> str:
        return _digest(self.to_payload())

    @property
    def stake_truth_proven(self) -> bool:
        return False

    @property
    def liability_truth_proven(self) -> bool:
        return False

    @property
    def gross_return_truth_proven(self) -> bool:
        return False

    @property
    def settlement_truth_proven(self) -> bool:
        return False

    @property
    def economic_pnl_truth_proven(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MatchbookPositionPage:
    """One structurally coherent detached page from the positions endpoint."""

    scope: MatchbookPositionScope
    offset: int
    per_page: int
    provider_total: int
    observed_at_utc: str
    request_semantics_sha256: str
    raw_response_sha256: str
    rows: tuple[MatchbookRunnerPositionObservation, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookPositionScope:
            raise MatchbookPositionEvidenceError(
                "scope must be an exact MatchbookPositionScope"
            )
        object.__setattr__(self, "offset", _non_negative_int(self.offset, "offset"))
        object.__setattr__(self, "per_page", _positive_int(self.per_page, "per_page"))
        object.__setattr__(
            self,
            "provider_total",
            _non_negative_int(self.provider_total, "provider_total"),
        )
        object.__setattr__(
            self,
            "observed_at_utc",
            _utc(self.observed_at_utc, "observed_at_utc"),
        )
        object.__setattr__(
            self,
            "request_semantics_sha256",
            _sha256(self.request_semantics_sha256, "request_semantics_sha256"),
        )
        object.__setattr__(
            self,
            "raw_response_sha256",
            _sha256(self.raw_response_sha256, "raw_response_sha256"),
        )
        if type(self.rows) is not tuple or len(self.rows) > self.per_page:
            raise MatchbookPositionEvidenceError(
                "rows must be a tuple no longer than per_page"
            )
        if self.offset > self.provider_total:
            raise MatchbookPositionEvidenceError("offset cannot exceed provider_total")
        if self.offset + len(self.rows) > self.provider_total:
            raise MatchbookPositionEvidenceError(
                "page rows cannot extend beyond provider_total"
            )

        seen: set[tuple[int, int, int]] = set()
        for row in self.rows:
            if type(row) is not MatchbookRunnerPositionObservation:
                raise MatchbookPositionEvidenceError(
                    "rows must contain exact MatchbookRunnerPositionObservation values"
                )
            if row.native_identity in seen:
                raise MatchbookPositionEvidenceError(
                    "provider runner position identity is duplicated within one page"
                )
            seen.add(row.native_identity)
            if self.scope.event_ids and row.provider_event_id not in self.scope.event_ids:
                raise MatchbookPositionEvidenceError(
                    "position row event is outside requested scope"
                )
            if self.scope.market_ids and row.provider_market_id not in self.scope.market_ids:
                raise MatchbookPositionEvidenceError(
                    "position row market is outside requested scope"
                )
            if self.scope.runner_ids and row.provider_runner_id not in self.scope.runner_ids:
                raise MatchbookPositionEvidenceError(
                    "position row runner is outside requested scope"
                )

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def product_issued_request_semantics_proven(self) -> bool:
        return False

    @property
    def page_reaches_reported_total(self) -> bool:
        return self.offset + len(self.rows) == self.provider_total

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _POSITIONS_ENDPOINT,
            "scope_id": self.scope.scope_id,
            "offset": self.offset,
            "per_page": self.per_page,
            "provider_total": self.provider_total,
            "observed_at_utc": self.observed_at_utc,
            "request_semantics_sha256": self.request_semantics_sha256,
            "raw_response_sha256": self.raw_response_sha256,
            "rows": [row.to_payload() for row in self.rows],
        }

    @property
    def page_evidence_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookPositionTraversal:
    """Complete, internally coherent offset walk for one fixed positions scope.

    Completeness here means only that the observed pages form a contiguous walk whose
    stable provider-reported total is exhausted. It does not prove provider snapshot
    atomicity, authenticated origin, or authoritative negative absence at any later time.
    """

    scope: MatchbookPositionScope
    pages: tuple[MatchbookPositionPage, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookPositionScope:
            raise MatchbookPositionPaginationError(
                "scope must be an exact MatchbookPositionScope"
            )
        if type(self.pages) is not tuple or not self.pages:
            raise MatchbookPositionPaginationError("pages must be a non-empty tuple")

        first = self.pages[0]
        if type(first) is not MatchbookPositionPage:
            raise MatchbookPositionPaginationError(
                "pages must contain exact MatchbookPositionPage values"
            )
        if first.scope != self.scope:
            raise MatchbookPositionPaginationError("page scope does not match traversal scope")
        if first.offset != 0:
            raise MatchbookPositionPaginationError("positions traversal must start at offset 0")

        expected_offset = 0
        expected_total = first.provider_total
        expected_per_page = first.per_page
        previous_observed_at: datetime | None = None
        seen: set[tuple[int, int, int]] = set()

        for index, page in enumerate(self.pages):
            if type(page) is not MatchbookPositionPage:
                raise MatchbookPositionPaginationError(
                    "pages must contain exact MatchbookPositionPage values"
                )
            if page.scope != self.scope:
                raise MatchbookPositionPaginationError(
                    "page scope does not match traversal scope"
                )
            if page.offset != expected_offset:
                raise MatchbookPositionPaginationError(
                    f"pagination is not contiguous at page index {index}"
                )
            if page.provider_total != expected_total:
                raise MatchbookPositionPaginationError(
                    "provider_total changed during one positions traversal"
                )
            if page.per_page != expected_per_page:
                raise MatchbookPositionPaginationError(
                    "per_page changed during one positions traversal"
                )

            observed_at = _instant(page.observed_at_utc, "page.observed_at_utc")
            if previous_observed_at is not None and observed_at < previous_observed_at:
                raise MatchbookPositionPaginationError(
                    "page observation time rolled backward"
                )
            previous_observed_at = observed_at

            for row in page.rows:
                if row.native_identity in seen:
                    raise MatchbookPositionPaginationError(
                        "provider runner position identity repeats across pages"
                    )
                seen.add(row.native_identity)

            end = page.offset + len(page.rows)
            is_last = index == len(self.pages) - 1
            if not is_last:
                if len(page.rows) != page.per_page:
                    raise MatchbookPositionPaginationError(
                        "a non-final positions page must be full"
                    )
                if end >= expected_total:
                    raise MatchbookPositionPaginationError(
                        "pagination continued after provider_total was exhausted"
                    )
                expected_offset = page.offset + page.per_page
            else:
                if end != expected_total:
                    raise MatchbookPositionPaginationError(
                        "final positions page does not exhaust provider_total"
                    )

    @property
    def traversal_evidence_id(self) -> str:
        return _digest(
            {
                "schema_version": _SCHEMA_VERSION,
                "endpoint": _POSITIONS_ENDPOINT,
                "scope_id": self.scope.scope_id,
                "pages": [page.page_evidence_id for page in self.pages],
            }
        )

    @property
    def structurally_complete(self) -> bool:
        return True

    @property
    def provider_snapshot_atomicity_proven(self) -> bool:
        return False

    @property
    def authoritative_absence_proven(self) -> bool:
        return False

    @property
    def canonical_position_truth_proven(self) -> bool:
        return False

    @property
    def settlement_truth_proven(self) -> bool:
        return False

    @property
    def economic_pnl_truth_proven(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False

    def require_observed_runner(
        self,
        *,
        provider_event_id: int,
        provider_market_id: int,
        provider_runner_id: int,
        provider_row_sha256: str,
    ) -> MatchbookRunnerPositionObservation:
        identity = (
            _positive_int(provider_event_id, "provider_event_id"),
            _positive_int(provider_market_id, "provider_market_id"),
            _positive_int(provider_runner_id, "provider_runner_id"),
        )
        expected_sha = _sha256(provider_row_sha256, "provider_row_sha256")
        for page in self.pages:
            for row in page.rows:
                if row.native_identity == identity:
                    if row.provider_row_sha256 != expected_sha:
                        raise MatchbookPositionEvidenceError(
                            "provider row digest does not match frozen traversal evidence"
                        )
                    return row
        raise MatchbookPositionEvidenceError(
            "provider runner identity is absent from frozen traversal evidence"
        )
