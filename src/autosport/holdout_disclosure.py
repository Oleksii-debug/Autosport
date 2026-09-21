from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .point_in_time_evidence import HoldoutConsumption, HoldoutConsumptionLedger
from .scientific_registry import DatasetSnapshot


class HoldoutDisclosureError(ValueError):
    """The disclosure boundary received a malformed policy input."""


class DisclosureChannel(str, Enum):
    """Where confirmation-holdout information becomes observable."""

    UI = "UI"
    HUMAN = "HUMAN"
    LLM = "LLM"
    EXPORT = "EXPORT"
    API = "API"
    LOG = "LOG"
    SEALED_EVALUATOR = "SEALED_EVALUATOR"


class DisclosureKind(str, Enum):
    """Semantic information class crossing the holdout disclosure boundary."""

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
    """Result of applying the disclosure-consumption policy.

    This record is descriptive only. In particular, a ``False`` consumed value is
    never evidence that the holdout is globally untouched; the canonical
    ``HoldoutConsumptionLedger`` remains the sole freshness authority.
    """

    channel: DisclosureChannel
    kind: DisclosureKind
    accessible_to_adaptive_actor: bool
    consumption: HoldoutConsumption | None

    @property
    def consumed(self) -> bool:
        return self.consumption is not None

    @property
    def proves_holdout_untouched(self) -> bool:
        return False

    @property
    def grants_promotion_authority(self) -> bool:
        return False


class HoldoutDisclosureGate:
    """Consume canonical confirmation evidence before adaptive disclosure returns.

    Call this boundary *before* an outcome-derived confirmation signal is exposed
    to any actor that can influence later model, policy, threshold, sizing, or
    research choices. The gate never creates a second holdout store: positive
    disclosure delegates directly to the existing durable
    ``HoldoutConsumptionLedger``.

    Pure non-outcome metadata and output that remains inaccessible inside a sealed
    precommitted evaluator do not consume confirmation capacity. Public disclosure
    channels are conservatively treated as adaptive-accessible even if a caller
    supplies ``False``; that flag can exempt only the sealed evaluator boundary.
    No result from this gate proves that a holdout is untouched or authorizes
    promotion.
    """

    _CONSUMER_IDENTITY = "holdout-disclosure-gate-v1"
    _PURPOSE = "outcome-derived-disclosure-to-adaptive-actor"

    def __init__(self, ledger: HoldoutConsumptionLedger) -> None:
        if type(ledger) is not HoldoutConsumptionLedger:
            raise HoldoutDisclosureError(
                "ledger must be the exact canonical HoldoutConsumptionLedger"
            )
        self._ledger = ledger

    @staticmethod
    def effective_accessibility(
        *,
        channel: DisclosureChannel,
        accessible_to_adaptive_actor: bool,
    ) -> bool:
        if type(channel) is not DisclosureChannel:
            raise HoldoutDisclosureError("channel must be an exact DisclosureChannel")
        if type(accessible_to_adaptive_actor) is not bool:
            raise HoldoutDisclosureError(
                "accessible_to_adaptive_actor must be an exact bool"
            )
        if channel is DisclosureChannel.SEALED_EVALUATOR:
            return accessible_to_adaptive_actor
        return True

    @classmethod
    def requires_consumption(
        cls,
        *,
        channel: DisclosureChannel,
        kind: DisclosureKind,
        accessible_to_adaptive_actor: bool,
    ) -> bool:
        if type(kind) is not DisclosureKind:
            raise HoldoutDisclosureError("kind must be an exact DisclosureKind")
        return kind.outcome_derived and cls.effective_accessibility(
            channel=channel,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
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
        effective_accessibility = self.effective_accessibility(
            channel=channel,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
        )
        if not self.requires_consumption(
            channel=channel,
            kind=kind,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
        ):
            return HoldoutDisclosureDecision(
                channel=channel,
                kind=kind,
                accessible_to_adaptive_actor=effective_accessibility,
                consumption=None,
            )

        consumption = self._ledger.consume(
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
            consumer_identity=self._CONSUMER_IDENTITY,
            purpose=self._PURPOSE,
            consumed_at_utc=disclosed_at_utc,
        )
        return HoldoutDisclosureDecision(
            channel=channel,
            kind=kind,
            accessible_to_adaptive_actor=effective_accessibility,
            consumption=consumption,
        )
