"""Structural completeness evidence for provider terminal outcome spaces.

This module composes with ``provider_settlement_rules``. It does not authenticate
provider source bytes and does not grant settlement, execution, payout, portfolio,
arbitrage, or real-money authority. A qualified value proves only that an explicit
completeness manifest is structurally exhaustive relative to one exact rulebook
market family and supplies a complete settlement state for every bound quote key.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from hashlib import sha256
import json

from .provider_settlement_rules import (
    ProviderSettlementRule,
    ProviderSettlementRulebook,
)


class ProviderTerminalOutcomeError(ValueError):
    """Raised when terminal-outcome completeness evidence is inconsistent."""


_ALLOWED_SETTLEMENTS = frozenset({"win", "loss", "void"})
_QUALIFICATION_TOKEN = object()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderTerminalOutcomeError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProviderTerminalOutcomeError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validate_quote_keys(quote_keys: object) -> tuple[str, ...]:
    if type(quote_keys) is not tuple or not quote_keys:
        raise ProviderTerminalOutcomeError("quote_keys must be a non-empty tuple")
    validated = tuple(_text(value, "quote_key") for value in quote_keys)
    if len(validated) != len(set(validated)):
        raise ProviderTerminalOutcomeError("quote_keys must be unique")
    return validated


@dataclass(frozen=True, slots=True)
class TerminalOutcomeScenario:
    """One declared terminal scenario and its complete quote settlement state."""

    scenario_code: str
    settlement_by_quote: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _text(self.scenario_code, "scenario_code")
        if type(self.settlement_by_quote) is not tuple or not self.settlement_by_quote:
            raise ProviderTerminalOutcomeError(
                "settlement_by_quote must be a non-empty tuple"
            )
        keys: list[str] = []
        for pair in self.settlement_by_quote:
            if type(pair) is not tuple or len(pair) != 2:
                raise ProviderTerminalOutcomeError(
                    "settlement_by_quote entries must be exact (quote_key, result) tuples"
                )
            quote_key, result = pair
            keys.append(_text(quote_key, "quote_key"))
            if type(result) is not str or result not in _ALLOWED_SETTLEMENTS:
                raise ProviderTerminalOutcomeError(
                    "settlement result must be win, loss, or void"
                )
        if len(keys) != len(set(keys)):
            raise ProviderTerminalOutcomeError(
                "settlement_by_quote contains duplicate quote keys"
            )

    def canonical_settlement(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.settlement_by_quote))

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "scenario_code": self.scenario_code,
            "settlement_by_quote": [
                {"quote_key": quote_key, "result": result}
                for quote_key, result in self.canonical_settlement()
            ],
        }


@dataclass(frozen=True, slots=True)
class ProviderTerminalOutcomeManifest:
    """Explicit source-backed declaration of a complete terminal outcome space.

    Construction does not itself prove that the external source is authentic.
    Qualification only verifies exact structural agreement with a supplied canonical
    provider rulebook.
    """

    provider_id: str
    rulebook_id: str
    market_family: str
    market_semantics_id: str
    completeness_source_ref: str
    completeness_source_payload_sha256: str
    quote_keys: tuple[str, ...]
    scenarios: tuple[TerminalOutcomeScenario, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        _sha256(self.rulebook_id, "rulebook_id")
        _text(self.market_family, "market_family")
        _text(self.market_semantics_id, "market_semantics_id")
        _text(self.completeness_source_ref, "completeness_source_ref")
        _sha256(
            self.completeness_source_payload_sha256,
            "completeness_source_payload_sha256",
        )
        _validate_quote_keys(self.quote_keys)
        if type(self.scenarios) is not tuple or not self.scenarios:
            raise ProviderTerminalOutcomeError(
                "scenarios must be a non-empty tuple"
            )
        if any(type(value) is not TerminalOutcomeScenario for value in self.scenarios):
            raise ProviderTerminalOutcomeError(
                "scenarios must contain exact TerminalOutcomeScenario values"
            )
        codes = [scenario.scenario_code for scenario in self.scenarios]
        if len(codes) != len(set(codes)):
            raise ProviderTerminalOutcomeError(
                "scenarios contain duplicate scenario_code values"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderTerminalOutcomeError("schema_version must be exactly 1")

    @property
    def manifest_id(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "completeness_source_payload_sha256": (
                self.completeness_source_payload_sha256
            ),
            "completeness_source_ref": self.completeness_source_ref,
            "market_family": self.market_family,
            "market_semantics_id": self.market_semantics_id,
            "provider_id": self.provider_id,
            "quote_keys": sorted(self.quote_keys),
            "rulebook_id": self.rulebook_id,
            "scenarios": [
                scenario.to_canonical_dict()
                for scenario in sorted(
                    self.scenarios, key=lambda value: value.scenario_code
                )
            ],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class QualifiedTerminalOutcome:
    """One rulebook-bound terminal state emitted only by the qualifier."""

    scenario_code: str
    treatment_code: str
    rule_id: str
    settlement_by_quote: tuple[tuple[str, str], ...]
    _qualification_token: InitVar[object] = None

    def __post_init__(self, _qualification_token: object) -> None:
        if _qualification_token is not _QUALIFICATION_TOKEN:
            raise ProviderTerminalOutcomeError(
                "QualifiedTerminalOutcome must be issued by qualify_terminal_outcome_space"
            )
        _text(self.scenario_code, "scenario_code")
        _text(self.treatment_code, "treatment_code")
        _sha256(self.rule_id, "rule_id")
        TerminalOutcomeScenario(
            scenario_code=self.scenario_code,
            settlement_by_quote=self.settlement_by_quote,
        )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "scenario_code": self.scenario_code,
            "settlement_by_quote": [
                {"quote_key": quote_key, "result": result}
                for quote_key, result in sorted(self.settlement_by_quote)
            ],
            "treatment_code": self.treatment_code,
        }


@dataclass(frozen=True, slots=True)
class QualifiedProviderTerminalOutcomeSpace:
    """Deterministic structural completeness witness for one exact rulebook."""

    provider_id: str
    rulebook_version: str
    rulebook_id: str
    rulebook_source_ref: str
    rulebook_source_payload_sha256: str
    market_family: str
    market_semantics_id: str
    completeness_manifest_id: str
    completeness_source_ref: str
    completeness_source_payload_sha256: str
    quote_keys: tuple[str, ...]
    outcomes: tuple[QualifiedTerminalOutcome, ...]
    schema_version: int = 1
    _qualification_token: InitVar[object] = None

    def __post_init__(self, _qualification_token: object) -> None:
        if _qualification_token is not _QUALIFICATION_TOKEN:
            raise ProviderTerminalOutcomeError(
                "QualifiedProviderTerminalOutcomeSpace must be issued by "
                "qualify_terminal_outcome_space"
            )
        _text(self.provider_id, "provider_id")
        _text(self.rulebook_version, "rulebook_version")
        _sha256(self.rulebook_id, "rulebook_id")
        _text(self.rulebook_source_ref, "rulebook_source_ref")
        _sha256(
            self.rulebook_source_payload_sha256,
            "rulebook_source_payload_sha256",
        )
        _text(self.market_family, "market_family")
        _text(self.market_semantics_id, "market_semantics_id")
        _sha256(self.completeness_manifest_id, "completeness_manifest_id")
        _text(self.completeness_source_ref, "completeness_source_ref")
        _sha256(
            self.completeness_source_payload_sha256,
            "completeness_source_payload_sha256",
        )
        _validate_quote_keys(self.quote_keys)
        if type(self.outcomes) is not tuple or not self.outcomes:
            raise ProviderTerminalOutcomeError(
                "outcomes must be a non-empty tuple"
            )
        if any(type(value) is not QualifiedTerminalOutcome for value in self.outcomes):
            raise ProviderTerminalOutcomeError(
                "outcomes must contain exact QualifiedTerminalOutcome values"
            )
        codes = [outcome.scenario_code for outcome in self.outcomes]
        if codes != sorted(codes) or len(codes) != len(set(codes)):
            raise ProviderTerminalOutcomeError(
                "outcomes must have unique scenario codes in canonical order"
            )
        quote_key_set = set(self.quote_keys)
        for outcome in self.outcomes:
            if {key for key, _ in outcome.settlement_by_quote} != quote_key_set:
                raise ProviderTerminalOutcomeError(
                    "every terminal outcome must settle every bound quote key exactly once"
                )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderTerminalOutcomeError("schema_version must be exactly 1")

    @property
    def space_id(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())

    @property
    def structural_completeness_proven(self) -> bool:
        return True

    @property
    def provider_source_authenticity_proven(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False

    @property
    def outcome_independent_positive(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "completeness_manifest_id": self.completeness_manifest_id,
            "completeness_source_payload_sha256": (
                self.completeness_source_payload_sha256
            ),
            "completeness_source_ref": self.completeness_source_ref,
            "market_family": self.market_family,
            "market_semantics_id": self.market_semantics_id,
            "outcomes": [outcome.to_canonical_dict() for outcome in self.outcomes],
            "provider_id": self.provider_id,
            "quote_keys": sorted(self.quote_keys),
            "rulebook_id": self.rulebook_id,
            "rulebook_source_payload_sha256": self.rulebook_source_payload_sha256,
            "rulebook_source_ref": self.rulebook_source_ref,
            "rulebook_version": self.rulebook_version,
            "schema_version": self.schema_version,
        }

    def verify_rulebook(self, rulebook: ProviderSettlementRulebook) -> None:
        if type(rulebook) is not ProviderSettlementRulebook:
            raise ProviderTerminalOutcomeError(
                "rulebook must be an exact ProviderSettlementRulebook"
            )
        if (
            self.provider_id != rulebook.provider_id
            or self.rulebook_version != rulebook.rulebook_version
            or self.rulebook_id != rulebook.rulebook_id
            or self.rulebook_source_ref != rulebook.source_ref
            or self.rulebook_source_payload_sha256 != rulebook.source_payload_sha256
        ):
            raise ProviderTerminalOutcomeError(
                "terminal outcome space does not match exact rulebook identity"
            )
        rules = {
            rule.scenario_code: rule
            for rule in rulebook.rules
            if rule.market_family == self.market_family
        }
        if set(rules) != {outcome.scenario_code for outcome in self.outcomes}:
            raise ProviderTerminalOutcomeError(
                "terminal outcome space no longer matches rulebook scenario coverage"
            )
        for outcome in self.outcomes:
            rule = rules[outcome.scenario_code]
            if (
                outcome.rule_id != rule.rule_id
                or outcome.treatment_code != rule.treatment_code
            ):
                raise ProviderTerminalOutcomeError(
                    "terminal outcome space no longer matches exact rule identity"
                )


def _rules_for_market(
    rulebook: ProviderSettlementRulebook,
    market_family: str,
) -> dict[str, ProviderSettlementRule]:
    rules = {
        rule.scenario_code: rule
        for rule in rulebook.rules
        if rule.market_family == market_family
    }
    if not rules:
        raise ProviderTerminalOutcomeError(
            "rulebook has no rules for manifest market_family"
        )
    return rules


def qualify_terminal_outcome_space(
    *,
    rulebook: ProviderSettlementRulebook,
    manifest: ProviderTerminalOutcomeManifest,
) -> QualifiedProviderTerminalOutcomeSpace:
    """Qualify structural terminal-state completeness against one exact rulebook."""

    if type(rulebook) is not ProviderSettlementRulebook:
        raise ProviderTerminalOutcomeError(
            "rulebook must be an exact ProviderSettlementRulebook"
        )
    if type(manifest) is not ProviderTerminalOutcomeManifest:
        raise ProviderTerminalOutcomeError(
            "manifest must be an exact ProviderTerminalOutcomeManifest"
        )
    if (
        manifest.provider_id != rulebook.provider_id
        or manifest.rulebook_id != rulebook.rulebook_id
    ):
        raise ProviderTerminalOutcomeError(
            "manifest does not bind the exact provider rulebook"
        )

    quote_keys = _validate_quote_keys(manifest.quote_keys)
    quote_key_set = set(quote_keys)
    rules = _rules_for_market(rulebook, manifest.market_family)
    scenarios = {scenario.scenario_code: scenario for scenario in manifest.scenarios}

    if set(scenarios) != set(rules):
        raise ProviderTerminalOutcomeError(
            "manifest scenario set must exactly match rulebook market-family coverage"
        )

    outcomes: list[QualifiedTerminalOutcome] = []
    for scenario_code in sorted(scenarios):
        scenario = scenarios[scenario_code]
        settlement_keys = {key for key, _ in scenario.settlement_by_quote}
        if settlement_keys != quote_key_set:
            raise ProviderTerminalOutcomeError(
                "every manifest scenario must settle every bound quote key exactly once"
            )
        rule = rules[scenario_code]
        outcomes.append(
            QualifiedTerminalOutcome(
                scenario_code=scenario_code,
                treatment_code=rule.treatment_code,
                rule_id=rule.rule_id,
                settlement_by_quote=tuple(sorted(scenario.settlement_by_quote)),
                _qualification_token=_QUALIFICATION_TOKEN,
            )
        )

    qualified = QualifiedProviderTerminalOutcomeSpace(
        provider_id=rulebook.provider_id,
        rulebook_version=rulebook.rulebook_version,
        rulebook_id=rulebook.rulebook_id,
        rulebook_source_ref=rulebook.source_ref,
        rulebook_source_payload_sha256=rulebook.source_payload_sha256,
        market_family=manifest.market_family,
        market_semantics_id=manifest.market_semantics_id,
        completeness_manifest_id=manifest.manifest_id,
        completeness_source_ref=manifest.completeness_source_ref,
        completeness_source_payload_sha256=manifest.completeness_source_payload_sha256,
        quote_keys=tuple(sorted(quote_keys)),
        outcomes=tuple(outcomes),
        _qualification_token=_QUALIFICATION_TOKEN,
    )
    qualified.verify_rulebook(rulebook)
    return qualified
