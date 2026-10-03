"""Fail-closed proof of one Betfair account-statement pagination traversal.

The module does not fetch provider data or create another Betfair client. It consumes
only product-issued ''BetfairProviderBillingInputsObservation'' pages from the
canonical provider-billing acquisition authority.

A terminal contiguous traversal is deliberately weaker than a coherent provider
snapshot. The current Betfair statement seam exposes no atomic snapshot version,
stable cross-session account identifier, or irreversible temporal-finality witness.
Accordingly this module never upgrades pagination evidence into complete provider
cost scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from weakref import ReferenceType, ref

from .betfair_account_readonly import BetfairReadOnlyError
from .betfair_provider_billing_inputs import (
    BetfairAccountStatementPageObservation,
    BetfairDeveloperAppEntitlementObservation,
    BetfairProviderBillingInputsObservation,
)
from .betfair_provider_billing_inputs_authority import (
    validate_betfair_provider_billing_inputs_traversal,
)

_SCHEMA = "autosport.betfair_statement_pagination_completeness"
_SCHEMA_VERSION = 1


class BetfairStatementCompletenessError(BetfairReadOnlyError):
    """Raised when account-statement pagination cannot be proven safely."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairStatementPaginationEvidence:
    venue_id: str
    currency_code: str
    record_count: int
    statement_from: str | None
    statement_to: str | None
    query_fingerprint_sha256: str
    page_count: int
    row_count: int
    page_offsets: tuple[int, ...]
    page_evidence_sha256s: tuple[str, ...]
    ordered_rows_sha256: str
    first_observed_at: str
    last_observed_at: str
    pagination_complete: bool
    provider_origin_verified: bool
    same_authenticated_session_proven: bool
    acquisition_owned_traversal_proven: bool
    coherent_snapshot_proven: bool
    stable_account_identity_proven: bool
    temporal_finality_attested: bool
    temporal_finality_evidence_sha256: str | None
    cost_scope_complete: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _currency(self.currency_code)
        _positive_int(self.record_count, "record_count")
        if self.record_count > 100:
            raise BetfairStatementCompletenessError(
                "record_count exceeds Betfair statement page limit"
            )
        start = _optional_timestamp(self.statement_from, "statement_from")
        end = _optional_timestamp(self.statement_to, "statement_to")
        if start is not None and end is not None and start > end:
            raise BetfairStatementCompletenessError(
                "statement_from must not be after statement_to"
            )
        _sha(self.query_fingerprint_sha256, "query_fingerprint_sha256")
        _positive_int(self.page_count, "page_count")
        _nonnegative_int(self.row_count, "row_count")
        if (
            type(self.page_offsets) is not tuple
            or len(self.page_offsets) != self.page_count
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in self.page_offsets
            )
        ):
            raise BetfairStatementCompletenessError(
                "page_offsets must contain one non-negative offset per page"
            )
        if not self.page_offsets or self.page_offsets[0] != 0:
            raise BetfairStatementCompletenessError(
                "statement pagination evidence must start at offset zero"
            )
        if (
            type(self.page_evidence_sha256s) is not tuple
            or len(self.page_evidence_sha256s) != self.page_count
        ):
            raise BetfairStatementCompletenessError(
                "page evidence count does not match page_count"
            )
        for digest in self.page_evidence_sha256s:
            _sha(digest, "page_evidence_sha256")
        _sha(self.ordered_rows_sha256, "ordered_rows_sha256")
        if _timestamp(self.last_observed_at, "last_observed_at") < _timestamp(
            self.first_observed_at, "first_observed_at"
        ):
            raise BetfairStatementCompletenessError(
                "last_observed_at cannot precede first_observed_at"
            )
        for value, field in (
            (self.pagination_complete, "pagination_complete"),
            (self.provider_origin_verified, "provider_origin_verified"),
            (
                self.same_authenticated_session_proven,
                "same_authenticated_session_proven",
            ),
            (
                self.acquisition_owned_traversal_proven,
                "acquisition_owned_traversal_proven",
            ),
            (self.coherent_snapshot_proven, "coherent_snapshot_proven"),
            (self.stable_account_identity_proven, "stable_account_identity_proven"),
            (self.temporal_finality_attested, "temporal_finality_attested"),
            (self.cost_scope_complete, "cost_scope_complete"),
        ):
            if type(value) is not bool:
                raise BetfairStatementCompletenessError(f"{field} must be bool")
        if not self.pagination_complete or not self.provider_origin_verified:
            raise BetfairStatementCompletenessError(
                "issued evidence requires verified terminal pagination"
            )
        if not self.same_authenticated_session_proven:
            raise BetfairStatementCompletenessError(
                "issued evidence requires one authenticated session traversal"
            )
        if not self.acquisition_owned_traversal_proven:
            raise BetfairStatementCompletenessError(
                "issued evidence requires one product-owned pagination acquisition"
            )
        if self.coherent_snapshot_proven:
            raise BetfairStatementCompletenessError(
                "account-statement atomic snapshot coherence is not proven"
            )
        if self.stable_account_identity_proven:
            raise BetfairStatementCompletenessError(
                "stable cross-session Betfair account identity is not proven"
            )
        if self.temporal_finality_attested:
            raise BetfairStatementCompletenessError(
                "statement temporal finality requires a separate canonical authority"
            )
        if self.temporal_finality_evidence_sha256 is not None:
            raise BetfairStatementCompletenessError(
                "no temporal-finality evidence is available on this authority"
            )
        if self.cost_scope_complete:
            raise BetfairStatementCompletenessError(
                "pagination completeness alone cannot complete provider cost scope"
            )
        _sha(self.evidence_sha256, "evidence_sha256")
        if self.evidence_sha256 != _canonical_sha256(_payload(self)):
            raise BetfairStatementCompletenessError(
                "statement pagination evidence digest mismatch"
            )


