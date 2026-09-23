from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import re
from typing import Mapping


class FixStream(str, Enum):
    PRIMARY = "PRIMARY"
    DROP_COPY = "DROP_COPY"


class ContinuityAction(str, Enum):
    RESUME = "RESUME"
    REQUEST_RESEND = "REQUEST_RESEND"
    RESET_AND_RECONCILE = "RESET_AND_RECONCILE"


class ResendAction(str, Enum):
    REQUEST_RESEND = "REQUEST_RESEND"
    PRUNED_GAP_RECONCILE = "PRUNED_GAP_RECONCILE"
    RESET_AND_RECONCILE = "RESET_AND_RECONCILE"


class ExecObservationRelation(str, Enum):
    DISTINCT = "DISTINCT"
    IDEMPOTENT_DUPLICATE = "IDEMPOTENT_DUPLICATE"
    CONFLICT = "CONFLICT"


def _require_token(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"{field} contains forbidden control characters")
    return value


def _require_positive_seq(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be int")
    if value < 1:
        raise ValueError(f"{field} must be >= 1")
    return value


@dataclass(frozen=True, slots=True)
class ProphetXFixSessionIdentity:
    """Non-secret identity for one persistent ProphetX FIX session.

    ProphetX documents the persistent FIX session identity as
    BeginString:SenderCompID:TargetCompID. The credential revision fingerprint
    is deliberately non-secret metadata: callers must never put passwords,
    bearer tokens, API secrets, or private keys here.
    """

    environment: str
    begin_string: str
    sender_comp_id: str
    target_comp_id: str
    credential_revision_fingerprint: str

    def __post_init__(self) -> None:
        for field in (
            "environment",
            "begin_string",
            "sender_comp_id",
            "target_comp_id",
            "credential_revision_fingerprint",
        ):
            object.__setattr__(self, field, _require_token(getattr(self, field), field))
        if self.environment not in {"SANDBOX", "PRODUCTION"}:
            raise ValueError("environment must be SANDBOX or PRODUCTION")
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", self.credential_revision_fingerprint) is None:
            raise ValueError(
                "credential_revision_fingerprint must be a non-secret sha256 fingerprint"
            )
        object.__setattr__(
            self,
            "credential_revision_fingerprint",
            self.credential_revision_fingerprint.lower(),
        )

    @property
    def persistent_session_key(self) -> str:
        return f"{self.begin_string}:{self.sender_comp_id}:{self.target_comp_id}"

    def to_evidence(self) -> Mapping[str, str]:
        return {
            "environment": self.environment,
            "begin_string": self.begin_string,
            "sender_comp_id": self.sender_comp_id,
            "target_comp_id": self.target_comp_id,
            "credential_revision_fingerprint": self.credential_revision_fingerprint,
            "persistent_session_key": self.persistent_session_key,
        }


@dataclass(frozen=True, slots=True)
class ProphetXFixSequenceCheckpoint:
    identity: ProphetXFixSessionIdentity
    stream: FixStream
    next_expected_inbound_seq_num: int
    next_outbound_seq_num: int
    last_durable_inbound_seq_num: int
    reset_epoch: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProphetXFixSessionIdentity):
            raise TypeError("identity must be ProphetXFixSessionIdentity")
        if not isinstance(self.stream, FixStream):
            raise TypeError("stream must be FixStream")
        object.__setattr__(
            self,
            "next_expected_inbound_seq_num",
            _require_positive_seq(
                self.next_expected_inbound_seq_num, "next_expected_inbound_seq_num"
            ),
        )
        object.__setattr__(
            self,
            "next_outbound_seq_num",
            _require_positive_seq(self.next_outbound_seq_num, "next_outbound_seq_num"),
        )
        if isinstance(self.last_durable_inbound_seq_num, bool) or not isinstance(
            self.last_durable_inbound_seq_num, int
        ):
            raise TypeError("last_durable_inbound_seq_num must be int")
        if self.last_durable_inbound_seq_num < 0:
            raise ValueError("last_durable_inbound_seq_num must be >= 0")
        if self.last_durable_inbound_seq_num >= self.next_expected_inbound_seq_num:
            raise ValueError(
                "last_durable_inbound_seq_num must be lower than "
                "next_expected_inbound_seq_num"
            )
        if isinstance(self.reset_epoch, bool) or not isinstance(self.reset_epoch, int):
            raise TypeError("reset_epoch must be int")
        if self.reset_epoch < 0:
            raise ValueError("reset_epoch must be >= 0")

    def accepts_sequence_comparison_with(
        self, other: "ProphetXFixSequenceCheckpoint"
    ) -> bool:
        return self.identity == other.identity and self.stream is other.stream


