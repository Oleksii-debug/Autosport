"""Versioned provider-specific settlement-rule evidence.

This module records *which documented rule version* applies to a synthetic settlement
scenario at a position acceptance instant.  It does not settle positions, calculate
money, call a provider, validate credentials, or create settlement-receipt/execution
authority.  Real provider rules must arrive as separately verified source evidence;
callers must not treat the examples/tests as bookmaker facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Iterable


class ProviderSettlementRuleError(ValueError):
    """Raised when rule-version evidence is malformed, ambiguous, or inconsistent."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderSettlementRuleError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProviderSettlementRuleError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderSettlementRuleError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderSettlementRuleError(
            f"{field} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderSettlementRule:
    """One provider-documented treatment for one market/scenario key.

    ``treatment_code`` is intentionally an opaque evidence label.  This layer does
    not define payout arithmetic or claim that any particular provider uses a code.
    """

    market_family: str
    scenario_code: str
    treatment_code: str

    def __post_init__(self) -> None:
        _text(self.market_family, "market_family")
        _text(self.scenario_code, "scenario_code")
        _text(self.treatment_code, "treatment_code")

    @property
    def key(self) -> tuple[str, str]:
        return (self.market_family, self.scenario_code)

    @property
    def rule_id(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "market_family": self.market_family,
            "scenario_code": self.scenario_code,
            "treatment_code": self.treatment_code,
        }