@dataclass(frozen=True, slots=True)
class _IssuedRecord:
    value_ref: ReferenceType[BetfairStatementPaginationEvidence]
    digest: str


def _build_authority():
    traversal_validator = validate_betfair_provider_billing_inputs_traversal
    source_cls = BetfairProviderBillingInputsObservation
    evidence_cls = BetfairStatementPaginationEvidence
    issued: dict[int, _IssuedRecord] = {}

    def resolve(
        pages: tuple[BetfairProviderBillingInputsObservation, ...],
    ) -> BetfairStatementPaginationEvidence:
        if type(pages) is not tuple or not pages:
            raise BetfairStatementCompletenessError(
                "pages must be a non-empty exact tuple"
            )
        for page in pages:
            if type(page) is not source_cls:
                raise BetfairStatementCompletenessError(
                    "pages must contain exact canonical provider-billing observations"
                )
        try:
            accepted_pages = traversal_validator(pages)
        except (BetfairReadOnlyError, TypeError, ValueError) as exc:
            raise BetfairStatementCompletenessError(
                "statement pages lack one canonical authenticated-session traversal"
            ) from exc
        if accepted_pages is not pages:
            raise BetfairStatementCompletenessError(
                "statement traversal validator returned a different tuple"
            )
        evidence = _derive_from_verified_pages(pages)
        identity = id(evidence)

        def discard(
            dead_ref: ReferenceType[BetfairStatementPaginationEvidence],
        ) -> None:
            current = issued.get(identity)
            if current is not None and current.value_ref is dead_ref:
                issued.pop(identity, None)

        value_ref = ref(evidence, discard)
        issued[identity] = _IssuedRecord(value_ref, evidence.evidence_sha256)
        return evidence

    def validate(value: object) -> BetfairStatementPaginationEvidence:
        if type(value) is not evidence_cls:
            raise BetfairStatementCompletenessError(
                "statement pagination evidence must be exact canonical type"
            )
        value.__post_init__()
        record = issued.get(id(value))
        if (
            record is None
            or record.value_ref() is not value
            or record.digest != value.evidence_sha256
        ):
            raise BetfairStatementCompletenessError(
                "statement pagination evidence must be product-issued"
            )
        return value

    return resolve, validate


