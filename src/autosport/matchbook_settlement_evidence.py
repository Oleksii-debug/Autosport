from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


_SCHEMA_VERSION = 1
_SETTLED_BETS_ENDPOINT = "/edge/rest/reports/v2/bets/settled"
_HEX = frozenset("0123456789abcdef")


class MatchbookSettlementEvidenceError(ValueError):
    """Fail-closed validation error for Matchbook settled-bet report evidence."""


class MatchbookPaginationEvidenceError(MatchbookSettlementEvidenceError):
    """The observed paginated report cannot prove exhaustion of the requested traversal."""


class MatchbookSettledStatus(str, Enum):
    """Provider-native statuses documented by Matchbook's settled-bet report."""

    WIN = "WIN"
    LOSE = "LOSE"
    PUSH = "PUSH"
    PUSH_WIN = "PUSH_WIN"
    PUSH_LOSE = "PUSH_LOSE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MatchbookSettlementEvidenceError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise MatchbookSettlementEvidenceError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookSettlementEvidenceError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise MatchbookSettlementEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _optional_utc(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _utc(value, name)


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatchbookSettlementEvidenceError(
            f"{name} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MatchbookSettlementEvidenceError(f"{name} must be a positive integer")
    return value


def _ids(values: object, name: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise MatchbookSettlementEvidenceError(f"{name} must be a tuple")
    normalized = tuple(_text(value, f"{name}[]") for value in values)
    if len(set(normalized)) != len(normalized):
        raise MatchbookSettlementEvidenceError(f"{name} must not contain duplicates")
    return tuple(sorted(normalized))


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookSettledReportScope:
    """Detached semantic scope descriptor for a settled-bet traversal.

    This DTO does not prove that a request was sent or authenticated.
    'account_context_id' and 'session_generation_id' are non-secret product
    references; credentials must never be stored in this evidence.
    """

    account_context_id: str
    session_generation_id: str
    after_utc: str | None = None
    before_utc: str | None = None
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
        object.__setattr__(
            self, "after_utc", _optional_utc(self.after_utc, "after_utc")
        )
        object.__setattr__(
            self, "before_utc", _optional_utc(self.before_utc, "before_utc")
        )
        object.__setattr__(self, "sport_ids", _ids(self.sport_ids, "sport_ids"))
        object.__setattr__(self, "event_ids", _ids(self.event_ids, "event_ids"))
        object.__setattr__(self, "market_ids", _ids(self.market_ids, "market_ids"))

        if self.after_utc is not None and self.before_utc is not None:
            if _instant(self.after_utc, "after_utc") > _instant(
                self.before_utc, "before_utc"
            ):
                raise MatchbookSettlementEvidenceError(
                    "after_utc must not be later than before_utc"
                )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _SETTLED_BETS_ENDPOINT,
            "account_context_id": self.account_context_id,
            "session_generation_id": self.session_generation_id,
            "after_utc": self.after_utc,
            "before_utc": self.before_utc,
            "sport_ids": list(self.sport_ids),
            "event_ids": list(self.event_ids),
            "market_ids": list(self.market_ids),
        }

    @property
    def scope_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookSettledBetObservation:
    """Detached representation of one provider-shaped settled-bet row.

    Construction validates shape and semantics only; it does not prove authenticated
    provider origin. The record deliberately carries no derived payout, commission,
    canonical win/loss/void mapping, or execution authority.
    """

    provider_bet_id: str
    provider_sport_id: str
    provider_event_id: str
    provider_market_id: str
    status: MatchbookSettledStatus
    settled_at_utc: str
    provider_row_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "provider_bet_id",
            "provider_sport_id",
            "provider_event_id",
            "provider_market_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        try:
            status = MatchbookSettledStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MatchbookSettlementEvidenceError(
                "status is not a documented Matchbook settled-bet status"
            ) from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "settled_at_utc",
            _utc(self.settled_at_utc, "settled_at_utc"),
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
            "status": self.status.value,
            "settled_at_utc": self.settled_at_utc,
            "provider_row_sha256": self.provider_row_sha256,
        }

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def observation_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MatchbookSettledReportPage:
    """Detached structurally coherent REPORTS page evidence.

    'request_semantics_sha256' must hash only non-secret HTTP method/path/query
    semantics. Authorization/session headers and credential material are excluded.
    The digest fields are caller-supplied until separately bound to authenticated
    transport authority.
    """

    scope: MatchbookSettledReportScope
    offset: int
    per_page: int
    observed_at_utc: str
    request_semantics_sha256: str
    raw_response_sha256: str
    rows: tuple[MatchbookSettledBetObservation, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookSettledReportScope:
            raise MatchbookSettlementEvidenceError(
                "scope must be an exact MatchbookSettledReportScope"
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
        if type(self.rows) is not tuple:
            raise MatchbookSettlementEvidenceError("rows must be a tuple")
        if len(self.rows) > self.per_page:
            raise MatchbookSettlementEvidenceError(
                "settled-bet page contains more rows than per_page"
            )

        seen: set[str] = set()
        observed_at = _instant(self.observed_at_utc, "observed_at_utc")
        after = (
            None
            if self.scope.after_utc is None
            else _instant(self.scope.after_utc, "scope.after_utc")
        )
        before = (
            None
            if self.scope.before_utc is None
            else _instant(self.scope.before_utc, "scope.before_utc")
        )
        for row in self.rows:
            if type(row) is not MatchbookSettledBetObservation:
                raise MatchbookSettlementEvidenceError(
                    "rows must contain exact MatchbookSettledBetObservation values"
                )
            if row.provider_bet_id in seen:
                raise MatchbookSettlementEvidenceError(
                    "provider bet identity is duplicated within one report page"
                )
            seen.add(row.provider_bet_id)

            settled_at = _instant(row.settled_at_utc, "row.settled_at_utc")
            if settled_at > observed_at:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row is future evidence relative to page observation"
                )
            if after is not None and settled_at <= after:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row is not strictly after requested after_utc scope"
                )
            if before is not None and settled_at >= before:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row is not strictly before requested before_utc scope"
                )
            if self.scope.sport_ids and row.provider_sport_id not in self.scope.sport_ids:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row sport is outside requested scope"
                )
            if self.scope.event_ids and row.provider_event_id not in self.scope.event_ids:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row event is outside requested scope"
                )
            if self.scope.market_ids and row.provider_market_id not in self.scope.market_ids:
                raise MatchbookSettlementEvidenceError(
                    "settled-bet row market is outside requested scope"
                )

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        return False

    @property
    def product_issued_request_semantics_proven(self) -> bool:
        return False

    @property
    def terminal_by_short_page(self) -> bool:
        """Whether this page is a pagination-exhaustion witness for this DTO only."""

        return len(self.rows) < self.per_page

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _SETTLED_BETS_ENDPOINT,
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
class MatchbookSettledReportTraversal:
    """A fail-closed ordered detached traversal for one settled-report scope.

    Structural pagination exhaustion is intentionally weaker than authenticated
    provider origin, provider snapshot atomicity, or authoritative absence. Even a
    complete detached traversal must never be upgraded to universal settlement/account
    history, commission truth, or net-P&L truth.
    """

    scope: MatchbookSettledReportScope
    pages: tuple[MatchbookSettledReportPage, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not MatchbookSettledReportScope:
            raise MatchbookSettlementEvidenceError(
                "scope must be an exact MatchbookSettledReportScope"
            )
        if type(self.pages) is not tuple or not self.pages:
            raise MatchbookPaginationEvidenceError(
                "pages must contain at least one exact report page"
            )

        expected_offset = 0
        per_page: int | None = None
        previous_observed_at: datetime | None = None
        seen_bets: set[str] = set()

        for index, page in enumerate(self.pages):
            if type(page) is not MatchbookSettledReportPage:
                raise MatchbookPaginationEvidenceError(
                    "pages must contain exact MatchbookSettledReportPage values"
                )
            if page.scope != self.scope:
                raise MatchbookPaginationEvidenceError(
                    "report page scope differs within traversal"
                )
            if page.offset != expected_offset:
                raise MatchbookPaginationEvidenceError(
                    "report pagination has a gap, overlap, or nonzero initial offset"
                )
            if per_page is None:
                per_page = page.per_page
            elif page.per_page != per_page:
                raise MatchbookPaginationEvidenceError(
                    "per_page changes within settled-bet traversal"
                )

            observed_at = _instant(page.observed_at_utc, "page.observed_at_utc")
            if previous_observed_at is not None and observed_at < previous_observed_at:
                raise MatchbookPaginationEvidenceError(
                    "report page observation time moves backwards"
                )
            previous_observed_at = observed_at

            for row in page.rows:
                if row.provider_bet_id in seen_bets:
                    raise MatchbookPaginationEvidenceError(
                        "provider bet identity repeats across report pages; "
                        "snapshot stability is unresolved"
                    )
                seen_bets.add(row.provider_bet_id)

            if page.terminal_by_short_page and index != len(self.pages) - 1:
                raise MatchbookPaginationEvidenceError(
                    "a short terminal page cannot be followed by another page"
                )
            expected_offset = page.offset + page.per_page

    @property
    def pagination_exhausted(self) -> bool:
        return self.pages[-1].terminal_by_short_page

    @property
    def authenticated_provider_origin_proven(self) -> bool:
        """Detached DTO construction cannot prove an authenticated Matchbook read."""

        return False

    @property
    def product_issued_request_semantics_proven(self) -> bool:
        """Caller-supplied request digests are not product-issued transport evidence."""

        return False

    @property
    def provider_snapshot_atomicity_proven(self) -> bool:
        return False

    @property
    def authoritative_absence_proven(self) -> bool:
        return False

    @property
    def commission_truth_proven(self) -> bool:
        return False

    @property
    def net_pnl_truth_proven(self) -> bool:
        return False

    def require_authenticated_provider_origin(self) -> None:
        """Fail closed until authenticated transport authority binds this traversal."""

        raise MatchbookSettlementEvidenceError(
            "detached settled-report evidence does not prove authenticated provider origin"
        )

    def require_pagination_exhausted(self) -> None:
        if not self.pagination_exhausted:
            raise MatchbookPaginationEvidenceError(
                "settled-bet traversal lacks a short final page and is not exhausted"
            )

    def positive_observations(self) -> tuple[MatchbookSettledBetObservation, ...]:
        return tuple(row for page in self.pages for row in page.rows)

    def require_observed_bet(
        self,
        *,
        provider_bet_id: str,
        expected_provider_row_sha256: str,
    ) -> MatchbookSettledBetObservation:
        """Re-resolve a row inside this detached traversal only.

        This protects against rebinding within the supplied evidence graph. It does
        not prove that the graph came from an authenticated Matchbook response.
        Consumers requiring provider-origin authority must separately satisfy the
        authenticated-provider-origin boundary instead of treating caller-supplied
        digests as authority.
        """

        bet_id = _text(provider_bet_id, "provider_bet_id")
        row_sha256 = _sha256(
            expected_provider_row_sha256,
            "expected_provider_row_sha256",
        )
        for row in self.positive_observations():
            if row.provider_bet_id == bet_id:
                if row.provider_row_sha256 != row_sha256:
                    raise MatchbookSettlementEvidenceError(
                        "settled-bet row identity does not match expected evidence"
                    )
                return row
        raise MatchbookSettlementEvidenceError(
            "provider bet identity is absent from positive settled-report evidence"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "endpoint": _SETTLED_BETS_ENDPOINT,
            "scope_id": self.scope.scope_id,
            "page_evidence_ids": [page.page_evidence_id for page in self.pages],
            "pagination_exhausted": self.pagination_exhausted,
            "authenticated_provider_origin_proven": False,
            "product_issued_request_semantics_proven": False,
            "provider_snapshot_atomicity_proven": False,
            "authoritative_absence_proven": False,
            "commission_truth_proven": False,
            "net_pnl_truth_proven": False,
        }

    @property
    def traversal_evidence_id(self) -> str:
        return _digest(self.to_payload())