@dataclass(frozen=True, slots=True)
class ProviderSettlementRulebook:
    """Immutable, provenance-bound provider rule version for an effective interval."""

    provider_id: str
    rulebook_version: str
    effective_from: str
    effective_until: str | None
    source_ref: str
    source_payload_sha256: str
    rules: tuple[ProviderSettlementRule, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        _text(self.rulebook_version, "rulebook_version")
        start = _timestamp(self.effective_from, "effective_from")
        if self.effective_until is not None:
            end = _timestamp(self.effective_until, "effective_until")
            if end <= start:
                raise ProviderSettlementRuleError(
                    "effective_until must be later than effective_from"
                )
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if type(self.rules) is not tuple or not self.rules:
            raise ProviderSettlementRuleError("rules must be a non-empty tuple")
        if any(type(rule) is not ProviderSettlementRule for rule in self.rules):
            raise ProviderSettlementRuleError(
                "rules must contain exact ProviderSettlementRule values"
            )
        keys = [rule.key for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ProviderSettlementRuleError(
                "rulebook contains duplicate market/scenario keys"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderSettlementRuleError("schema_version must be exactly 1")

    @property
    def rulebook_id(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "provider_id": self.provider_id,
            "rulebook_version": self.rulebook_version,
            "rules": [
                rule.to_canonical_dict()
                for rule in sorted(self.rules, key=lambda rule: rule.key)
            ],
            "schema_version": self.schema_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
        }

    def applies_at(self, accepted_at: str) -> bool:
        instant = _timestamp(accepted_at, "accepted_at")
        start = _timestamp(self.effective_from, "effective_from")
        if instant < start:
            return False
        if self.effective_until is None:
            return True
        return instant < _timestamp(self.effective_until, "effective_until")

    def rule_for(self, market_family: str, scenario_code: str) -> ProviderSettlementRule:
        key = (
            _text(market_family, "market_family"),
            _text(scenario_code, "scenario_code"),
        )
        matches = [rule for rule in self.rules if rule.key == key]
        if len(matches) != 1:
            raise ProviderSettlementRuleError(
                "rulebook has no exact rule for market/scenario key"
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class ProviderSettlementBinding:
    """Exact historical rule identity selected for one acceptance instant.

    The binding is evidence for rule interpretation only.  It is not a settlement
    receipt, provider acknowledgement, execution record, or financial amount.
    """

    provider_id: str
    rulebook_version: str
    rulebook_id: str
    accepted_at: str
    market_family: str
    scenario_code: str
    treatment_code: str
    rule_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        _text(self.rulebook_version, "rulebook_version")
        _sha256(self.rulebook_id, "rulebook_id")
        _timestamp(self.accepted_at, "accepted_at")
        _text(self.market_family, "market_family")
        _text(self.scenario_code, "scenario_code")
        _text(self.treatment_code, "treatment_code")
        _sha256(self.rule_id, "rule_id")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderSettlementRuleError("schema_version must be exactly 1")

    @property
    def binding_id(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "accepted_at": self.accepted_at,
            "market_family": self.market_family,
            "provider_id": self.provider_id,
            "rule_id": self.rule_id,
            "rulebook_id": self.rulebook_id,
            "rulebook_version": self.rulebook_version,
            "scenario_code": self.scenario_code,
            "schema_version": self.schema_version,
            "treatment_code": self.treatment_code,
        }

    def verify_rulebook(self, rulebook: ProviderSettlementRulebook) -> None:
        if type(rulebook) is not ProviderSettlementRulebook:
            raise ProviderSettlementRuleError(
                "rulebook must be an exact ProviderSettlementRulebook"
            )
        if (
            self.provider_id != rulebook.provider_id
            or self.rulebook_version != rulebook.rulebook_version
            or self.rulebook_id != rulebook.rulebook_id
        ):
            raise ProviderSettlementRuleError(
                "settlement binding does not match exact rulebook identity"
            )
        if not rulebook.applies_at(self.accepted_at):
            raise ProviderSettlementRuleError(
                "settlement binding acceptance instant is outside rulebook interval"
            )
        rule = rulebook.rule_for(self.market_family, self.scenario_code)
        if self.rule_id != rule.rule_id or self.treatment_code != rule.treatment_code:
            raise ProviderSettlementRuleError(
                "settlement binding does not match exact rule identity"
            )


@dataclass(frozen=True, slots=True)
class ProviderSettlementRuleTimeline:
    """Non-overlapping chronological rulebook history for exactly one provider."""

    provider_id: str
    rulebooks: tuple[ProviderSettlementRulebook, ...]

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        if type(self.rulebooks) is not tuple or not self.rulebooks:
            raise ProviderSettlementRuleError("rulebooks must be a non-empty tuple")
        if any(type(book) is not ProviderSettlementRulebook for book in self.rulebooks):
            raise ProviderSettlementRuleError(
                "rulebooks must contain exact ProviderSettlementRulebook values"
            )
        if any(book.provider_id != self.provider_id for book in self.rulebooks):
            raise ProviderSettlementRuleError(
                "all rulebooks must belong to timeline provider_id"
            )
        versions = [book.rulebook_version for book in self.rulebooks]
        if len(versions) != len(set(versions)):
            raise ProviderSettlementRuleError(
                "timeline contains duplicate rulebook_version values"
            )

        starts = [
            _timestamp(book.effective_from, "effective_from")
            for book in self.rulebooks
        ]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            raise ProviderSettlementRuleError(
                "rulebooks must be supplied in strictly increasing effective_from order"
            )
        for previous, current in zip(self.rulebooks, self.rulebooks[1:]):
            if previous.effective_until is None:
                raise ProviderSettlementRuleError(
                    "open-ended rulebook cannot precede another version"
                )
            previous_end = _timestamp(previous.effective_until, "effective_until")
            current_start = _timestamp(current.effective_from, "effective_from")
            if previous_end > current_start:
                raise ProviderSettlementRuleError(
                    "rulebook effective intervals must not overlap"
                )

    def select(self, accepted_at: str) -> ProviderSettlementRulebook:
        _timestamp(accepted_at, "accepted_at")
        matches = [book for book in self.rulebooks if book.applies_at(accepted_at)]
        if len(matches) != 1:
            raise ProviderSettlementRuleError(
                "acceptance instant does not select exactly one rulebook version"
            )
        return matches[0]

    def bind(
        self,
        *,
        accepted_at: str,
        market_family: str,
        scenario_code: str,
    ) -> ProviderSettlementBinding:
        rulebook = self.select(accepted_at)
        rule = rulebook.rule_for(market_family, scenario_code)
        binding = ProviderSettlementBinding(
            provider_id=self.provider_id,
            rulebook_version=rulebook.rulebook_version,
            rulebook_id=rulebook.rulebook_id,
            accepted_at=accepted_at,
            market_family=rule.market_family,
            scenario_code=rule.scenario_code,
            treatment_code=rule.treatment_code,
            rule_id=rule.rule_id,
        )
        binding.verify_rulebook(rulebook)
        return binding


def build_provider_settlement_timeline(
    provider_id: str,
    rulebooks: Iterable[ProviderSettlementRulebook],
) -> ProviderSettlementRuleTimeline:
    """Construct a timeline after ordering exact rulebook evidence by effective time."""

    books = tuple(rulebooks)
    ordered = tuple(
        sorted(
            books,
            key=lambda book: _timestamp(book.effective_from, "effective_from")
            if type(book) is ProviderSettlementRulebook
            else datetime.max.replace(tzinfo=timezone.utc),
        )
    )
    return ProviderSettlementRuleTimeline(
        provider_id=provider_id,
        rulebooks=ordered,
    )