(
    resolve_betfair_statement_pagination_completeness,
    validate_betfair_statement_pagination_evidence,
) = _build_authority()


def _derive_from_verified_pages(
    pages: tuple[BetfairProviderBillingInputsObservation, ...],
) -> BetfairStatementPaginationEvidence:
    """Structural derivation used only after canonical provider-origin validation."""

    if type(pages) is not tuple or not pages:
        raise BetfairStatementCompletenessError(
            "verified pages must be a non-empty exact tuple"
        )
    first = pages[0]
    _components(first)
    first_page = first.statement
    if first_page.from_record != 0:
        raise BetfairStatementCompletenessError(
            "first statement page must use from_record=0"
        )
    fingerprint = _query_fingerprint(first)
    expected_offset = 0
    previous_observed: datetime | None = None
    offsets: list[int] = []
    page_hashes: list[str] = []
    rows: list[dict[str, object]] = []
    previous_item_date: datetime | None = None
    chronology_direction = 0

    for index, source in enumerate(pages):
        _components(source)
        page = source.statement
        if _query_fingerprint(source) != fingerprint:
            raise BetfairStatementCompletenessError(
                "statement pagination query/provider scope changed across pages"
            )
        if page.from_record != expected_offset:
            raise BetfairStatementCompletenessError(
                "statement pagination offset is not contiguous"
            )
        observed = _timestamp(
            page.statement_evidence.observed_at,
            "statement page observed_at",
        )
        if previous_observed is not None and observed < previous_observed:
            raise BetfairStatementCompletenessError(
                "statement page observation time moved backwards"
            )
        previous_observed = observed
        count = len(page.items)
        if page.more_available and count == 0:
            raise BetfairStatementCompletenessError(
                "more_available statement page made no pagination progress"
            )
        is_last = index == len(pages) - 1
        if is_last and page.more_available:
            raise BetfairStatementCompletenessError(
                "statement pagination ended before provider terminal page"
            )
        if not is_last and not page.more_available:
            raise BetfairStatementCompletenessError(
                "pages were supplied after a provider terminal statement page"
            )
        offsets.append(page.from_record)
        page_hashes.append(source.evidence_sha256)
        for row in page.items:
            item_date = _timestamp(row.item_date, "statement item_date")
            if previous_item_date is not None:
                if item_date > previous_item_date:
                    step = 1
                elif item_date < previous_item_date:
                    step = -1
                else:
                    step = 0
                if step:
                    if chronology_direction == 0:
                        chronology_direction = step
                    elif step != chronology_direction:
                        raise BetfairStatementCompletenessError(
                            "statement items are not chronologically ordered"
                        )
            previous_item_date = item_date
        rows.extend(
            {
                "ref_id": row.ref_id,
                "item_date": row.item_date,
                "amount": str(row.amount),
                "balance": str(row.balance),
                "item_class": row.item_class,
                "item_class_data_sha256": row.item_class_data_sha256,
            }
            for row in page.items
        )
        expected_offset += count

    values: dict[str, object] = {
        "venue_id": first_page.venue_id,
        "currency_code": first_page.currency_code,
        "record_count": first_page.record_count,
        "statement_from": first_page.statement_from,
        "statement_to": first_page.statement_to,
        "query_fingerprint_sha256": fingerprint,
        "page_count": len(pages),
        "row_count": len(rows),
        "page_offsets": tuple(offsets),
        "page_evidence_sha256s": tuple(page_hashes),
        "ordered_rows_sha256": _canonical_sha256(rows),
        "first_observed_at": pages[0].statement.statement_evidence.observed_at,
        "last_observed_at": pages[-1].statement.statement_evidence.observed_at,
        "pagination_complete": True,
        "provider_origin_verified": True,
        "same_authenticated_session_proven": True,
        "acquisition_owned_traversal_proven": True,
        "coherent_snapshot_proven": False,
        "stable_account_identity_proven": False,
        "temporal_finality_attested": False,
        "temporal_finality_evidence_sha256": None,
        "cost_scope_complete": False,
    }
    return BetfairStatementPaginationEvidence(
        **values,
        evidence_sha256=_canonical_sha256(_payload_values(values)),
    )


