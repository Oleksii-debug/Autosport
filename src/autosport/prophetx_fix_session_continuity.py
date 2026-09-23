"""Durable, fail-closed ProphetX FIX session continuity evidence.

This module owns transport-session continuity facts only.  It does not open FIX
connections, store credentials, submit orders, mutate the canonical execution
ledger, infer settlement, or authorize an economic effect.  Sequence reset and
resend decisions are *transport recovery plans*; any unresolved order/economic
state remains the responsibility of canonical execution reconciliation.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Final

_PROTOCOL: Final = "prophetx-fix-session-continuity-v1"
_SCHEMA_VERSION: Final = 1
_MAX_SEQUENCE: Final = 2_147_483_647
_PROVIDER_RESEND_WINDOW: Final = timedelta(days=7)


class ProphetXFixContinuityError(RuntimeError):
    """Base error for ProphetX FIX continuity."""


class ProphetXFixContractError(ProphetXFixContinuityError):
    """Input or durable-state contract violation."""


class ProphetXFixEvidenceConflict(ProphetXFixContinuityError):
    """Conflicting evidence reused an identity that must be immutable."""


class ProphetXFixCheckpointMissing(ProphetXFixContractError):
    """No durable checkpoint exists for the exact FIX session identity."""


class ProphetXFixEnvironment(str, Enum):
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class ProphetXFixStream(str, Enum):
    PRIMARY = "PRIMARY"
    DROP_COPY = "DROP_COPY"


class ReconnectDisposition(str, Enum):
    RESUME = "RESUME"
    RESEND_REQUIRED = "RESEND_REQUIRED"
    RESET_PROVIDER_SEQUENCE_LOWER = "RESET_PROVIDER_SEQUENCE_LOWER"
    RESET_LOCAL_STORE_LOST = "RESET_LOCAL_STORE_LOST"
    RESET_RESEND_WINDOW_EXCEEDED = "RESET_RESEND_WINDOW_EXCEEDED"


class ExecutionReportDisposition(str, Enum):
    FIRST_SEEN = "FIRST_SEEN"
    DUPLICATE_SAME_STREAM = "DUPLICATE_SAME_STREAM"
    DUPLICATE_CROSS_STREAM = "DUPLICATE_CROSS_STREAM"


@dataclass(frozen=True, slots=True)
class FixSessionIdentity:
    environment: ProphetXFixEnvironment
    stream: ProphetXFixStream
    begin_string: str
    sender_comp_id: str
    target_comp_id: str
    credential_identity_sha256: str

    def __post_init__(self) -> None:
        if type(self.environment) is not ProphetXFixEnvironment:
            raise ProphetXFixContractError("environment must be canonical ProphetXFixEnvironment")
        if type(self.stream) is not ProphetXFixStream:
            raise ProphetXFixContractError("stream must be canonical ProphetXFixStream")
        _ascii_token(self.begin_string, "begin_string", 32)
        if not self.begin_string.startswith(("FIX.", "FIXT.")):
            raise ProphetXFixContractError("begin_string must identify a canonical FIX protocol")
        _ascii_token(self.sender_comp_id, "sender_comp_id", 128)
        _ascii_token(self.target_comp_id, "target_comp_id", 128)
        _digest(self.credential_identity_sha256, "credential_identity_sha256")

    @property
    def canonical_payload(self) -> dict[str, str]:
        return {
            "environment": self.environment.value,
            "stream": self.stream.value,
            "begin_string": self.begin_string,
            "sender_comp_id": self.sender_comp_id,
            "target_comp_id": self.target_comp_id,
            "credential_identity_sha256": self.credential_identity_sha256,
        }

    @property
    def session_key(self) -> str:
        return _hash_json(f"{_PROTOCOL}.session", self.canonical_payload)

    @property
    def contains_secret_material(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class FixSequenceCheckpoint:
    identity: FixSessionIdentity
    next_expected_inbound: int
    next_outbound: int
    last_durable_inbound: int
    reset_epoch: int
    revision: int
    application_reconciliation_required: bool
    updated_at: str

    def __post_init__(self) -> None:
        if type(self.identity) is not FixSessionIdentity:
            raise ProphetXFixContractError("checkpoint identity must be canonical")
        _sequence(self.next_expected_inbound, "next_expected_inbound")
        _sequence(self.next_outbound, "next_outbound")
        _nonnegative_int(self.last_durable_inbound, "last_durable_inbound")
        if self.last_durable_inbound != self.next_expected_inbound - 1:
            raise ProphetXFixContractError(
                "last_durable_inbound must equal next_expected_inbound - 1"
            )
        _nonnegative_int(self.reset_epoch, "reset_epoch")
        _positive_int(self.revision, "revision")
        if type(self.application_reconciliation_required) is not bool:
            raise ProphetXFixContractError(
                "application_reconciliation_required must be an exact bool"
            )
        _time(self.updated_at, "updated_at")

    @property
    def transport_continuity_only(self) -> bool:
        return True

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def application_completeness_proven(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class FixReconnectPlan:
    identity: FixSessionIdentity
    checkpoint_revision: int | None
    disposition: ReconnectDisposition
    provider_logon_msg_seq_num: int
    reset_seq_num_flag_candidate: bool
    resend_begin_seq: int | None
    resend_end_seq: int | None
    application_reconciliation_required: bool
    venue_reset_notice_seen: bool
    observed_at: str

    def __post_init__(self) -> None:
        if type(self.identity) is not FixSessionIdentity:
            raise ProphetXFixContractError("reconnect identity must be canonical")
        if type(self.disposition) is not ReconnectDisposition:
            raise ProphetXFixContractError("reconnect disposition must be canonical")
        if self.checkpoint_revision is not None:
            _positive_int(self.checkpoint_revision, "checkpoint_revision")
        _sequence(self.provider_logon_msg_seq_num, "provider_logon_msg_seq_num")
        if type(self.reset_seq_num_flag_candidate) is not bool:
            raise ProphetXFixContractError("reset_seq_num_flag_candidate must be bool")
        if type(self.application_reconciliation_required) is not bool:
            raise ProphetXFixContractError("application_reconciliation_required must be bool")
        if type(self.venue_reset_notice_seen) is not bool:
            raise ProphetXFixContractError("venue_reset_notice_seen must be bool")
        _time(self.observed_at, "observed_at")
        if (self.resend_begin_seq is None) != (self.resend_end_seq is None):
            raise ProphetXFixContractError("resend range must be complete or absent")
        if self.resend_begin_seq is not None:
            _sequence(self.resend_begin_seq, "resend_begin_seq")
            _sequence(self.resend_end_seq, "resend_end_seq")
            if self.resend_begin_seq > self.resend_end_seq:
                raise ProphetXFixContractError("resend range is reversed")
        if self.disposition is ReconnectDisposition.RESEND_REQUIRED:
            if self.resend_begin_seq is None or self.reset_seq_num_flag_candidate:
                raise ProphetXFixContractError("RESEND_REQUIRED plan shape is invalid")
        elif self.resend_begin_seq is not None:
            raise ProphetXFixContractError("non-resend plan cannot carry a resend range")
        reset_dispositions = {
            ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER,
            ReconnectDisposition.RESET_LOCAL_STORE_LOST,
            ReconnectDisposition.RESET_RESEND_WINDOW_EXCEEDED,
        }
        if self.reset_seq_num_flag_candidate != (self.disposition in reset_dispositions):
            raise ProphetXFixContractError("reset flag candidate contradicts disposition")

    @property
    def next_expected_msg_seq_num_789_authoritative(self) -> bool:
        return False

    @property
    def economic_state_complete(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ExecutionReportObservation:
    identity: FixSessionIdentity
    msg_seq_num: int
    exec_id: str
    client_order_id: str
    provider_order_id: str
    transact_time: str
    economic_payload_sha256: str
    observed_at: str

    def __post_init__(self) -> None:
        if type(self.identity) is not FixSessionIdentity:
            raise ProphetXFixContractError("report identity must be canonical")
        _sequence(self.msg_seq_num, "msg_seq_num")
        _text(self.exec_id, "exec_id", 256)
        _text(self.client_order_id, "client_order_id", 256)
        _text(self.provider_order_id, "provider_order_id", 256)
        _time(self.transact_time, "transact_time")
        _digest(self.economic_payload_sha256, "economic_payload_sha256")
        _time(self.observed_at, "observed_at")
        if _time(self.transact_time, "transact_time") > _time(self.observed_at, "observed_at"):
            raise ProphetXFixContractError("future execution report transact_time is invalid")

    @property
    def semantic_sha256(self) -> str:
        return _hash_json(
            f"{_PROTOCOL}.exec-report",
            {
                "environment": self.identity.environment.value,
                "exec_id": self.exec_id,
                "client_order_id": self.client_order_id,
                "provider_order_id": self.provider_order_id,
                "transact_time": _canonical_time(self.transact_time, "transact_time"),
                "economic_payload_sha256": self.economic_payload_sha256,
            },
        )

    @property
    def economic_event_key(self) -> str:
        return _hash_json(
            f"{_PROTOCOL}.economic-event",
            {
                "environment": self.identity.environment.value,
                "exec_id": self.exec_id,
            },
        )


@dataclass(frozen=True, slots=True)
class ExecutionReportRecord:
    disposition: ExecutionReportDisposition
    economic_event_key: str
    semantic_sha256: str
    provenance_observation_count: int
    distinct_stream_count: int

    @property
    def economic_application_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False


class ProphetXFixContinuityStore:
    """SQLite-backed durable session/sequence and ExecID continuity evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.name:
            raise ProphetXFixContractError("continuity store path must name a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    ) STRICT;
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_key TEXT PRIMARY KEY,
                        identity_json TEXT NOT NULL,
                        next_expected_inbound INTEGER NOT NULL,
                        next_outbound INTEGER NOT NULL,
                        last_durable_inbound INTEGER NOT NULL,
                        reset_epoch INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        reconciliation_required INTEGER NOT NULL CHECK (reconciliation_required IN (0, 1)),
                        updated_at TEXT NOT NULL
                    ) STRICT;
                    CREATE TABLE IF NOT EXISTS pruned_ranges (
                        session_key TEXT NOT NULL,
                        reset_epoch INTEGER NOT NULL,
                        begin_seq INTEGER NOT NULL,
                        end_seq INTEGER NOT NULL,
                        evidence_sha256 TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY (session_key, reset_epoch, begin_seq, end_seq),
                        FOREIGN KEY (session_key) REFERENCES sessions(session_key)
                    ) STRICT;
                    CREATE TABLE IF NOT EXISTS execution_events (
                        economic_event_key TEXT PRIMARY KEY,
                        semantic_sha256 TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        exec_id TEXT NOT NULL,
                        client_order_id TEXT NOT NULL,
                        provider_order_id TEXT NOT NULL,
                        transact_time TEXT NOT NULL,
                        economic_payload_sha256 TEXT NOT NULL
                    ) STRICT;
                    CREATE TABLE IF NOT EXISTS report_provenance (
                        economic_event_key TEXT NOT NULL,
                        session_key TEXT NOT NULL,
                        reset_epoch INTEGER NOT NULL,
                        msg_seq_num INTEGER NOT NULL,
                        stream TEXT NOT NULL,
                        observation_sha256 TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY (session_key, reset_epoch, msg_seq_num),
                        FOREIGN KEY (economic_event_key) REFERENCES execution_events(economic_event_key),
                        FOREIGN KEY (session_key) REFERENCES sessions(session_key)
                    ) STRICT;
                    CREATE TABLE IF NOT EXISTS reset_plan_authority (
                        session_key TEXT PRIMARY KEY,
                        checkpoint_revision INTEGER,
                        plan_sha256 TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    ) STRICT;
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                        (str(_SCHEMA_VERSION),),
                    )
                elif row["value"] != str(_SCHEMA_VERSION):
                    raise ProphetXFixContractError("unsupported continuity schema version")
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        self.verify_integrity()

    def initialize_session(
        self,
        identity: FixSessionIdentity,
        *,
        next_expected_inbound: int = 1,
        next_outbound: int = 1,
        observed_at: str,
    ) -> FixSequenceCheckpoint:
        _identity(identity)
        _sequence(next_expected_inbound, "next_expected_inbound")
        _sequence(next_outbound, "next_outbound")
        observed = _canonical_time(observed_at, "observed_at")
        payload = _identity_json(identity)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key=?", (identity.session_key,)
                ).fetchone()
                if row is not None:
                    checkpoint = self._checkpoint_from_row(row)
                    if checkpoint.identity != identity:
                        raise ProphetXFixEvidenceConflict("session key identity collision")
                    return checkpoint
                conn.execute(
                    """INSERT INTO sessions(
                        session_key, identity_json, next_expected_inbound, next_outbound,
                        last_durable_inbound, reset_epoch, revision,
                        reconciliation_required, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 1, 0, ?)""",
                    (
                        identity.session_key,
                        payload,
                        next_expected_inbound,
                        next_outbound,
                        next_expected_inbound - 1,
                        observed,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.load_checkpoint(identity)

    def load_checkpoint(self, identity: FixSessionIdentity) -> FixSequenceCheckpoint:
        _identity(identity)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_key=?", (identity.session_key,)
            ).fetchone()
        if row is None:
            raise ProphetXFixCheckpointMissing("FIX session has no durable checkpoint")
        checkpoint = self._checkpoint_from_row(row)
        if checkpoint.identity != identity:
            raise ProphetXFixEvidenceConflict("stored FIX session identity changed")
        return checkpoint

    def checkpoint_sequences(
        self,
        identity: FixSessionIdentity,
        *,
        expected_revision: int,
        next_expected_inbound: int,
        next_outbound: int,
        observed_at: str,
    ) -> FixSequenceCheckpoint:
        _identity(identity)
        _positive_int(expected_revision, "expected_revision")
        _sequence(next_expected_inbound, "next_expected_inbound")
        _sequence(next_outbound, "next_outbound")
        observed = _canonical_time(observed_at, "observed_at")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key=?", (identity.session_key,)
                ).fetchone()
                if row is None:
                    raise ProphetXFixContractError("FIX session has no durable checkpoint")
                current = self._checkpoint_from_row(row)
                if current.identity != identity:
                    raise ProphetXFixEvidenceConflict("stored FIX session identity changed")
                if current.revision != expected_revision:
                    raise ProphetXFixEvidenceConflict("stale checkpoint revision")
                if next_expected_inbound < current.next_expected_inbound:
                    raise ProphetXFixEvidenceConflict("inbound sequence rollback")
                if next_outbound < current.next_outbound:
                    raise ProphetXFixEvidenceConflict("outbound sequence rollback")
                if _time(observed, "observed_at") < _time(current.updated_at, "updated_at"):
                    raise ProphetXFixEvidenceConflict("checkpoint observation time rollback")
                conn.execute(
                    """UPDATE sessions
                       SET next_expected_inbound=?, next_outbound=?,
                           last_durable_inbound=?, revision=revision+1, updated_at=?
                       WHERE session_key=? AND revision=?""",
                    (
                        next_expected_inbound,
                        next_outbound,
                        next_expected_inbound - 1,
                        observed,
                        identity.session_key,
                        expected_revision,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ProphetXFixEvidenceConflict("checkpoint changed concurrently")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.load_checkpoint(identity)

    def plan_reconnect(
        self,
        identity: FixSessionIdentity,
        *,
        provider_logon_msg_seq_num: int,
        observed_at: str,
        disconnected_since: str | None,
        local_sequence_store_lost: bool = False,
        venue_reset_notice_seen: bool = False,
    ) -> FixReconnectPlan:
        _identity(identity)
        _sequence(provider_logon_msg_seq_num, "provider_logon_msg_seq_num")
        observed = _canonical_time(observed_at, "observed_at")
        if type(local_sequence_store_lost) is not bool:
            raise ProphetXFixContractError("local_sequence_store_lost must be bool")
        if type(venue_reset_notice_seen) is not bool:
            raise ProphetXFixContractError("venue_reset_notice_seen must be bool")
        try:
            checkpoint = self.load_checkpoint(identity)
        except ProphetXFixCheckpointMissing as exc:
            if local_sequence_store_lost:
                raise ProphetXFixContractError(
                    "local sequence-store loss requires product-owned recovery incident authority"
                ) from exc
            raise

        if local_sequence_store_lost:
            raise ProphetXFixContractError(
                "caller local_sequence_store_lost flag cannot authorize sequence reset"
            )

        if disconnected_since is not None:
            disconnected = _canonical_time(disconnected_since, "disconnected_since")
            if _time(disconnected, "disconnected_since") > _time(observed, "observed_at"):
                raise ProphetXFixContractError("disconnected_since is in the future")
            if (
                _time(observed, "observed_at") - _time(disconnected, "disconnected_since")
                > _PROVIDER_RESEND_WINDOW
            ):
                plan = FixReconnectPlan(
                    identity,
                    checkpoint.revision,
                    ReconnectDisposition.RESET_RESEND_WINDOW_EXCEEDED,
                    provider_logon_msg_seq_num,
                    True,
                    None,
                    None,
                    True,
                    venue_reset_notice_seen,
                    observed,
                )
                return self._publish_reconnect_plan(plan)

        expected = checkpoint.next_expected_inbound
        if provider_logon_msg_seq_num < expected:
            plan = FixReconnectPlan(
                identity,
                checkpoint.revision,
                ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER,
                provider_logon_msg_seq_num,
                True,
                None,
                None,
                True,
                venue_reset_notice_seen,
                observed,
            )
            return self._publish_reconnect_plan(plan)
        if provider_logon_msg_seq_num > expected:
            plan = FixReconnectPlan(
                identity,
                checkpoint.revision,
                ReconnectDisposition.RESEND_REQUIRED,
                provider_logon_msg_seq_num,
                False,
                expected,
                provider_logon_msg_seq_num - 1,
                checkpoint.application_reconciliation_required,
                venue_reset_notice_seen,
                observed,
            )
            return self._publish_reconnect_plan(plan)
        plan = FixReconnectPlan(
            identity,
            checkpoint.revision,
            ReconnectDisposition.RESUME,
            provider_logon_msg_seq_num,
            False,
            None,
            None,
            checkpoint.application_reconciliation_required,
            venue_reset_notice_seen,
            observed,
        )
        return self._publish_reconnect_plan(plan)

    def _publish_reconnect_plan(self, plan: FixReconnectPlan) -> FixReconnectPlan:
        if type(plan) is not FixReconnectPlan:
            raise ProphetXFixContractError(
                "reconnect plan authority requires canonical plan"
            )
        digest = _reconnect_plan_sha256(plan)
        observed = _canonical_time(plan.observed_at, "plan observed_at")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """SELECT plan_sha256, observed_at
                       FROM reset_plan_authority WHERE session_key=?""",
                    (plan.identity.session_key,),
                ).fetchone()
                if existing is not None:
                    old_time = _time(
                        existing["observed_at"], "stored reset plan observed_at"
                    )
                    new_time = _time(observed, "plan observed_at")
                    if old_time > new_time:
                        raise ProphetXFixEvidenceConflict(
                            "reconnect observation time rolls back reset authority"
                        )
                    if old_time == new_time:
                        if not plan.reset_seq_num_flag_candidate:
                            raise ProphetXFixEvidenceConflict(
                                "same-time reconnect facts conflict with reset authority"
                            )
                        if existing["plan_sha256"] != digest:
                            raise ProphetXFixEvidenceConflict(
                                "same-time reset authority has conflicting reconnect facts"
                            )
                if plan.reset_seq_num_flag_candidate:
                    conn.execute(
                        """INSERT INTO reset_plan_authority(
                            session_key, checkpoint_revision, plan_sha256, observed_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(session_key) DO UPDATE SET
                            checkpoint_revision=excluded.checkpoint_revision,
                            plan_sha256=excluded.plan_sha256,
                            observed_at=excluded.observed_at""",
                        (
                            plan.identity.session_key,
                            plan.checkpoint_revision,
                            digest,
                            observed,
                        ),
                    )
                else:
                    conn.execute(
                        "DELETE FROM reset_plan_authority WHERE session_key=?",
                        (plan.identity.session_key,),
                    )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return plan

    def record_reset(
        self,
        plan: FixReconnectPlan,
        *,
        observed_at: str,
    ) -> FixSequenceCheckpoint:
        if type(plan) is not FixReconnectPlan:
            raise ProphetXFixContractError("reset requires a canonical reconnect plan")
        if not plan.reset_seq_num_flag_candidate:
            raise ProphetXFixContractError("non-reset reconnect plan cannot reset sequence state")
        observed = _canonical_time(observed_at, "observed_at")
        if _time(observed, "observed_at") < _time(plan.observed_at, "plan observed_at"):
            raise ProphetXFixContractError("reset cannot precede reconnect plan")
        plan_digest = _reconnect_plan_sha256(plan)

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key=?", (plan.identity.session_key,)
                ).fetchone()
                if row is None:
                    raise ProphetXFixContractError(
                        "reset target session is missing; local sequence-store loss "
                        "requires product-owned recovery incident authority"
                    )
                current = self._checkpoint_from_row(row)
                if current.identity != plan.identity:
                    raise ProphetXFixEvidenceConflict("reset session identity changed")
                if plan.checkpoint_revision != current.revision:
                    raise ProphetXFixEvidenceConflict("reset plan is stale")

                authority = conn.execute(
                    """SELECT checkpoint_revision, plan_sha256, observed_at
                       FROM reset_plan_authority WHERE session_key=?""",
                    (plan.identity.session_key,),
                ).fetchone()
                if authority is None or authority["plan_sha256"] != plan_digest:
                    raise ProphetXFixEvidenceConflict(
                        "reset plan was not issued by the continuity store"
                    )
                if authority["checkpoint_revision"] != plan.checkpoint_revision:
                    raise ProphetXFixEvidenceConflict(
                        "reset plan authority revision does not match plan"
                    )
                if _canonical_time(
                    authority["observed_at"], "stored reset plan observed_at"
                ) != _canonical_time(plan.observed_at, "plan observed_at"):
                    raise ProphetXFixEvidenceConflict(
                        "reset plan authority observation does not match plan"
                    )

                conn.execute(
                    """UPDATE sessions
                       SET next_expected_inbound=1, next_outbound=1,
                           last_durable_inbound=0, reset_epoch=reset_epoch+1,
                           revision=revision+1, reconciliation_required=1,
                           updated_at=? WHERE session_key=? AND revision=?""",
                    (observed, plan.identity.session_key, current.revision),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ProphetXFixEvidenceConflict(
                        "reset checkpoint changed concurrently"
                    )

                conn.execute(
                    """DELETE FROM reset_plan_authority
                       WHERE session_key=? AND plan_sha256=?""",
                    (plan.identity.session_key, plan_digest),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ProphetXFixEvidenceConflict(
                        "reset plan authority changed concurrently"
                    )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return self.load_checkpoint(plan.identity)

    def record_pruned_gap(
        self,
        identity: FixSessionIdentity,
        *,
        begin_seq: int,
        end_seq: int,
        evidence_sha256: str,
        recorded_at: str,
    ) -> FixSequenceCheckpoint:
        _identity(identity)
        _sequence(begin_seq, "begin_seq")
        _sequence(end_seq, "end_seq")
        if begin_seq > end_seq:
            raise ProphetXFixContractError("pruned gap range is reversed")
        evidence = _digest(evidence_sha256, "evidence_sha256")
        recorded = _canonical_time(recorded_at, "recorded_at")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key=?", (identity.session_key,)
                ).fetchone()
                if row is None:
                    raise ProphetXFixContractError("pruned gap session is missing")
                checkpoint = self._checkpoint_from_row(row)
                if checkpoint.identity != identity:
                    raise ProphetXFixEvidenceConflict("pruned gap session identity changed")
                existing = conn.execute(
                    """SELECT evidence_sha256, recorded_at FROM pruned_ranges
                       WHERE session_key=? AND reset_epoch=? AND begin_seq=? AND end_seq=?""",
                    (identity.session_key, checkpoint.reset_epoch, begin_seq, end_seq),
                ).fetchone()
                if existing is not None:
                    if existing["evidence_sha256"] != evidence:
                        raise ProphetXFixEvidenceConflict("pruned gap evidence changed")
                else:
                    conn.execute(
                        """INSERT INTO pruned_ranges(
                            session_key, reset_epoch, begin_seq, end_seq,
                            evidence_sha256, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            identity.session_key,
                            checkpoint.reset_epoch,
                            begin_seq,
                            end_seq,
                            evidence,
                            recorded,
                        ),
                    )
                if not checkpoint.application_reconciliation_required:
                    conn.execute(
                        """UPDATE sessions
                           SET reconciliation_required=1, revision=revision+1, updated_at=?
                           WHERE session_key=? AND revision=?""",
                        (recorded, identity.session_key, checkpoint.revision),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        raise ProphetXFixEvidenceConflict("pruned gap checkpoint changed concurrently")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.load_checkpoint(identity)

    def record_execution_report(
        self, observation: ExecutionReportObservation
    ) -> ExecutionReportRecord:
        if type(observation) is not ExecutionReportObservation:
            raise ProphetXFixContractError("execution report observation must be canonical")
        identity = observation.identity
        session_key = identity.session_key
        semantic = observation.semantic_sha256
        event_key = observation.economic_event_key
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key=?", (session_key,)
                ).fetchone()
                if row is None:
                    raise ProphetXFixContractError("execution report session is missing")
                checkpoint = self._checkpoint_from_row(row)
                if checkpoint.identity != identity:
                    raise ProphetXFixEvidenceConflict("execution report session identity changed")
                observation_sha = _hash_json(
                    f"{_PROTOCOL}.report-provenance",
                    {
                        "session_key": session_key,
                        "reset_epoch": checkpoint.reset_epoch,
                        "stream": identity.stream.value,
                        "msg_seq_num": observation.msg_seq_num,
                        "exec_id": observation.exec_id,
                        "semantic_sha256": semantic,
                    },
                )

                seq_row = conn.execute(
                    """SELECT economic_event_key, observation_sha256
                       FROM report_provenance
                       WHERE session_key=? AND reset_epoch=? AND msg_seq_num=?""",
                    (session_key, checkpoint.reset_epoch, observation.msg_seq_num),
                ).fetchone()
                if seq_row is not None:
                    if (
                        seq_row["economic_event_key"] != event_key
                        or seq_row["observation_sha256"] != observation_sha
                    ):
                        raise ProphetXFixEvidenceConflict(
                            "same session/reset MsgSeqNum has conflicting execution evidence"
                        )

                event_row = conn.execute(
                    "SELECT * FROM execution_events WHERE economic_event_key=?", (event_key,)
                ).fetchone()
                first_seen = event_row is None
                if event_row is None:
                    conn.execute(
                        """INSERT INTO execution_events(
                            economic_event_key, semantic_sha256, environment,
                            exec_id, client_order_id,
                            provider_order_id, transact_time, economic_payload_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_key,
                            semantic,
                            identity.environment.value,
                            observation.exec_id,
                            observation.client_order_id,
                            observation.provider_order_id,
                            _canonical_time(observation.transact_time, "transact_time"),
                            observation.economic_payload_sha256,
                        ),
                    )
                elif event_row["semantic_sha256"] != semantic:
                    raise ProphetXFixEvidenceConflict(
                        "same ProphetX ExecID has conflicting semantic execution evidence"
                    )

                if seq_row is None:
                    conn.execute(
                        """INSERT INTO report_provenance(
                            economic_event_key, session_key, reset_epoch, msg_seq_num,
                            stream, observation_sha256, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_key,
                            session_key,
                            checkpoint.reset_epoch,
                            observation.msg_seq_num,
                            identity.stream.value,
                            observation_sha,
                            _canonical_time(observation.observed_at, "observed_at"),
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT stream FROM report_provenance WHERE economic_event_key=?",
                (event_key,),
            ).fetchall()
        streams = {row["stream"] for row in rows}
        if first_seen:
            disposition = ExecutionReportDisposition.FIRST_SEEN
        elif len(streams) > 1:
            disposition = ExecutionReportDisposition.DUPLICATE_CROSS_STREAM
        else:
            disposition = ExecutionReportDisposition.DUPLICATE_SAME_STREAM
        return ExecutionReportRecord(
            disposition,
            event_key,
            semantic,
            len(rows),
            len(streams),
        )

    def clear_application_reconciliation_requirement(self, *args: object, **kwargs: object) -> None:
        """Fail closed until a product-owned reconciliation resolver is composed.

        A caller-supplied digest is not evidence that unknown economic effects were
        reconciled, so this isolated continuity component cannot clear the flag.
        """

        raise ProphetXFixContractError(
            "application reconciliation requires a product-owned canonical resolver"
        )

    def verify_integrity(self) -> None:
        with closing(self._connect()) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ProphetXFixContractError("continuity SQLite integrity check failed")
            schema = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if schema is None or schema["value"] != str(_SCHEMA_VERSION):
                raise ProphetXFixContractError("continuity schema version is invalid")
            session_rows = conn.execute("SELECT * FROM sessions").fetchall()
            pruned_rows = conn.execute("SELECT * FROM pruned_ranges").fetchall()
            event_rows = conn.execute("SELECT * FROM execution_events").fetchall()
            provenance_rows = conn.execute("SELECT * FROM report_provenance").fetchall()
            reset_authority_rows = conn.execute(
                "SELECT * FROM reset_plan_authority"
            ).fetchall()

        checkpoints: dict[str, FixSequenceCheckpoint] = {}
        for row in session_rows:
            checkpoint = self._checkpoint_from_row(row)
            if checkpoint.identity.session_key != row["session_key"]:
                raise ProphetXFixEvidenceConflict("stored session key does not match identity")
            checkpoints[row["session_key"]] = checkpoint

        for row in pruned_rows:
            checkpoint = checkpoints.get(row["session_key"])
            if checkpoint is None:
                raise ProphetXFixEvidenceConflict("orphan pruned-gap session")
            _nonnegative_int(row["reset_epoch"], "stored pruned reset_epoch")
            if row["reset_epoch"] > checkpoint.reset_epoch:
                raise ProphetXFixEvidenceConflict("pruned gap references future reset epoch")
            _sequence(row["begin_seq"], "stored pruned begin_seq")
            _sequence(row["end_seq"], "stored pruned end_seq")
            if row["begin_seq"] > row["end_seq"]:
                raise ProphetXFixEvidenceConflict("stored pruned gap range is reversed")
            _digest(row["evidence_sha256"], "stored pruned evidence_sha256")
            _time(row["recorded_at"], "stored pruned recorded_at")

        events: dict[str, sqlite3.Row] = {}
        for row in event_rows:
            key = _hash_json(
                f"{_PROTOCOL}.economic-event",
                {
                    "environment": row["environment"],
                    "exec_id": row["exec_id"],
                },
            )
            if key != row["economic_event_key"]:
                raise ProphetXFixEvidenceConflict("stored economic event key is invalid")
            semantic = _hash_json(
                f"{_PROTOCOL}.exec-report",
                {
                    "environment": row["environment"],
                    "exec_id": row["exec_id"],
                    "client_order_id": row["client_order_id"],
                    "provider_order_id": row["provider_order_id"],
                    "transact_time": _canonical_time(row["transact_time"], "transact_time"),
                    "economic_payload_sha256": _digest(
                        row["economic_payload_sha256"], "economic_payload_sha256"
                    ),
                },
            )
            if semantic != row["semantic_sha256"]:
                raise ProphetXFixEvidenceConflict("stored execution semantic digest is invalid")
            events[key] = row

        for row in provenance_rows:
            checkpoint = checkpoints.get(row["session_key"])
            if checkpoint is None:
                raise ProphetXFixEvidenceConflict("orphan report provenance session")
            if row["economic_event_key"] not in events:
                raise ProphetXFixEvidenceConflict("orphan report provenance event")
            _sequence(row["msg_seq_num"], "stored msg_seq_num")
            _nonnegative_int(row["reset_epoch"], "stored reset_epoch")
            if row["reset_epoch"] > checkpoint.reset_epoch:
                raise ProphetXFixEvidenceConflict(
                    "report provenance references future reset epoch"
                )
            if row["stream"] != checkpoint.identity.stream.value:
                raise ProphetXFixEvidenceConflict(
                    "stored report stream does not match session identity"
                )
            observed = _canonical_time(row["observed_at"], "stored observed_at")
            stored_observation = _digest(
                row["observation_sha256"], "stored observation_sha256"
            )
            event = events[row["economic_event_key"]]
            expected_observation = _hash_json(
                f"{_PROTOCOL}.report-provenance",
                {
                    "session_key": row["session_key"],
                    "reset_epoch": row["reset_epoch"],
                    "stream": row["stream"],
                    "msg_seq_num": row["msg_seq_num"],
                    "exec_id": event["exec_id"],
                    "semantic_sha256": event["semantic_sha256"],
                },
            )
            if stored_observation != expected_observation:
                raise ProphetXFixEvidenceConflict(
                    "stored report provenance digest is invalid"
                )
            del observed

        for row in reset_authority_rows:
            session_key = _digest(
                row["session_key"], "stored reset authority session_key"
            )
            _digest(row["plan_sha256"], "stored reset authority plan_sha256")
            _time(row["observed_at"], "stored reset authority observed_at")
            revision = row["checkpoint_revision"]
            if revision is not None:
                _positive_int(revision, "stored reset authority checkpoint_revision")
            checkpoint = checkpoints.get(session_key)
            if checkpoint is None:
                raise ProphetXFixEvidenceConflict(
                    "orphan reset plan authority session"
                )
            if revision is None:
                raise ProphetXFixEvidenceConflict(
                    "reset plan authority is missing checkpoint revision"
                )
            if revision > checkpoint.revision:
                raise ProphetXFixEvidenceConflict(
                    "reset authority references future checkpoint revision"
                )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> FixSequenceCheckpoint:
        try:
            payload = json.loads(row["identity_json"])
            if type(payload) is not dict or set(payload) != {
                "environment",
                "stream",
                "begin_string",
                "sender_comp_id",
                "target_comp_id",
                "credential_identity_sha256",
            }:
                raise ValueError("identity field set")
            identity = FixSessionIdentity(
                ProphetXFixEnvironment(payload["environment"]),
                ProphetXFixStream(payload["stream"]),
                payload["begin_string"],
                payload["sender_comp_id"],
                payload["target_comp_id"],
                payload["credential_identity_sha256"],
            )
            return FixSequenceCheckpoint(
                identity,
                row["next_expected_inbound"],
                row["next_outbound"],
                row["last_durable_inbound"],
                row["reset_epoch"],
                row["revision"],
                bool(row["reconciliation_required"]),
                row["updated_at"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProphetXFixContractError("durable FIX checkpoint is malformed") from exc


def _identity(value: object) -> FixSessionIdentity:
    if type(value) is not FixSessionIdentity:
        raise ProphetXFixContractError("session identity must be canonical FixSessionIdentity")
    return value


def _reconnect_plan_sha256(plan: FixReconnectPlan) -> str:
    if type(plan) is not FixReconnectPlan:
        raise ProphetXFixContractError("reconnect plan digest requires canonical plan")
    return _hash_json(
        f"{_PROTOCOL}.reconnect-plan-authority",
        {
            "identity": plan.identity.canonical_payload,
            "checkpoint_revision": plan.checkpoint_revision,
            "disposition": plan.disposition.value,
            "provider_logon_msg_seq_num": plan.provider_logon_msg_seq_num,
            "reset_seq_num_flag_candidate": plan.reset_seq_num_flag_candidate,
            "resend_begin_seq": plan.resend_begin_seq,
            "resend_end_seq": plan.resend_end_seq,
            "application_reconciliation_required": (
                plan.application_reconciliation_required
            ),
            "venue_reset_notice_seen": plan.venue_reset_notice_seen,
            "observed_at": _canonical_time(plan.observed_at, "plan observed_at"),
        },
    )


def _identity_json(identity: FixSessionIdentity) -> str:
    return json.dumps(
        identity.canonical_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_json(domain: str, payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(domain.encode("ascii") + b"\x00" + raw).hexdigest()


def _ascii_token(value: object, field: str, max_len: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_len
        or not value.isascii()
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ProphetXFixContractError(f"{field} must be bounded canonical ASCII text")
    return value


def _text(value: object, field: str, max_len: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_len:
        raise ProphetXFixContractError(f"{field} must be bounded canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProphetXFixContractError(f"{field} must be valid UTF-8") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProphetXFixContractError(f"{field} contains forbidden control text")
    return value


def _digest(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ProphetXFixContractError(f"{field} must be lowercase SHA-256")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ProphetXFixContractError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProphetXFixContractError(f"{field} must be a non-negative integer")
    return value


def _sequence(value: object, field: str) -> int:
    result = _positive_int(value, field)
    if result > _MAX_SEQUENCE:
        raise ProphetXFixContractError(f"{field} exceeds supported FIX sequence range")
    return result


def _time(value: object, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProphetXFixContractError(f"{field} must be timezone-aware ISO-8601 text")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProphetXFixContractError(
            f"{field} must be timezone-aware ISO-8601 text"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXFixContractError(f"{field} must be timezone-aware ISO-8601 text")
    return parsed.astimezone(timezone.utc)


def _canonical_time(value: object, field: str) -> str:
    return _time(value, field).isoformat()
