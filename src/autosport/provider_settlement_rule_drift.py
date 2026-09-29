from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any

from .provider_settlement_rules import (
    ProviderSettlementRuleError,
    ProviderSettlementRuleSelection,
    ProviderSettlementRulebook,
)


class ProviderSettlementRuleDriftError(ValueError):
    """Raised when drift/quarantine evidence is malformed or caller-minted."""


class RulesetDisposition(str, Enum):
    BOUND_SNAPSHOT_STRUCTURALLY_VALID = "BOUND_SNAPSHOT_STRUCTURALLY_VALID"
    QUARANTINE = "QUARANTINE"


class LatestRulesetSignal(str, Enum):
    SAME_AS_BOUND = "SAME_AS_BOUND"
    LATEST_DIFFERS = "LATEST_DIFFERS"
    LATEST_UNKNOWN = "LATEST_UNKNOWN"
    LATEST_UNRELATED = "LATEST_UNRELATED"


class QuarantineReason(str, Enum):
    NONE = "NONE"
    BOUND_SNAPSHOT_MISSING = "BOUND_SNAPSHOT_MISSING"
    BOUND_SNAPSHOT_MISMATCH = "BOUND_SNAPSHOT_MISMATCH"
    MARKET_SCOPE_DRIFT = "MARKET_SCOPE_DRIFT"
    MARKET_INFO_DRIFT = "MARKET_INFO_DRIFT"


_HEX = frozenset("0123456789abcdef")