@dataclass(frozen=True, slots=True)
class LogonContinuityAssessment:
    action: ContinuityAction
    reason: str
    provider_logon_msg_seq_num: int
    next_expected_inbound_seq_num: int | None
    reset_epoch_after_action: int | None
    application_reconciliation_required: bool
    operator_reset_instruction_observed: bool
    next_expected_msg_seq_num_tag_ignored: bool

    @property
    def authorizes_economic_action(self) -> bool:
        return False


def assess_logon_continuity(
    checkpoint: ProphetXFixSequenceCheckpoint | None,
    *,
    provider_logon_msg_seq_num: int,
    local_sequence_store_available: bool = True,
    operator_reset_instruction_observed: bool = False,
    next_expected_msg_seq_num_tag: int | None = None,
) -> LogonContinuityAssessment:
    """Classify reconnect/reset behavior without minting economic authority.

    Tag 789 is deliberately ignored because current ProphetX order-entry
    documentation says it is unsupported/ignored. Operator reset text is
    retained as evidence but cannot by itself rewrite sequence/economic truth.
    """

    provider_seq = _require_positive_seq(
        provider_logon_msg_seq_num, "provider_logon_msg_seq_num"
    )
    if not isinstance(local_sequence_store_available, bool):
        raise TypeError("local_sequence_store_available must be bool")
    if not isinstance(operator_reset_instruction_observed, bool):
        raise TypeError("operator_reset_instruction_observed must be bool")
    if next_expected_msg_seq_num_tag is not None:
        _require_positive_seq(next_expected_msg_seq_num_tag, "next_expected_msg_seq_num_tag")

    ignored_789 = next_expected_msg_seq_num_tag is not None

    if checkpoint is None or not local_sequence_store_available:
        epoch = 0 if checkpoint is None else checkpoint.reset_epoch + 1
        return LogonContinuityAssessment(
            action=ContinuityAction.RESET_AND_RECONCILE,
            reason="LOCAL_SEQUENCE_STATE_UNAVAILABLE",
            provider_logon_msg_seq_num=provider_seq,
            next_expected_inbound_seq_num=(
                None if checkpoint is None else checkpoint.next_expected_inbound_seq_num
            ),
            reset_epoch_after_action=epoch,
            application_reconciliation_required=True,
            operator_reset_instruction_observed=operator_reset_instruction_observed,
            next_expected_msg_seq_num_tag_ignored=ignored_789,
        )

    expected = checkpoint.next_expected_inbound_seq_num
    if provider_seq < expected:
        return LogonContinuityAssessment(
            action=ContinuityAction.RESET_AND_RECONCILE,
            reason="PROVIDER_SEQUENCE_STATE_BEHIND_DURABLE_EXPECTATION",
            provider_logon_msg_seq_num=provider_seq,
            next_expected_inbound_seq_num=expected,
            reset_epoch_after_action=checkpoint.reset_epoch + 1,
            application_reconciliation_required=True,
            operator_reset_instruction_observed=operator_reset_instruction_observed,
            next_expected_msg_seq_num_tag_ignored=ignored_789,
        )
    if provider_seq > expected:
        return LogonContinuityAssessment(
            action=ContinuityAction.REQUEST_RESEND,
            reason="PROVIDER_SEQUENCE_GAP",
            provider_logon_msg_seq_num=provider_seq,
            next_expected_inbound_seq_num=expected,
            reset_epoch_after_action=checkpoint.reset_epoch,
            application_reconciliation_required=False,
            operator_reset_instruction_observed=operator_reset_instruction_observed,
            next_expected_msg_seq_num_tag_ignored=ignored_789,
        )
    return LogonContinuityAssessment(
        action=ContinuityAction.RESUME,
        reason="DURABLE_SEQUENCE_CONTINUES",
        provider_logon_msg_seq_num=provider_seq,
        next_expected_inbound_seq_num=expected,
        reset_epoch_after_action=checkpoint.reset_epoch,
        application_reconciliation_required=False,
        operator_reset_instruction_observed=operator_reset_instruction_observed,
        next_expected_msg_seq_num_tag_ignored=ignored_789,
    )


@dataclass(frozen=True, slots=True)
class ResendWindowAssessment:
    action: ResendAction
    disconnect_duration_microseconds: int
    resend_window_available: bool
    provider_history_complete: bool
    application_reconciliation_required: bool

    @property
    def proves_omitted_application_events_absent(self) -> bool:
        return False

    @property
    def authorizes_economic_action(self) -> bool:
        return False


