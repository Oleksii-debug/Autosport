"""Fail-closed structural completeness for Betfair account-statement pages.

The module performs no provider I/O. It consumes existing provider-billing page
observations and proves only that one declared query was paginated contiguously to
``moreAvailable=false``. Pagination completeness is not provider temporal finality,
so this authority mechanically cannot mint complete provider-cost scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from .betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
    BetfairAccountStatementPageObservation,
    BetfairDeveloperAppEntitlementObservation,
    BetfairProviderBillingInputsObservation,
)

_SCHEMA = "autosport.betfair_account_statement_pagination_proof"
_SCHEMA_VERSION = 1
_MISSING = ("TEMPORAL_FINALITY_ATTESTATION",)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BetfairAccountStatementCompletenessError(ValueError):
    """Raised when structural statement completeness cannot be proven."""


def _instant(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairAccountStatementCompletenessError(f"{field} must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairAccountStatementCompletenessError(
            f"{field} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairAccountStatementCompletenessError(
            f"{field} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairAccountStatementCompletenessError(
            "pagination evidence is outside canonical JSON domain"
        ) from exc
    return sha256(raw).hexdigest()


def _query_identity(source: BetfairProviderBillingInputsObservation) -> dict[str, object]:
    entitlement = source.entitlement
    statement = source.statement
    return {
        "venue_id": entitlement.venue_id,
        "provider_owner": entitlement.provider_owner,
        "app_id": entitlement.app_id,
        "app_version_id": entitlement.version_id,
        "entitlement_projection_sha256": entitlement.source_projection_sha256,
        "currency_code": statement.currency_code,
        "statement_from": statement.statement_from,
        "statement_to": statement.statement_to,
        "record_count": statement.record_count,
    }


@dataclass(frozen=True, slots=True)
class BetfairAccountStatementPaginationProof:
    venue_id: str
    provider_owner: str
    app_id: int
    app_version_id: int
    entitlement_projection_sha256: str
    currency_code: str
    statement_from: str | None
    statement_to: str | None
    record_count: int
    page_count: int
    row_count: int
    first_observed_at: str
    last_observed_at: str
    query_fingerprint_sha256: str
    page_evidence_sha256: tuple[str, ...]
    ref_ids_sha256: str
    pagination_complete: bool
    temporal_finality_attested: bool
    cost_scope_complete: bool
    missing_authorities: tuple[str, ...]
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.pagination_complete is not True:
            raise BetfairAccountStatementCompletenessError(
                "pagination_complete must be true"
            )
        if self.temporal_finality_attested is not False:
            raise BetfairAccountStatementCompletenessError(
                "temporal_finality_attested is unavailable in this authority"
            )
        if self.cost_scope_complete is not False:
            raise BetfairAccountStatementCompletenessError(
                "cost_scope_complete requires separate temporal-finality authority"
            )
        if self.missing_authorities != _MISSING:
            raise BetfairAccountStatementCompletenessError(
                "missing_authorities must preserve temporal-finality boundary"
            )
        if (
            isinstance(self.record_count, bool)
            or type(self.record_count) is not int
            or not 1 <= self.record_count <= 100
        ):
            raise BetfairAccountStatementCompletenessError(
                "record_count must be in provider range 1..100"
            )
        if self.page_count <= 0 or self.row_count < 0:
            raise BetfairAccountStatementCompletenessError("invalid page/row count")
        if len(self.page_evidence_sha256) != self.page_count:
            raise BetfairAccountStatementCompletenessError(
                "page evidence count must equal page_count"
            )
        for field, value in (
            ("entitlement_projection_sha256", self.entitlement_projection_sha256),
            ("query_fingerprint_sha256", self.query_fingerprint_sha256),
            ("ref_ids_sha256", self.ref_ids_sha256),
            ("evidence_sha256", self.evidence_sha256),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise BetfairAccountStatementCompletenessError(
                    f"{field} must be lowercase SHA-256"
                )
        if any(type(value) is not str or _SHA256.fullmatch(value) is None for value in self.page_evidence_sha256):
            raise BetfairAccountStatementCompletenessError(
                "page evidence must contain lowercase SHA-256 digests"
            )
        if _instant(self.last_observed_at, "last_observed_at") < _instant(
            self.first_observed_at, "first_observed_at"
        ):
            raise BetfairAccountStatementCompletenessError(
                "last_observed_at must not precede first_observed_at"
            )
        if self.evidence_sha256 != _digest(_proof_payload(self)):
            raise BetfairAccountStatementCompletenessError(
                "pagination proof evidence digest mismatch"
            )


def _proof_payload(proof: BetfairAccountStatementPaginationProof) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "query": {
            "venue_id": proof.venue_id,
            "provider_owner": proof.provider_owner,
            "app_id": proof.app_id,
            "app_version_id": proof.app_version_id,
            "entitlement_projection_sha256": proof.entitlement_projection_sha256,
            "currency_code": proof.currency_code,
            "statement_from": proof.statement_from,
            "statement_to": proof.statement_to,
            "record_count": proof.record_count,
        },
        "page_count": proof.page_count,
        "row_count": proof.row_count,
        "first_observed_at": proof.first_observed_at,
        "last_observed_at": proof.last_observed_at,
        "query_fingerprint_sha256": proof.query_fingerprint_sha256,
        "page_evidence_sha256": list(proof.page_evidence_sha256),
        "ref_ids_sha256": proof.ref_ids_sha256,
        "pagination_complete": proof.pagination_complete,
        "temporal_finality_attested": proof.temporal_finality_attested,
        "cost_scope_complete": proof.cost_scope_complete,
        "missing_authorities": list(proof.missing_authorities),
    }


def prove_betfair_account_statement_pagination_complete(
    pages: tuple[BetfairProviderBillingInputsObservation, ...],
) -> BetfairAccountStatementPaginationProof:
    """Return a proof only for one exact contiguous terminal pagination chain."""

    if type(pages) is not tuple or not pages:
        raise BetfairAccountStatementCompletenessError(
            "pages must be a non-empty exact tuple"
        )
    if any(type(source) is not BetfairProviderBillingInputsObservation for source in pages):
        raise BetfairAccountStatementCompletenessError(
            "pages must contain exact provider-billing observations"
        )

    first = pages[0]
    identity = _query_identity(first)
    fingerprint = _digest(
        {"schema": _SCHEMA, "schema_version": _SCHEMA_VERSION, "query": identity}
    )
    expected_offset = 0
    previous_time: datetime | None = None
    seen_refs: set[str] = set()
    ordered_refs: list[str] = []
    page_digests: list[str] = []

    for index, source in enumerate(pages):
        try:
            BetfairProviderBillingInputsObservation.__post_init__(source)
            BetfairDeveloperAppEntitlementObservation.__post_init__(source.entitlement)
            BetfairAccountStatementPageObservation.__post_init__(source.statement)
        except Exception as exc:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} failed canonical observation validation"
            ) from exc
        if _query_identity(source) != identity:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} changed provider/query scope"
            )
        statement = source.statement
        if statement.from_record != expected_offset:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} from_record is not the exact contiguous offset"
            )
        observed = _instant(source.observed_at, f"pages[{index}].observed_at")
        if previous_time is not None and observed < previous_time:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} observation time moved backwards"
            )
        previous_time = observed
        if statement.more_available and not statement.items:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} claims moreAvailable without pagination progress"
            )
        if index < len(pages) - 1 and statement.more_available is not True:
            raise BetfairAccountStatementCompletenessError(
                f"page {index} is terminal before supplied chain ends"
            )
        if index == len(pages) - 1 and statement.more_available is not False:
            raise BetfairAccountStatementCompletenessError(
                "last page must be provider-terminal with moreAvailable=false"
            )
        for item in statement.items:
            if type(item) is not BetfairAccountStatementItemObservation:
                raise BetfairAccountStatementCompletenessError(
                    f"page {index} contains non-canonical statement row"
                )
            BetfairAccountStatementItemObservation.__post_init__(item)
            if item.ref_id in seen_refs:
                raise BetfairAccountStatementCompletenessError(
                    f"duplicate statement ref_id across pages: {item.ref_id}"
                )
            seen_refs.add(item.ref_id)
            ordered_refs.append(item.ref_id)
        expected_offset += len(statement.items)
        page_digests.append(source.evidence_sha256)

    values = dict(
        **identity,
        page_count=len(pages),
        row_count=len(ordered_refs),
        first_observed_at=pages[0].observed_at,
        last_observed_at=pages[-1].observed_at,
        query_fingerprint_sha256=fingerprint,
        page_evidence_sha256=tuple(page_digests),
        ref_ids_sha2556=_digest({"ref_ids_in_page_order": ordered_refs}),
        pagination_complete=True,
        temporal_finality_attested=False,
        cost_scope_complete=False,
        missing_authorities=_MISSING,
    )
    shell = object.__new__(BetfairAccountStatementPaginationProof)
    for name, value in values.items():
        object.__setattr__(shell, name, value)
    object.__setattr__(shell, "evidence_sha256", _digest(_proof_payload(shell)))
    shell.__post_init__()
    return shell
