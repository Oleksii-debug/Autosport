"""Fail-closed row identity for authenticated provider billing observations.

This module is deliberately narrower than economic allocation.  It can prove which
exact account-statement row was observed under which provider owner/application
observation, but it cannot prove that the row belongs to Autosport, that the whole
provider activity denominator is known, that a cost class applies, or that any
share may be allocated to a live opportunity/campaign.  Those missing authorities
remain explicit in every result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import re

from .betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
    BetfairAccountStatementPageObservation,
    BetfairDeveloperAppEntitlementObservation,
    BetfairProviderBillingInputsObservation,
)


_SCHEMA = "autosport.provider_billing_row_attribution"
_SCHEMA_VERSION = 1
_MISSING_AUTHORITIES = (
    "AUTOSPORT_ACTIVITY_NUMERATOR",
    "PROVIDER_WINDOW_TOTAL_DENOMINATOR",
    "COST_APPLICABILITY",
    "ALLOCATION_RULE",
)


class ProviderBillingAttributionError(ValueError):
    """Raised when provider billing row identity cannot be proven exactly."""


def _build_capability():
    error_cls = ProviderBillingAttributionError
    schema = _SCHEMA
    schema_version = _SCHEMA_VERSION
    source_cls = BetfairProviderBillingInputsObservation
    entitlement_cls = BetfairDeveloperAppEntitlementObservation
    page_cls = BetfairAccountStatementPageObservation
    item_cls = BetfairAccountStatementItemObservation
    source_validate = source_cls.__dict__["__post_init__"]
    entitlement_validate = entitlement_cls.__dict__["__post_init__"]
    page_validate = page_cls.__dict__["__post_init__"]
    item_validate = item_cls.__dict__["__post_init__"]
    dumps = json.dumps
    hash_ctor = sha256
    decimal_cls = Decimal
    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    parse_datetime = datetime.fromisoformat
    missing_authorities = _MISSING_AUTHORITIES

    def required_text(value: object, field: str) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or "\x00" in value
        ):
            raise error_cls(
                f"{field} must be canonical non-empty text"
            )
        return value

    def sha256_hex(value: object, field: str) -> str:
        text = required_text(value, field)
        if sha_pattern.fullmatch(text) is None:
            raise error_cls(
                f"{field} must be lowercase SHA-256"
            )
        return text

    def iso_timestamp(value: object, field: str) -> str:
        text = required_text(value, field)
        try:
            parsed = parse_datetime(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise error_cls(
                f"{field} must be timezone-aware ISO-8601"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise error_cls(
                f"{field} must be timezone-aware ISO-8601"
            )
        return text

    def canonical_sha256(value: object) -> str:
        try:
            payload = dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise error_cls(
                "provider billing attribution payload is not canonical JSON"
            ) from exc
        return hash_ctor(payload).hexdigest()

    def amount_sign(value: Decimal) -> str:
        if type(value) is not decimal_cls or not value.is_finite():
            raise error_cls(
                "statement amount must be a finite Decimal"
            )
        if value < 0:
            return "NEGATIVE"
        if value > 0:
            return "POSITIVE"
        return "ZERO"

    @dataclass(frozen=True, slots=True)
    class ProviderBillingRowAttributionEvidence:
        venue_id: str
        provider_owner: str
        app_id: int
        app_version_id: int
        currency_code: str
        source_observed_at: str
        source_evidence_sha256: str
        statement_request_scope_sha256: str
        statement_source_payload_sha256: str
        statement_more_available: bool
        row_ref_id: str
        row_item_date: str
        row_amount: Decimal
        row_amount_sign: str
        row_item_class: str
        row_item_class_data_sha256: str
        attribution_state: str
        missing_authorities: tuple[str, ...]
        evidence_sha256: str

        def __post_init__(self) -> None:
            required_text(self.venue_id, "venue_id")
            required_text(self.provider_owner, "provider_owner")
            if isinstance(self.app_id, bool) or type(self.app_id) is not int or self.app_id <= 0:
                raise error_cls("app_id must be a positive integer")
            if (
                isinstance(self.app_version_id, bool)
                or type(self.app_version_id) is not int
                or self.app_version_id <= 0
            ):
                raise error_cls(
                    "app_version_id must be a positive integer"
                )
            currency = required_text(self.currency_code, "currency_code")
            if len(currency) != 3 or not currency.isascii() or currency != currency.upper():
                raise error_cls(
                    "currency_code must be canonical three-letter uppercase ASCII"
                )
            iso_timestamp(self.source_observed_at, "source_observed_at")
            sha256_hex(self.source_evidence_sha256, "source_evidence_sha256")
            sha256_hex(
                self.statement_request_scope_sha256,
                "statement_request_scope_sha256",
            )
            sha256_hex(
                self.statement_source_payload_sha256,
                "statement_source_payload_sha256",
            )
            if type(self.statement_more_available) is not bool:
                raise error_cls(
                    "statement_more_available must be bool"
                )
            required_text(self.row_ref_id, "row_ref_id")
            iso_timestamp(self.row_item_date, "row_item_date")
            required_text(self.row_item_class, "row_item_class")
            sha256_hex(
                self.row_item_class_data_sha256,
                "row_item_class_data_sha256",
            )
            if self.row_amount_sign != amount_sign(self.row_amount):
                raise error_cls("row amount sign mismatch")
            if self.attribution_state != "UNPROVEN":
                raise error_cls(
                    "provider billing row cannot mint positive attribution"
                )
            if self.missing_authorities != missing_authorities:
                raise error_cls(
                    "provider billing attribution missing-authority set is invalid"
                )
            sha256_hex(self.evidence_sha256, "evidence_sha256")
            if self.evidence_sha256 != evidence_sha256(self):
                raise error_cls(
                    "provider billing attribution evidence digest mismatch"
                )

    def evidence_payload(
        *,
        venue_id: str,
        provider_owner: str,
        app_id: int,
        app_version_id: int,
        currency_code: str,
        source_observed_at: str,
        source_evidence_sha256: str,
        statement_request_scope_sha256: str,
        statement_source_payload_sha256: str,
        statement_more_available: bool,
        row_ref_id: str,
        row_item_date: str,
        row_amount: Decimal,
        row_amount_sign: str,
        row_item_class: str,
        row_item_class_data_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema": schema,
            "schema_version": schema_version,
            "venue_id": venue_id,
            "provider_owner": provider_owner,
            "app_id": app_id,
            "app_version_id": app_version_id,
            "currency_code": currency_code,
            "source_observed_at": source_observed_at,
            "source_evidence_sha256": source_evidence_sha256,
            "statement_request_scope_sha256": statement_request_scope_sha256,
            "statement_source_payload_sha256": statement_source_payload_sha256,
            "statement_more_available": statement_more_available,
            "row": {
                "ref_id": row_ref_id,
                "item_date": row_item_date,
                "amount": str(row_amount),
                "amount_sign": row_amount_sign,
                "item_class": row_item_class,
                "item_class_data_sha256": row_item_class_data_sha256,
            },
            "attribution_state": "UNPROVEN",
            "missing_authorities": list(missing_authorities),
        }

    def evidence_sha256(value: ProviderBillingRowAttributionEvidence) -> str:
        return canonical_sha256(
            evidence_payload(
                venue_id=value.venue_id,
                provider_owner=value.provider_owner,
                app_id=value.app_id,
                app_version_id=value.app_version_id,
                currency_code=value.currency_code,
                source_observed_at=value.source_observed_at,
                source_evidence_sha256=value.source_evidence_sha256,
                statement_request_scope_sha256=value.statement_request_scope_sha256,
                statement_source_payload_sha256=value.statement_source_payload_sha256,
                statement_more_available=value.statement_more_available,
                row_ref_id=value.row_ref_id,
                row_item_date=value.row_item_date,
                row_amount=value.row_amount,
                row_amount_sign=value.row_amount_sign,
                row_item_class=value.row_item_class,
                row_item_class_data_sha256=value.row_item_class_data_sha256,
            )
        )

    def resolve(
        source: BetfairProviderBillingInputsObservation,
        row_ref_id: str,
    ) -> ProviderBillingRowAttributionEvidence:
        """Resolve one exact observed statement row while keeping allocation UNPROVEN."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        required_text(row_ref_id, "row_ref_id")

        # Re-run the closure-sealed validators so object.__new__/attribute tamper
        # cannot turn transport-shaped data into accepted source evidence.
        if type(source.entitlement) is not entitlement_cls:
            raise error_cls(
                "source entitlement is not exact canonical observation"
            )
        if type(source.statement) is not page_cls:
            raise error_cls(
                "source statement is not exact canonical observation"
            )
        entitlement_validate(source.entitlement)
        for item in source.statement.items:
            if type(item) is not item_cls:
                raise error_cls(
                    "statement row is not exact canonical observation"
                )
            item_validate(item)
        page_validate(source.statement)
        source_validate(source)

        matches = tuple(
            item for item in source.statement.items if item.ref_id == row_ref_id
        )
        if len(matches) != 1:
            raise error_cls(
                "provider billing attribution requires exactly one statement ref_id"
            )
        row = matches[0]
        sign = amount_sign(row.amount)
        payload = evidence_payload(
            venue_id=source.entitlement.venue_id,
            provider_owner=source.entitlement.provider_owner,
            app_id=source.entitlement.app_id,
            app_version_id=source.entitlement.version_id,
            currency_code=source.statement.currency_code,
            source_observed_at=source.observed_at,
            source_evidence_sha256=source.evidence_sha256,
            statement_request_scope_sha256=source.statement.request_scope_sha256,
            statement_source_payload_sha256=(
                source.statement.statement_evidence.source_payload_sha256
            ),
            statement_more_available=source.statement.more_available,
            row_ref_id=row.ref_id,
            row_item_date=row.item_date,
            row_amount=row.amount,
            row_amount_sign=sign,
            row_item_class=row.item_class,
            row_item_class_data_sha256=row.item_class_data_sha256,
        )
        return ProviderBillingRowAttributionEvidence(
            venue_id=source.entitlement.venue_id,
            provider_owner=source.entitlement.provider_owner,
            app_id=source.entitlement.app_id,
            app_version_id=source.entitlement.version_id,
            currency_code=source.statement.currency_code,
            source_observed_at=source.observed_at,
            source_evidence_sha256=source.evidence_sha256,
            statement_request_scope_sha256=source.statement.request_scope_sha256,
            statement_source_payload_sha256=(
                source.statement.statement_evidence.source_payload_sha256
            ),
            statement_more_available=source.statement.more_available,
            row_ref_id=row.ref_id,
            row_item_date=row.item_date,
            row_amount=row.amount,
            row_amount_sign=sign,
            row_item_class=row.item_class,
            row_item_class_data_sha256=row.item_class_data_sha256,
            attribution_state="UNPROVEN",
            missing_authorities=missing_authorities,
            evidence_sha256=canonical_sha256(payload),
        )

    return resolve, ProviderBillingRowAttributionEvidence


(
    resolve_provider_billing_row_attribution,
    ProviderBillingRowAttributionEvidence,
) = _build_capability()