def assess_resend_window(
    disconnect_duration: timedelta,
    *,
    provider_gap_fill_pruned: bool = False,
) -> ResendWindowAssessment:
    if not isinstance(disconnect_duration, timedelta):
        raise TypeError("disconnect_duration must be datetime.timedelta")
    if not isinstance(provider_gap_fill_pruned, bool):
        raise TypeError("provider_gap_fill_pruned must be bool")
    if disconnect_duration.total_seconds() < 0:
        raise ValueError("disconnect_duration must not be negative")

    micros = (
        disconnect_duration.days * 86_400_000_000
        + disconnect_duration.seconds * 1_000_000
        + disconnect_duration.microseconds
    )
    if provider_gap_fill_pruned:
        return ResendWindowAssessment(
            action=ResendAction.PRUNED_GAP_RECONCILE,
            disconnect_duration_microseconds=micros,
            resend_window_available=False,
            provider_history_complete=False,
            application_reconciliation_required=True,
        )

    if disconnect_duration <= timedelta(days=7):
        return ResendWindowAssessment(
            action=ResendAction.REQUEST_RESEND,
            disconnect_duration_microseconds=micros,
            resend_window_available=True,
            provider_history_complete=False,
            application_reconciliation_required=False,
        )

    return ResendWindowAssessment(
        action=ResendAction.RESET_AND_RECONCILE,
        disconnect_duration_microseconds=micros,
        resend_window_available=False,
        provider_history_complete=False,
        application_reconciliation_required=True,
    )


@dataclass(frozen=True, slots=True)
class ProphetXFixExecutionObservation:
    """Provider-origin execution identity/provenance, not an execution ledger."""

    environment: str
    economic_account_ref: str
    session_identity: ProphetXFixSessionIdentity
    stream: FixStream
    msg_seq_num: int
    exec_id: str
    provider_order_id: str
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_identity, ProphetXFixSessionIdentity):
            raise TypeError("session_identity must be ProphetXFixSessionIdentity")
        if not isinstance(self.stream, FixStream):
            raise TypeError("stream must be FixStream")
        object.__setattr__(self, "environment", _require_token(self.environment, "environment"))
        object.__setattr__(
            self,
            "economic_account_ref",
            _require_token(self.economic_account_ref, "economic_account_ref"),
        )
        object.__setattr__(
            self, "msg_seq_num", _require_positive_seq(self.msg_seq_num, "msg_seq_num")
        )
        object.__setattr__(self, "exec_id", _require_token(self.exec_id, "exec_id"))
        object.__setattr__(
            self,
            "provider_order_id",
            _require_token(self.provider_order_id, "provider_order_id"),
        )
        object.__setattr__(
            self,
            "semantic_fingerprint",
            _require_token(self.semantic_fingerprint, "semantic_fingerprint"),
        )
        if self.environment != self.session_identity.environment:
            raise ValueError(
                "execution observation environment must match session identity environment"
            )

    @property
    def economic_exec_key(self) -> tuple[str, str, str]:
        return (self.environment, self.economic_account_ref, self.exec_id)

    @property
    def authorizes_economic_action(self) -> bool:
        return False


def compare_execution_observations(
    first: ProphetXFixExecutionObservation,
    second: ProphetXFixExecutionObservation,
) -> ExecObservationRelation:
    """Compare provider observations across independent delivery streams.

    MsgSeqNum and stream/session provenance deliberately do not define a second
    economic event. The same ExecID in the same environment/account is
    idempotent only when provider order identity and the semantic fingerprint
    are identical.
    """

    if first.economic_exec_key != second.economic_exec_key:
        return ExecObservationRelation.DISTINCT
    if (
        first.provider_order_id == second.provider_order_id
        and first.semantic_fingerprint == second.semantic_fingerprint
    ):
        return ExecObservationRelation.IDEMPOTENT_DUPLICATE
    return ExecObservationRelation.CONFLICT


def compare_sequence_progress(
    first: ProphetXFixSequenceCheckpoint,
    second: ProphetXFixSequenceCheckpoint,
) -> int:
    """Compare next inbound sequence only inside one exact stream/session.

    Primary and drop-copy MsgSeqNum spaces are independent and therefore
    intentionally incomparable.
    """

    if not first.accepts_sequence_comparison_with(second):
        raise ValueError(
            "sequence checkpoints are incomparable across session identities or streams"
        )
    return (
        (first.next_expected_inbound_seq_num > second.next_expected_inbound_seq_num)
        - (first.next_expected_inbound_seq_num < second.next_expected_inbound_seq_num)
    )