def _sha256(value: Any, *, field: str) -> str:
    if type(value) is not str or len(value) != 64 or value != value.lower():
        raise ProviderSettlementRuleDriftError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    if any(ch not in _HEX for ch in value):
        raise ProviderSettlementRuleDriftError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return value


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderSettlementRuleSnapshotAssertion:
    """Structural snapshot payload for separately authoritative decision evidence.

    This value does not prove that a decision/order existed, that a provider
    accepted anything, or that settlement is applicable. It freezes the exact
    structural rule selection plus market-scope and market-info identities so a
    later consumer can detect substitution or drift.
    """

    provider_id: str
    rulebook_version: str
    rulebook_id: str
    evaluated_at: str
    market_family: str
    scenario_code: str
    treatment_code: str
    rule_id: str
    selection_id: str
    market_scope_sha256: str
    market_info_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderSettlementRuleDriftError("schema_version must be exactly 1")
        _sha256(self.selection_id, field="selection_id")
        _sha256(self.market_scope_sha256, field="market_scope_sha256")
        _sha256(self.market_info_sha256, field="market_info_sha256")
        try:
            selection = self.to_selection()
        except ProviderSettlementRuleError as exc:
            raise ProviderSettlementRuleDriftError(
                "snapshot assertion contains invalid settlement-rule selection"
            ) from exc
        if selection.selection_id != self.selection_id:
            raise ProviderSettlementRuleDriftError(
                "selection_id does not match exact structural rule selection"
            )

    @classmethod
    def from_selection(
        cls,
        selection: ProviderSettlementRuleSelection,
        *,
        market_scope_sha256: str,
        market_info_sha256: str,
    ) -> "ProviderSettlementRuleSnapshotAssertion":
        if type(selection) is not ProviderSettlementRuleSelection:
            raise ProviderSettlementRuleDriftError(
                "selection must be an exact ProviderSettlementRuleSelection"
            )
        return cls(
            provider_id=selection.provider_id,
            rulebook_version=selection.rulebook_version,
            rulebook_id=selection.rulebook_id,
            evaluated_at=selection.evaluated_at,
            market_family=selection.market_family,
            scenario_code=selection.scenario_code,
            treatment_code=selection.treatment_code,
            rule_id=selection.rule_id,
            selection_id=selection.selection_id,
            market_scope_sha256=_sha256(
                market_scope_sha256, field="market_scope_sha256"
            ),
            market_info_sha256=_sha256(
                market_info_sha256, field="market_info_sha256"
            ),
        )

    def to_selection(self) -> ProviderSettlementRuleSelection:
        return ProviderSettlementRuleSelection(
            provider_id=self.provider_id,
            rulebook_version=self.rulebook_version,
            rulebook_id=self.rulebook_id,
            evaluated_at=self.evaluated_at,
            market_family=self.market_family,
            scenario_code=self.scenario_code,
            treatment_code=self.treatment_code,
            rule_id=self.rule_id,
        )

    @property
    def assertion_id(self) -> str:
        return _canonical_sha256(
            {
                "market_info_sha256": self.market_info_sha256,
                "market_scope_sha256": self.market_scope_sha256,
                "schema_version": self.schema_version,
                "selection_id": self.selection_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderSettlementRuleDriftAssessment:
    """Fail-closed structural drift assessment.

    bound_treatment_code is exposed only when the exact bound snapshot survives
    all quarantine checks. A newer rulebook can signal drift but cannot replace
    that frozen treatment. This value is not settlement or execution authority.
    """

    assertion_id: str
    disposition: RulesetDisposition
    latest_signal: LatestRulesetSignal
    quarantine_reason: QuarantineReason
    bound_rulebook_id: str | None
    bound_rule_id: str | None
    bound_treatment_code: str | None
    latest_rulebook_id: str | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _sha256(self.assertion_id, field="assertion_id")
        if type(self.disposition) is not RulesetDisposition:
            raise ProviderSettlementRuleDriftError(
                "disposition must be an exact RulesetDisposition"
            )
        if type(self.latest_signal) is not LatestRulesetSignal:
            raise ProviderSettlementRuleDriftError(
                "latest_signal must be an exact LatestRulesetSignal"
            )
        if type(self.quarantine_reason) is not QuarantineReason:
            raise ProviderSettlementRuleDriftError(
                "quarantine_reason must be an exact QuarantineReason"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderSettlementRuleDriftError("schema_version must be exactly 1")
        for name, value in (
            ("bound_rulebook_id", self.bound_rulebook_id),
            ("bound_rule_id", self.bound_rule_id),
            ("latest_rulebook_id", self.latest_rulebook_id),
        ):
            if value is not None:
                _sha256(value, field=name)
        if self.disposition is RulesetDisposition.QUARANTINE:
            if self.quarantine_reason is QuarantineReason.NONE:
                raise ProviderSettlementRuleDriftError(
                    "quarantine assessment requires a quarantine reason"
                )
            if any(
                value is not None
                for value in (
                    self.bound_rulebook_id,
                    self.bound_rule_id,
                    self.bound_treatment_code,
                )
            ):
                raise ProviderSettlementRuleDriftError(
                    "quarantine assessment must not expose a usable bound treatment"
                )
        else:
            if self.quarantine_reason is not QuarantineReason.NONE:
                raise ProviderSettlementRuleDriftError(
                    "usable bound snapshot cannot carry a quarantine reason"
                )
            if (
                self.bound_rulebook_id is None
                or self.bound_rule_id is None
                or type(self.bound_treatment_code) is not str
                or not self.bound_treatment_code
            ):
                raise ProviderSettlementRuleDriftError(
                    "usable bound snapshot requires exact bound rule identity"
                )


def _latest_signal(
    assertion: ProviderSettlementRuleSnapshotAssertion,
    latest_rulebook: ProviderSettlementRulebook | None,
) -> tuple[LatestRulesetSignal, str | None]:
    if latest_rulebook is None:
        return LatestRulesetSignal.LATEST_UNKNOWN, None
    if type(latest_rulebook) is not ProviderSettlementRulebook:
        raise ProviderSettlementRuleDriftError(
            "latest_rulebook must be an exact ProviderSettlementRulebook or None"
        )
    if latest_rulebook.provider_id != assertion.provider_id:
        return LatestRulesetSignal.LATEST_UNRELATED, latest_rulebook.rulebook_id
    if latest_rulebook.rulebook_id == assertion.rulebook_id:
        return LatestRulesetSignal.SAME_AS_BOUND, latest_rulebook.rulebook_id
    return LatestRulesetSignal.LATEST_DIFFERS, latest_rulebook.rulebook_id


def _quarantine(
    *,
    assertion: ProviderSettlementRuleSnapshotAssertion,
    reason: QuarantineReason,
    latest_signal: LatestRulesetSignal,
    latest_rulebook_id: str | None,
) -> ProviderSettlementRuleDriftAssessment:
    return ProviderSettlementRuleDriftAssessment(
        assertion_id=assertion.assertion_id,
        disposition=RulesetDisposition.QUARANTINE,
        latest_signal=latest_signal,
        quarantine_reason=reason,
        bound_rulebook_id=None,
        bound_rule_id=None,
        bound_treatment_code=None,
        latest_rulebook_id=latest_rulebook_id,
    )


def assess_settlement_rule_drift(
    assertion: ProviderSettlementRuleSnapshotAssertion,
    *,
    bound_rulebook: ProviderSettlementRulebook | None,
    latest_rulebook: ProviderSettlementRulebook | None,
    current_market_scope_sha256: str,
    current_market_info_sha256: str,
) -> ProviderSettlementRuleDriftAssessment:
    """Reverify a frozen rule snapshot and quarantine material bound-state drift.

    The exact bound rulebook is the sole source of the returned structural treatment
    label. This assessment is not permission to settle. latest_rulebook is diagnostic
    drift evidence only and can never replace the bound snapshot.
    """

    if type(assertion) is not ProviderSettlementRuleSnapshotAssertion:
        raise ProviderSettlementRuleDriftError(
            "assertion must be an exact ProviderSettlementRuleSnapshotAssertion"
        )
    current_market_scope_sha256 = _sha256(
        current_market_scope_sha256, field="current_market_scope_sha256"
    )
    current_market_info_sha256 = _sha256(
        current_market_info_sha256, field="current_market_info_sha256"
    )
    latest_signal, latest_id = _latest_signal(assertion, latest_rulebook)

    if bound_rulebook is None:
        return _quarantine(
            assertion=assertion,
            reason=QuarantineReason.BOUND_SNAPSHOT_MISSING,
            latest_signal=latest_signal,
            latest_rulebook_id=latest_id,
        )
    if type(bound_rulebook) is not ProviderSettlementRulebook:
        return _quarantine(
            assertion=assertion,
            reason=QuarantineReason.BOUND_SNAPSHOT_MISMATCH,
            latest_signal=latest_signal,
            latest_rulebook_id=latest_id,
        )

    selection = assertion.to_selection()
    try:
        selection.verify_rulebook(bound_rulebook)
    except ProviderSettlementRuleError:
        return _quarantine(
            assertion=assertion,
            reason=QuarantineReason.BOUND_SNAPSHOT_MISMATCH,
            latest_signal=latest_signal,
            latest_rulebook_id=latest_id,
        )

    if current_market_scope_sha256 != assertion.market_scope_sha256:
        return _quarantine(
            assertion=assertion,
            reason=QuarantineReason.MARKET_SCOPE_DRIFT,
            latest_signal=latest_signal,
            latest_rulebook_id=latest_id,
        )
    if current_market_info_sha256 != assertion.market_info_sha256:
        return _quarantine(
            assertion=assertion,
            reason=QuarantineReason.MARKET_INFO_DRIFT,
            latest_signal=latest_signal,
            latest_rulebook_id=latest_id,
        )

    return ProviderSettlementRuleDriftAssessment(
        assertion_id=assertion.assertion_id,
        disposition=RulesetDisposition.BOUND_SNAPSHOT_STRUCTURALLY_VALID,
        latest_signal=latest_signal,
        quarantine_reason=QuarantineReason.NONE,
        bound_rulebook_id=bound_rulebook.rulebook_id,
        bound_rule_id=selection.rule_id,
        bound_treatment_code=selection.treatment_code,
        latest_rulebook_id=latest_id,
    )
