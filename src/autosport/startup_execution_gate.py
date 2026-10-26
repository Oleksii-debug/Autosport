from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from autosport.real_execution_ledger import AttemptState, EventType, RealExecutionLedger


class StartupExecutionBlockReason(str, Enum):
    LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"
    PROVIDER_RECONCILIATION_UNAVAILABLE = "PROVIDER_RECONCILIATION_UNAVAILABLE"
    PROVIDER_RECONCILIATION_FAILED = "PROVIDER_RECONCILIATION_FAILED"
    UNRESOLVED_EXTERNAL_EFFECTS = "UNRESOLVED_EXTERNAL_EFFECTS"
    ACCOUNT_STATE_REBUILD_UNAVAILABLE = "ACCOUNT_STATE_REBUILD_UNAVAILABLE"
    ACCOUNT_STATE_REBUILD_FAILED = "ACCOUNT_STATE_REBUILD_FAILED"
    ACCOUNT_STATE_INVALID = "ACCOUNT_STATE_INVALID"
    ACCOUNT_STATE_AUTHORITY_UNAVAILABLE = "ACCOUNT_STATE_AUTHORITY_UNAVAILABLE"


def _nonempty_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _nonnegative_decimal(value: Decimal | str | int, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return parsed


def _aware_timestamp(value: str, name: str) -> str:
    _nonempty_text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionAccount:
    bookmaker_id: str
    account_id: str

    def __post_init__(self) -> None:
        _nonempty_text(self.bookmaker_id, "bookmaker_id")
        _nonempty_text(self.account_id, "account_id")


@dataclass(frozen=True, slots=True)
class AccountExposureSnapshot:
    """Structural account-state projection for diagnostics only.

    This value is intentionally caller-constructible and therefore cannot grant
    positive startup execution authority. A future positive path must re-resolve
    product-issued provider/account-read evidence with explicit freshness.
    """

    bookmaker_id: str
    account_id: str
    currency: str
    available_bankroll: Decimal | str | int
    open_exposure: Decimal | str | int
    observed_at: str

    def __post_init__(self) -> None:
        _nonempty_text(self.bookmaker_id, "bookmaker_id")
        _nonempty_text(self.account_id, "account_id")
        _nonempty_text(self.currency, "currency")
        object.__setattr__(
            self,
            "available_bankroll",
            _nonnegative_decimal(self.available_bankroll, "available_bankroll"),
        )
        object.__setattr__(
            self,
            "open_exposure",
            _nonnegative_decimal(self.open_exposure, "open_exposure"),
        )
        _aware_timestamp(self.observed_at, "observed_at")

    @property
    def account(self) -> ExecutionAccount:
        return ExecutionAccount(self.bookmaker_id, self.account_id)


@dataclass(frozen=True, slots=True)
class StartupExecutionStatus:
    execution_enabled: bool
    analysis_read_only_enabled: bool
    reason: StartupExecutionBlockReason | None
    promoted_attempt_ids: tuple[str, ...]
    unresolved_attempt_ids: tuple[str, ...]
    account_snapshots: tuple[AccountExposureSnapshot, ...]


ReconcileUnresolved = Callable[[tuple[str, ...]], None]
RebuildAccountState = Callable[[], Iterable[AccountExposureSnapshot]]


class StartupExecutionGate:
    """Fail-closed startup authority for externally effectful execution.

    The gate deliberately has no provider write capability. It first asks the
    durable execution ledger to recover crash-interrupted attempts to UNKNOWN,
    then requires provider reconciliation to make every external effect
    definitive. AccountExposureSnapshot remains useful diagnostic state but is
    not product-issued provider authority, so it cannot enable execution.

    Positive startup admission remains disabled until Autosport has a
    product-owned account-read resolver with explicit freshness plus an atomic
    ledger-generation handoff to the execution writer.
    """

    _UNRESOLVED_STATES = frozenset(
        {AttemptState.RESERVED, AttemptState.SUBMITTED, AttemptState.UNKNOWN}
    )

    def __init__(
        self,
        *,
        ledger: RealExecutionLedger,
        expected_accounts: Iterable[ExecutionAccount],
        reconcile_unresolved: ReconcileUnresolved | None,
        rebuild_account_state: RebuildAccountState | None,
    ) -> None:
        accounts = tuple(expected_accounts)
        if not accounts:
            raise ValueError("expected_accounts must contain at least one account")
        if any(not isinstance(account, ExecutionAccount) for account in accounts):
            raise TypeError("expected_accounts must contain ExecutionAccount values")
        if len(set(accounts)) != len(accounts):
            raise ValueError("expected_accounts must not contain duplicates")
        self._ledger = ledger
        self._expected_accounts = accounts
        self._reconcile_unresolved = reconcile_unresolved
        self._rebuild_account_state = rebuild_account_state

    def evaluate(self) -> StartupExecutionStatus:
        promoted: tuple[str, ...] = ()
        try:
            promoted = self._ledger.recover_uncertain()
            unresolved = self._unresolved_attempt_ids()
        except Exception:
            return self._blocked(
                StartupExecutionBlockReason.LEDGER_UNAVAILABLE,
                promoted=promoted,
            )

        if unresolved:
            if self._reconcile_unresolved is None:
                return self._blocked(
                    StartupExecutionBlockReason.PROVIDER_RECONCILIATION_UNAVAILABLE,
                    promoted=promoted,
                    unresolved=unresolved,
                )
            try:
                self._reconcile_unresolved(unresolved)
            except Exception:
                return self._blocked(
                    StartupExecutionBlockReason.PROVIDER_RECONCILIATION_FAILED,
                    promoted=promoted,
                    unresolved=unresolved,
                )
            try:
                unresolved = self._unresolved_attempt_ids()
            except Exception:
                return self._blocked(
                    StartupExecutionBlockReason.LEDGER_UNAVAILABLE,
                    promoted=promoted,
                )
            if unresolved:
                return self._blocked(
                    StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
                    promoted=promoted,
                    unresolved=unresolved,
                )

        if self._rebuild_account_state is None:
            return self._blocked(
                StartupExecutionBlockReason.ACCOUNT_STATE_REBUILD_UNAVAILABLE,
                promoted=promoted,
            )
        try:
            snapshots = tuple(self._rebuild_account_state())
        except Exception:
            return self._blocked(
                StartupExecutionBlockReason.ACCOUNT_STATE_REBUILD_FAILED,
                promoted=promoted,
            )
        if not self._valid_account_snapshots(snapshots):
            return self._blocked(
                StartupExecutionBlockReason.ACCOUNT_STATE_INVALID,
                promoted=promoted,
            )

        # Re-read canonical ledger events after the point-in-time snapshot. This
        # catches an append that lands immediately after verified_snapshot()
        # returns its bytes (the #1019 race). A still-later append cannot become
        # unsafe startup authority because positive admission is disabled below.
        try:
            unresolved = self._unresolved_attempt_ids()
        except Exception:
            return self._blocked(
                StartupExecutionBlockReason.LEDGER_UNAVAILABLE,
                promoted=promoted,
            )
        if unresolved:
            return self._blocked(
                StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
                promoted=promoted,
                unresolved=unresolved,
                snapshots=snapshots,
            )

        # AccountExposureSnapshot is a structural projection, not product-issued
        # provider evidence. Do not convert caller-controlled values or timestamps
        # into execution authority. A later positive implementation must consume
        # a non-caller-mintable provider/account resolver, enforce freshness, and
        # atomically bind the admitted ledger generation to the execution writer.
        return self._blocked(
            StartupExecutionBlockReason.ACCOUNT_STATE_AUTHORITY_UNAVAILABLE,
            promoted=promoted,
            snapshots=snapshots,
        )

    def _unresolved_attempt_ids(self) -> tuple[str, ...]:
        # First force the public integrity snapshot boundary. Then derive both
        # attempt identity and state from one fresh canonical event read instead
        # of mixing stale snapshot IDs with later per-attempt live reads.
        self._ledger.verified_snapshot()
        events = self._ledger._events()
        attempt_ids: list[str] = []
        seen: set[str] = set()
        for event in events:
            if event["event_type"] != EventType.ATTEMPT_RESERVED.value:
                continue
            attempt_id = event["attempt_id"]
            if attempt_id not in seen:
                attempt_ids.append(attempt_id)
                seen.add(attempt_id)
        return tuple(
            attempt_id
            for attempt_id in attempt_ids
            if RealExecutionLedger._state(
                RealExecutionLedger._attempt_events(events, attempt_id)
            )
            in self._UNRESOLVED_STATES
        )

    def _valid_account_snapshots(
        self, snapshots: tuple[AccountExposureSnapshot, ...]
    ) -> bool:
        if any(not isinstance(item, AccountExposureSnapshot) for item in snapshots):
            return False
        accounts = tuple(item.account for item in snapshots)
        if len(set(accounts)) != len(accounts):
            return False
        return set(accounts) == set(self._expected_accounts)

    @staticmethod
    def _blocked(
        reason: StartupExecutionBlockReason,
        *,
        promoted: tuple[str, ...] = (),
        unresolved: tuple[str, ...] = (),
        snapshots: tuple[AccountExposureSnapshot, ...] = (),
    ) -> StartupExecutionStatus:
        return StartupExecutionStatus(
            execution_enabled=False,
            analysis_read_only_enabled=True,
            reason=reason,
            promoted_attempt_ids=promoted,
            unresolved_attempt_ids=unresolved,
            account_snapshots=snapshots,
        )
