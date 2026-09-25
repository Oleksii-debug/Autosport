"""Structural append-only provider settlement revision projection.

This module does not perform provider I/O, settle PaperBook tickets, allocate commission,
or mint provider authority. It composes already-issued exact
BookmakerPositionObservation SETTLED evidence into one linear correction chain so
callers can distinguish what was causally knowable at a historical cutoff from the
latest restated provider observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json

from .bookmaker_capability import BookmakerPositionObservation, BookmakerPositionState


class ProviderSettlementRevisionError(ValueError):
    """Raised when a provider settlement correction chain is ambiguous or invalid."""


_MAX_SETTLEMENT_DECIMAL_COEFFICIENT_DIGITS = 4096
_MAX_SETTLEMENT_DECIMAL_ABS_EXPONENT = 4096
_MAX_SETTLEMENT_DECIMAL_TEXT_LENGTH = 8192


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderSettlementRevisionError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _instant(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSettlementRevisionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderSettlementRevisionError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ProviderSettlementRevisionError(
            "settlement decimal fields must be finite exact Decimal values"
        )

    parts = value.as_tuple()
    exponent = parts.exponent
    raw_digits = parts.digits
    if type(exponent) is not int:
        raise ProviderSettlementRevisionError(
            "settlement decimal exponent must be an integer"
        )
    if len(raw_digits) > _MAX_SETTLEMENT_DECIMAL_COEFFICIENT_DIGITS:
        raise ProviderSettlementRevisionError(
            "settlement decimal coefficient exceeds resource limit"
        )
    if abs(exponent) > _MAX_SETTLEMENT_DECIMAL_ABS_EXPONENT:
        raise ProviderSettlementRevisionError(
            "settlement decimal exponent exceeds resource limit"
        )

    # Validate shape before the zero shortcut: Decimal('0e1000000') must not use
    # its numeric zero value to smuggle attacker-sized representation metadata.
    sign = parts.sign
    digits = list(raw_digits)
    if not any(digits):
        return "0"

    while digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient_length = len(digits)
    if exponent >= 0:
        rendered_length = coefficient_length + exponent
    else:
        point = coefficient_length + exponent
        rendered_length = (
            coefficient_length + 1
            if point > 0
            else 2 + (-point) + coefficient_length
        )
    if sign:
        rendered_length += 1
    if rendered_length > _MAX_SETTLEMENT_DECIMAL_TEXT_LENGTH:
        raise ProviderSettlementRevisionError(
            "settlement decimal canonical text exceeds resource limit"
        )

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            text = f"0.{('0' * -point)}{coefficient}"
    return f"-{text}" if sign else text


def _observation_payload(value: BookmakerPositionObservation) -> dict[str, object]:
    if type(value) is not BookmakerPositionObservation:
        raise ProviderSettlementRevisionError(
            "settlement must be an exact BookmakerPositionObservation"
        )
    if value.state is not BookmakerPositionState.SETTLED:
        raise ProviderSettlementRevisionError(
            "settlement revision requires terminal SETTLED provider evidence"
        )
    return {
        "venue_id": value.venue_id,
        "account_id": value.account_id,
        "adapter_id": value.adapter_id,
        "observation_id": value.observation_id,
        "external_position_id": value.external_position_id,
        "state": value.state.value,
        "currency": value.currency,
        "observed_at": value.observed_at,
        "source_payload_sha256": value.source_payload_sha256,
        "provider_amount": _decimal_text(value.provider_amount),
        "provider_amount_semantics": value.provider_amount_semantics,
        "provider_side": value.provider_side,
        "decimal_odds": _decimal_text(value.decimal_odds),
        "gross_return": _decimal_text(value.gross_return),
        "external_receipt_id": value.external_receipt_id,
    }


@dataclass(frozen=True, slots=True)
class ProviderSettlementRevision:
    """One immutable structural revision around canonical provider settlement evidence.

    supersedes_revision_id is the predecessor revision fingerprint, not merely the
    provider observation id. This keeps correction lineage bound to exact prior content
    while provider-issued observation identity remains visible inside settlement.
    """

    settlement: BookmakerPositionObservation
    available_at: str
    source_ref: str
    supersedes_revision_id: str | None = None

    def __post_init__(self) -> None:
        payload = _observation_payload(self.settlement)
        _text(self.source_ref, "source_ref")
        available = _instant(self.available_at, "available_at")
        observed = _instant(self.settlement.observed_at, "settlement.observed_at")
        if available < observed:
            raise ProviderSettlementRevisionError(
                "settlement revision cannot be available before it was observed"
            )
        predecessor = self.supersedes_revision_id
        if predecessor is not None:
            _text(predecessor, "supersedes_revision_id")
            if len(predecessor) != 64 or any(
                character not in "0123456789abcdef" for character in predecessor
            ):
                raise ProviderSettlementRevisionError(
                    "supersedes_revision_id must be a lowercase SHA-256 digest"
                )
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def revision_id(self) -> str:
        payload = {
            "available_at": _instant(
                self.available_at, "available_at"
            ).isoformat(),
            "settlement": _observation_payload(self.settlement),
            "source_ref": self.source_ref,
            "supersedes_revision_id": self.supersedes_revision_id,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def provider_observation_id(self) -> str:
        return self.settlement.observation_id


@dataclass(frozen=True, slots=True)
class ProviderSettlementRevisionChain:
    """Validated linear revision history for one provider position."""

    revisions: tuple[ProviderSettlementRevision, ...]

    def __post_init__(self) -> None:
        if type(self.revisions) is not tuple or not self.revisions:
            raise ProviderSettlementRevisionError(
                "revisions must be a non-empty tuple"
            )

        unique: list[ProviderSettlementRevision] = []
        by_revision_id: dict[str, ProviderSettlementRevision] = {}
        observation_payloads: dict[str, dict[str, object]] = {}
        for revision in self.revisions:
            if type(revision) is not ProviderSettlementRevision:
                raise ProviderSettlementRevisionError(
                    "revisions must contain exact ProviderSettlementRevision values"
                )
            revision_id = revision.revision_id
            existing_revision = by_revision_id.get(revision_id)
            if existing_revision is not None:
                if existing_revision != revision:
                    raise ProviderSettlementRevisionError(
                        "revision_id conflicts with prior revision content"
                    )
                continue
            by_revision_id[revision_id] = revision

            observation = _observation_payload(revision.settlement)
            observation_id = revision.settlement.observation_id
            existing_observation = observation_payloads.get(observation_id)
            if existing_observation is not None:
                if existing_observation != observation:
                    raise ProviderSettlementRevisionError(
                        "provider observation_id was reused with conflicting content"
                    )
                raise ProviderSettlementRevisionError(
                    "provider observation_id cannot create a distinct settlement revision"
                )
            observation_payloads[observation_id] = observation
            unique.append(revision)

        if not unique:
            raise ProviderSettlementRevisionError(
                "revision chain cannot be empty after idempotent deduplication"
            )

        first = unique[0]
        if first.supersedes_revision_id is not None:
            raise ProviderSettlementRevisionError(
                "first settlement revision cannot supersede an unseen predecessor"
            )

        identity = self._identity(first)
        previous = first
        previous_available = _instant(first.available_at, "available_at")
        previous_observed = _instant(
            first.settlement.observed_at, "settlement.observed_at"
        )

        for current in unique[1:]:
            if self._identity(current) != identity:
                raise ProviderSettlementRevisionError(
                    "settlement correction cannot rewrite provider/account/adapter/"
                    "position/currency identity or executed position terms"
                )
            if current.supersedes_revision_id != previous.revision_id:
                raise ProviderSettlementRevisionError(
                    "settlement correction predecessor is missing, forked, or out of order"
                )
            current_available = _instant(current.available_at, "available_at")
            current_observed = _instant(
                current.settlement.observed_at, "settlement.observed_at"
            )
            if current_available <= previous_available:
                raise ProviderSettlementRevisionError(
                    "distinct settlement revisions require strictly increasing "
                    "causal availability"
                )
            if current_observed < previous_observed:
                raise ProviderSettlementRevisionError(
                    "settlement correction observation time cannot move backward"
                )
            previous = current
            previous_available = current_available
            previous_observed = current_observed

        object.__setattr__(self, "revisions", tuple(unique))

    @staticmethod
    def _identity(
        revision: ProviderSettlementRevision,
    ) -> tuple[
        str,
        str,
        str,
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
    ]:
        settlement = revision.settlement
        return (
            settlement.venue_id,
            settlement.account_id,
            settlement.adapter_id,
            settlement.external_position_id,
            settlement.currency,
            _decimal_text(settlement.provider_amount),
            settlement.provider_amount_semantics,
            settlement.provider_side,
            _decimal_text(settlement.decimal_odds),
        )

    @property
    def current_restated(self) -> ProviderSettlementRevision:
        """Latest structural revision currently visible to this chain."""

        return self.revisions[-1]

    def as_known_at(self, cutoff: str) -> ProviderSettlementRevision | None:
        """Latest revision causally available at cutoff; never backfills later truth."""

        instant = _instant(cutoff, "cutoff")
        visible = [
            revision
            for revision in self.revisions
            if _instant(revision.available_at, "available_at") <= instant
        ]
        return visible[-1] if visible else None

    @property
    def chain_id(self) -> str:
        payload = {
            "identity": list(self._identity(self.revisions[0])),
            "revision_ids": [revision.revision_id for revision in self.revisions],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def build_provider_settlement_revision_chain(
    revisions: tuple[ProviderSettlementRevision, ...],
) -> ProviderSettlementRevisionChain:
    """Build and validate one provider settlement correction chain.

    This is structural validation only. It does not prove that the supplied canonical
    observations were issued by a live provider adapter. Consumers still need the
    product-owned provider evidence issuer/verifier before treating positive values as
    provider-authoritative money truth.
    """

    return ProviderSettlementRevisionChain(revisions=revisions)
