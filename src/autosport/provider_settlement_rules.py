"""Immutable provider settlement-rule version evidence.

This is provenance/policy evidence only.  It never settles a position or infers a
provider-specific outcome when the exact applicable rule version is not proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json


SETTLEMENT_RULE_SCHEMA_VERSION = 1


class ProviderSettlementRuleError(ValueError):
    pass


class SettlementRuleApplicability(str, Enum):
    APPLICABLE = "applicable"
    NOT_EFFECTIVE = "not_effective"
    VERSION_MISMATCH = "version_mismatch"
    UNKNOWN = "unknown"


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderSettlementRuleError(f"{field} must be a non-empty trimmed string")
    return value


def _time(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSettlementRuleError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderSettlementRuleError(f"{field} must include a timezone offset")
    return parsed


def _sha(value: str, field: str) -> str:
    _text(value, field)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ProviderSettlementRuleError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProviderSettlementRuleVersion:
    venue_id: str
    rule_set_id: str
    rule_version: str
    effective_from: str
    effective_until: str | None
    observed_at: str
    source_ref: str
    source_payload_sha256: str
    schema_version: int = SETTLEMENT_RULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.rule_set_id, "rule_set_id")
        _text(self.rule_version, "rule_version")
        start = _time(self.effective_from, "effective_from")
        if self.effective_until is not None:
            end = _time(self.effective_until, "effective_until")
            if end <= start:
                raise ProviderSettlementRuleError(
                    "effective_until must be after effective_from"
                )
        observed = _time(self.observed_at, "observed_at")
        if observed < start:
            raise ProviderSettlementRuleError(
                "observed_at cannot predate the rule version effective_from"
            )
        _text(self.source_ref, "source_ref")
        _sha(self.source_payload_sha256, "source_payload_sha256")
        if (
            type(self.schema_version) is not int
            or self.schema_version != SETTLEMENT_RULE_SCHEMA_VERSION
        ):
            raise ProviderSettlementRuleError(
                f"schema_version must be integer {SETTLEMENT_RULE_SCHEMA_VERSION}"
            )

    @property
    def rule_id(self) -> str:
        payload = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @property
    def settlement_authorized(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "observed_at": self.observed_at,
            "rule_set_id": self.rule_set_id,
            "rule_version": self.rule_version,
            "schema_version": self.schema_version,
            "settlement_authorized": self.settlement_authorized,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "venue_id": self.venue_id,
        }

    def applicability(
        self,
        *,
        venue_id: str,
        rule_set_id: str,
        rule_version: str | None,
        event_time: str,
    ) -> SettlementRuleApplicability:
        _text(venue_id, "venue_id")
        _text(rule_set_id, "rule_set_id")
        when = _time(event_time, "event_time")
        if venue_id != self.venue_id or rule_set_id != self.rule_set_id:
            return SettlementRuleApplicability.UNKNOWN
        if rule_version is None:
            return SettlementRuleApplicability.UNKNOWN
        _text(rule_version, "rule_version")
        if rule_version != self.rule_version:
            return SettlementRuleApplicability.VERSION_MISMATCH
        start = _time(self.effective_from, "effective_from")
        end = (
            _time(self.effective_until, "effective_until")
            if self.effective_until is not None
            else None
        )
        if when < start or (end is not None and when >= end):
            return SettlementRuleApplicability.NOT_EFFECTIVE
        return SettlementRuleApplicability.APPLICABLE


@dataclass(frozen=True, slots=True)
class ProviderSettlementRuleCatalog:
    rules: tuple[ProviderSettlementRuleVersion, ...]
    as_of: str

    def __post_init__(self) -> None:
        if type(self.rules) is not tuple or not self.rules:
            raise ProviderSettlementRuleError("rules must be a non-empty tuple")
        as_of = _time(self.as_of, "as_of")
        identities: set[tuple[str, str, str]] = set()
        ids: set[str] = set()
        for rule in self.rules:
            if type(rule) is not ProviderSettlementRuleVersion:
                raise ProviderSettlementRuleError(
                    "rules must contain exact ProviderSettlementRuleVersion values"
                )
            if _time(rule.observed_at, "rule.observed_at") > as_of:
                raise ProviderSettlementRuleError(
                    "rule observation cannot be after catalog as_of"
                )
            identity = (rule.venue_id, rule.rule_set_id, rule.rule_version)
            if identity in identities:
                raise ProviderSettlementRuleError("duplicate settlement rule version")
            identities.add(identity)
            if rule.rule_id in ids:
                raise ProviderSettlementRuleError("duplicate settlement rule identity")
            ids.add(rule.rule_id)

    @property
    def catalog_id(self) -> str:
        payload = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @property
    def settlement_authorized(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "rules": [
                rule.to_canonical_dict()
                for rule in sorted(
                    self.rules,
                    key=lambda item: (
                        item.venue_id,
                        item.rule_set_id,
                        item.rule_version,
                        item.effective_from,
                        item.rule_id,
                    ),
                )
            ],
            "schema_version": SETTLEMENT_RULE_SCHEMA_VERSION,
            "settlement_authorized": self.settlement_authorized,
        }
