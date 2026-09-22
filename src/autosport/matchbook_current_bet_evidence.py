from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping


_SCHEMA_VERSION = 1
_CURRENT_BETS_ENDPOINT = "/edge/rest/reports/v2/bets/current"
_INT32_MAX = (1 << 31) - 1
_HEX = frozenset("0123456789abcdef")


class MatchbookCurrentBetEvidenceError(ValueError):
    """Fail-closed validation error for detached Matchbook current-bet evidence."""


class MatchbookCurrentBetPaginationError(MatchbookCurrentBetEvidenceError):
    """The observed offset walk is structurally incomplete or internally inconsistent."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MatchbookCurrentBetEvidenceError(f"{name} must be valid UTF-8") from exc
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookCurrentBetEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > _INT32_MAX:
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be a non-negative signed int32"
        )
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0 or value > _INT32_MAX:
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be a positive signed int32"
        )
    return value


def _ids(values: object, name: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise MatchbookCurrentBetEvidenceError(f"{name} must be a tuple")
    normalized = tuple(_text(value, f"{name}[]") for value in values)
    if len(set(normalized)) != len(normalized):
        raise MatchbookCurrentBetEvidenceError(f"{name} must not contain duplicates")
    return tuple(sorted(normalized))


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise MatchbookCurrentBetEvidenceError(
            f"{name} must be an exact finite positive Decimal"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    """Return exact compact numeric identity without fixed-point exponent expansion."""

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
        raise MatchbookCurrentBetEvidenceError(
            "evidence payload is not canonical JSON"
        ) from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookCurrentBetReportScope:
    """Detached non-secret scope for one Current Bets offset walk.

    Matchbook's current public request documentation describes after/before
    with settlement wording even though this endpoint is for markets not yet
    settled. This first evidence contract deliberately omits those ambiguous
    temporal filters. It also fixes DECIMAL odds instead of inheriting account or
    regional defaults.

    Construction proves only internal shape. account_context_id and
    session_generation_id are non-secret references, never credentials or a
    session token.
    """

    account_context_id: str
    session_generation_id: str
    sport_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    market_ids: tuple[str, ...] = ()

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
        object.__setattr__(self, "sport_ids", _ids(self.sport_ids, "sport_ids"))
        object.__setattr__(self, "event_ids", _ids(self.event_ids, "event_ids"))
        object.__setattr__(self, "market_ids", _ids(self.market_ids, "market_ids"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _CURRENT_BETS_ENDPOINT,
            "http_method": "GET",
            "odds_type": "DECIMAL",
            "account_context_id": self.account_context_id,
            "session_generation_id": self.session_generation_id,
            "sport_ids": list(self.sport_ids),
            "event_ids": list(self.event_ids),
            "market_ids": list(self.market_ids),
        }

    @property
    def scope_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookCurrentBetObservation:
    """Detached provider-shaped current-bet row.

    This record preserves row semantics needed by later composition while
    deliberately omitting profit/loss or settlement interpretation. It does not
    prove authenticated provider origin, current-state finality, execution effect,
    or economic truth.
    """

    provider_bet_id: str
    provider_sport_id: str
    provider_event_id: str
    provider_market_id: str
    provider_runner_id: str
    provider_side: str
    odds: Decimal
    stake: Decimal
    submitted_at_utc: str
    provider_row_sha256: str
    provider_offer_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "provider_bet_id",
            "provider_sport_id",
            "provider_event_id",
            "provider_market_id",
            "provider_runner_id",
            "provider_side",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "provider_offer_id",
            _optional_text(self.provider_offer_id, "provider_offer_id"),
        )
        object.__setattr__(self, "odds", _decimal(self.odds, "odds"))
        object.__setattr__(self, "stake", _decimal(self.stake, "stake"))
        object.__setattr__(
            self,
            "submitted_at_utc",
            _utc(self.submitted_at_utc, "submitted_at_utc"),
        )
        object.__setattr__(
            self,
            "provider_row_sha256",
            _sha256(self.provider_row_sha256, "provider_row_sha256"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_bet_id": self.provider_bet_id,
            "provider_sport_id": self.provider_sport_id,
            "provider_event_id": self.provider_event_id,
            "provider_market_id": self.provider_market_id,
            "provider_runner_id": self.provider_runner_id,
            "provider_side": self.provider_side,
            "odds": _decimal_text(self.odds),
            "stake": _decimal_text(self.stake),
            "submitted_at_utc": self.submitted_at_utc,
            "provider_row_sha256": self.provider_row_sha256,
            "provider_offer_id": self.provider_offer_id,
        }

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def current_state_finality_proven(self) -> bool:
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

    @property
    def observation_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookCurrentBetReportPage:
    """One detached structurally coherent Current Bets page.

    Request and raw-response digests are caller-supplied until a separate
    authenticated transport authority binds them. A short page is only a local
    offset-walk boundary witness; it never proves provider-wide absence or an
    atomic provider snapshot.
    """

    scope: MatchbookCurrentBetReportScope
    offset: int
    per_page: int
    observed_at_utc: str
    request_semantics_sha256: str
    raw_response_sha256: str
    rows: tuple[MatchbookCurrentBetObservation, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookCurrentBetReportScope:
            raise MatchbookCurrentBetEvidenceError(
                "scope must be an exact MatchbookCurrentBetReportScope"
            )
        object.__setattr__(self, "offset", _non_negative_int(self.offset, "offset"))
        object.__setattr__(self, "per_page", _positive_int(self.per_page, "per_page"))
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
            raise MatchbookCurrentBetEvidenceError(
                "rows must be a tuple no longer than per_page"
            )

        observed_at = _instant(self.observed_at_utc, "observed_at_utc")
        seen: set[str] = set()
        for row in self.rows:
            if type(row) is not MatchbookCurrentBetObservation:
                raise MatchbookCurrentBetEvidenceError(
                    "rows must contain exact MatchbookCurrentBetObservation values"
                )
            if row.provider_bet_id in seen:
                raise MatchbookCurrentBetEvidenceError(
                    "provider bet identity is duplicated within one current-bet page"
                )
            seen.add(row.provider_bet_id)
            if _instant(row.submitted_at_utc, "row.submitted_at_utc") > observed_at:
                raise MatchbookCurrentBetEvidenceError(
                    "current-bet row is future evidence relative to page observation"
                )
            if self.scope.sport_ids and row.provider_sport_id not in self.scope.sport_ids:
                raise MatchbookCurrentBetEvidenceError(
                    "current-bet row sport is outside requested scope"
                )
            if self.scope.event_ids and row.provider_event_id not in self.scope.event_ids:
                raise MatchbookCurrentBetEvidenceError(
                    "current-bet row event is outside requested scope"
                )
            if self.scope.market_ids and row.provider_market_id not in self.scope.market_ids:
                raise MatchbookCurrentBetEvidenceError(
                    "current-bet row market is outside requested scope"
                )

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def product_issued_request_semantics_proven(self) -> bool:
        return False

    @property
    def terminal_by_short_page(self) -> bool:
        return len(self.rows) < self.per_page

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _CURRENT_BETS_ENDPOINT,
            "scope_id": self.scope.scope_id,
            "offset": self.offset,
            "per_page": self.per_page,
            "observed_at_utc": self.observed_at_utc,
            "request_semantics_sha256": self.request_semantics_sha256,
            "raw_response_sha256": self.raw_response_sha256,
            "rows": [row.to_payload() for row in self.rows],
        }

    @property
    def page_evidence_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookCurrentBetReportTraversal:
    """Ordered detached offset walk for one fixed Current Bets scope."""

    scope: MatchbookCurrentBetReportScope
    pages: tuple[MatchbookCurrentBetReportPage, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookCurrentBetReportScope:
            raise MatchbookCurrentBetPaginationError(
                "scope must be an exact MatchbookCurrentBetReportScope"
            )
        if type(self.pages) is not tuple or not self.pages:
            raise MatchbookCurrentBetPaginationError(
                "pages must contain at least one exact current-bet page"
            )

        expected_offset = 0
        per_page: int | None = None
        previous_observed_at: datetime | None = None
        seen_bets: set[str] = set()

        for index, page in enumerate(self.pages):
            if type(page) is not MatchbookCurrentBetReportPage:
                raise MatchbookCurrentBetPaginationError(
                    "pages must contain exact MatchbookCurrentBetReportPage values"
                )
            if page.scope != self.scope:
                raise MatchbookCurrentBetPaginationError(
                    "current-bet page scope changes within traversal"
                )
            if page.offset != expected_offset:
                raise MatchbookCurrentBetPaginationError(
                    "current-bet pagination has a gap, overlap, or nonzero initial offset"
                )
            if per_page is None:
                per_page = page.per_page
            elif page.per_page != per_page:
                raise MatchbookCurrentBetPaginationError(
                    "per_page changes within current-bet traversal"
                )

            observed_at = _instant(page.observed_at_utc, "page.observed_at_utc")
            if previous_observed_at is not None and observed_at < previous_observed_at:
                raise MatchbookCurrentBetPaginationError(
                    "current-bet page observation time moves backwards"
                )
            previous_observed_at = observed_at

            for row in page.rows:
                if row.provider_bet_id in seen_bets:
                    raise MatchbookCurrentBetPaginationError(
                        "provider bet identity repeats across current-bet pages; "
                        "offset-view stability is unresolved"
                    )
                seen_bets.add(row.provider_bet_id)

            if page.terminal_by_short_page and index != len(self.pages) - 1:
                raise MatchbookCurrentBetPaginationError(
                    "a short terminal current-bet page cannot be followed by another page"
                )
            expected_offset = page.offset + page.per_page

    @property
    def pagination_walk_closed(self) -> bool:
        return self.pages[-1].terminal_by_short_page

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def product_issued_request_semantics_proven(self) -> bool:
        return False

    @property
    def provider_snapshot_atomicity_proven(self) -> bool:
        return False

    @property
    def authoritative_absence_proven(self) -> bool:
        return False

    @property
    def current_state_finality_proven(self) -> bool:
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

    def require_authenticated_provider_origin(self) -> None:
        raise MatchbookCurrentBetEvidenceError(
            "detached current-bet evidence does not prove authenticated provider origin"
        )

    def require_authoritative_current_state(self) -> None:
        raise MatchbookCurrentBetEvidenceError(
            "offset-paged detached current-bet evidence does not prove one atomic "
            "authoritative provider current state"
        )

    def require_pagination_walk_closed(self) -> None:
        if not self.pagination_walk_closed:
            raise MatchbookCurrentBetPaginationError(
                "current-bet traversal lacks a short final page"
            )

    def positive_observations(self) -> tuple[MatchbookCurrentBetObservation, ...]:
        return tuple(row for page in self.pages for row in page.rows)

    def require_observed_bet(
        self,
        *,
        provider_bet_id: str,
        expected_provider_row_sha256: str,
    ) -> MatchbookCurrentBetObservation:
        """Re-resolve a row inside this detached evidence graph only."""

        bet_id = _text(provider_bet_id, "provider_bet_id")
        row_sha256 = _sha256(
            expected_provider_row_sha256,
            "expected_provider_row_sha256",
        )
        for row in self.positive_observations():
            if row.provider_bet_id == bet_id:
                if row.provider_row_sha256 != row_sha256:
                    raise MatchbookCurrentBetEvidenceError(
                        "current-bet row identity does not match expected evidence"
                    )
                return row
        raise MatchbookCurrentBetEvidenceError(
            "provider bet identity is absent from positive current-bet evidence"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _CURRENT_BETS_ENDPOINT,
            "scope_id": self.scope.scope_id,
            "page_evidence_ids": [page.page_evidence_id for page in self.pages],
            "pagination_walk_closed": self.pagination_walk_closed,
            "authenticated_provider_origin_proven": False,
            "product_issued_request_semantics_proven": False,
            "provider_snapshot_atomicity_proven": False,
            "authoritative_absence_proven": False,
            "current_state_finality_proven": False,
            "settlement_truth_proven": False,
            "economic_pnl_truth_proven": False,
            "execution_authority": False,
        }

    @property
    def traversal_evidence_id(self) -> str:
        return _digest(self.to_payload())
