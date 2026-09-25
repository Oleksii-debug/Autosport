from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .point_in_time_evidence import HoldoutConsumption, HoldoutConsumptionLedger
from .scientific_registry import DatasetSnapshot


_CANONICAL_HOLDOUT_LEDGER_CONSUME = HoldoutConsumptionLedger.consume


class HoldoutDisclosureError(ValueError):
    """The disclosure boundary received a malformed policy input."""


class DisclosureChannel(str, Enum):
    """Audit-only origin/channel descriptor for a disclosure."""

    UI = "UI"
    HUMAN = "HUMAN"
    LLM = "LLM"
    EXPORT = "EXPORT"
    API = "API"
    LOG = "LOG"
    SEALED_EVALUATOR = "SEALED_EVALUATOR"


class DisclosureKind(str, Enum):
    """Audit-only semantic descriptor for information crossing the boundary."""

    RAW_LABEL = "RAW_LABEL"
    EVENT_OUTCOME = "EVENT_OUTCOME"
    AGGREGATE_SCORE = "AGGREGATE_SCORE"
    PASS_FAIL = "PASS_FAIL"
    NON_OUTCOME_METADATA = "NON_OUTCOME_METADATA"

    @property
    def outcome_derived(self) -> bool:
        return self is not DisclosureKind.NON_OUTCOME_METADATA


@dataclass(frozen=True, slots=True)
class HoldoutDisclosureDecision:
    """Durable result of crossing the confirmation-holdout disclosure boundary.

    ``channel``, ``kind`` and ``accessible_to_adaptive_actor`` are audit
    descriptors only. They never decide freshness. Calling the disclosure gate
    means information has already crossed the sealed evaluation boundary, so
    canonical holdout consumption must exist before this object is returned.
    """

    channel: DisclosureChannel
    kind: DisclosureKind
    accessible_to_adaptive_actor: bool
    consumption: HoldoutConsumption

    @property
    def consumed(self) -> bool:
        return True

    @property
    def proves_holdout_untouched(self) -> bool:
        return False

    @property
    def grants_promotion_authority(self) -> bool:
        return False


class HoldoutDisclosureGate:
    """Consume canonical confirmation capacity before a disclosure returns.

    This adapter is intentionally fail-closed. Invoking :meth:`record` means
    information has crossed the sealed evaluation boundary. The gate therefore
    always delegates to the existing durable ``HoldoutConsumptionLedger`` before
    returning, regardless of caller-authored channel/kind/accessibility labels.

    A value that remains strictly internal to a sealed precommitted evaluator has
    not been disclosed and must not call this gate. A future narrower exemption
    would require an independent product-owned capability proving that internal
    boundary; caller labels are never sufficient.

    The gate creates no second holdout store, never proves a holdout untouched,
    and grants no promotion authority.
    """

    _CONSUMER_IDENTITY = "holdout-disclosure-gate-v1"
    _PURPOSE = "holdout-disclosure-boundary-crossing-v1"

    def __init__(self, ledger: HoldoutConsumptionLedger) -> None:
        if type(ledger) is not HoldoutConsumptionLedger:
            raise HoldoutDisclosureError(
                "ledger must be the exact canonical HoldoutConsumptionLedger"
            )
        self._ledger = ledger

    @staticmethod
    def _validate_descriptors(
        *,
        channel: DisclosureChannel,
        kind: DisclosureKind,
        accessible_to_adaptive_actor: bool,
    ) -> None:
        if type(channel) is not DisclosureChannel:
            raise HoldoutDisclosureError("channel must be an exact DisclosureChannel")
        if type(kind) is not DisclosureKind:
            raise HoldoutDisclosureError("kind must be an exact DisclosureKind")
        if type(accessible_to_adaptive_actor) is not bool:
            raise HoldoutDisclosureError(
                "accessible_to_adaptive_actor must be an exact bool"
            )

    def record(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        channel: DisclosureChannel,
        kind: DisclosureKind,
        accessible_to_adaptive_actor: bool,
        disclosed_at_utc: str,
    ) -> HoldoutDisclosureDecision:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise HoldoutDisclosureError(
                "dataset_snapshot must be an exact DatasetSnapshot"
            )
        self._validate_descriptors(
            channel=channel,
            kind=kind,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
        )

        if "consume" in vars(self._ledger):
            raise HoldoutDisclosureError(
                "ledger consume dispatch must not be instance-shadowed"
            )
        if HoldoutConsumptionLedger.consume is not _CANONICAL_HOLDOUT_LEDGER_CONSUME:
            raise HoldoutDisclosureError(
                "canonical HoldoutConsumptionLedger.consume dispatch was rebound"
            )

        consumption = _CANONICAL_HOLDOUT_LEDGER_CONSUME(
            self._ledger,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
            consumer_identity=self._CONSUMER_IDENTITY,
            purpose=self._PURPOSE,
            consumed_at_utc=disclosed_at_utc,
        )
        if type(consumption) is not HoldoutConsumption:
            raise HoldoutDisclosureError(
                "canonical holdout ledger returned an invalid consumption record"
            )
        return HoldoutDisclosureDecision(
            channel=channel,
            kind=kind,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
            consumption=consumption,
        )