def _components(source: object) -> None:
    if type(source) is not BetfairProviderBillingInputsObservation:
        raise BetfairStatementCompletenessError(
            "verified page has unexpected source type"
        )
    if type(source.statement) is not BetfairAccountStatementPageObservation:
        raise BetfairStatementCompletenessError(
            "statement page must be exact canonical type"
        )
    if type(source.entitlement) is not BetfairDeveloperAppEntitlementObservation:
        raise BetfairStatementCompletenessError(
            "statement entitlement must be exact canonical type"
        )


def _query_fingerprint(source: BetfairProviderBillingInputsObservation) -> str:
    page = source.statement
    entitlement = source.entitlement
    return _canonical_sha256(
        {
            "schema": "autosport.betfair_statement_query_scope",
            "schema_version": 1,
            "venue_id": page.venue_id,
            "currency_code": page.currency_code,
            "record_count": page.record_count,
            "statement_from": page.statement_from,
            "statement_to": page.statement_to,
            "application_entitlement": {
                "app_id": entitlement.app_id,
                "app_name": entitlement.app_name,
                "version_id": entitlement.version_id,
                "version": entitlement.version,
                "delay_data": entitlement.delay_data,
                "subscription_required": entitlement.subscription_required,
                "owner_managed": entitlement.owner_managed,
                "active": entitlement.active,
                "vendor_id": entitlement.vendor_id,
                "provider_owner": entitlement.provider_owner,
                "source_projection_sha256": entitlement.source_projection_sha256,
            },
        }
    )


def _payload(value: BetfairStatementPaginationEvidence) -> dict[str, object]:
    return _payload_values(
        {
            field: getattr(value, field)
            for field in (
                "venue_id",
                "currency_code",
                "record_count",
                "statement_from",
                "statement_to",
                "query_fingerprint_sha256",
                "page_count",
                "row_count",
                "page_offsets",
                "page_evidence_sha256s",
                "ordered_rows_sha256",
                "first_observed_at",
                "last_observed_at",
                "pagination_complete",
                "provider_origin_verified",
                "same_authenticated_session_proven",
                "acquisition_owned_traversal_proven",
                "coherent_snapshot_proven",
                "stable_account_identity_proven",
                "temporal_finality_attested",
                "temporal_finality_evidence_sha256",
                "cost_scope_complete",
            )
        }
    )


def _payload_values(values: dict[str, object]) -> dict[str, object]:
    return {"schema": _SCHEMA, "schema_version": _SCHEMA_VERSION, **values}


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStatementCompletenessError(
            "statement completeness evidence is not canonical JSON"
        ) from exc
    return sha256(raw).hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BetfairStatementCompletenessError(
            f"{field} must be valid ISO-8601"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairStatementCompletenessError(
            f"{field} must include timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: object, field: str) -> datetime | None:
    return None if value is None else _timestamp(value, field)


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairStatementCompletenessError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _currency(value: object) -> str:
    text = _required_text(value, "currency_code")
    if len(text) != 3 or not text.isascii() or not text.isalpha() or text != text.upper():
        raise BetfairStatementCompletenessError(
            "currency_code must be three-letter uppercase ASCII"
        )
    return text


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BetfairStatementCompletenessError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BetfairStatementCompletenessError(
            f"{field} must be a non-negative integer"
        )
    return value


def _sha(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BetfairStatementCompletenessError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return text
