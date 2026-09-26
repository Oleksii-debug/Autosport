from __future__ import annotations

from pathlib import Path

from .paper import PaperBook
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from .paper_execution_reality import PaperAttemptOutcome


_ORIGINAL_PREPARE = PaperExecutionAdoptionRuntime.prepare
_ORIGINAL_EXECUTE = PaperExecutionAdoptionRuntime.execute
_ORIGINAL_TICKET_MATCHES_ATTEMPT = PaperExecutionAdoptionRuntime._ticket_matches_attempt
_LIVE_DECISION_PREFIX = "live-"
_PRE_ACTION_BOOK_FILE_NAME = "live_decision_pre_action_book.json"
_ACCEPTED_EQUIVALENT = frozenset(
    {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
)


def _exact_ticket_matches_attempt(
    *,
    ticket,
    attempt,
    action,
    binding,
) -> bool:
    """Require one exact attempt-id token before accepting durable exposure identity."""
    if type(ticket.strategy_reason) is not str:
        return False
    marker_prefix = PaperExecutionAdoptionRuntime._TICKET_MARKER
    expected_marker = f"{marker_prefix}{attempt.attempt_id}"
    marker_tokens = tuple(
        token
        for token in (part.strip() for part in ticket.strategy_reason.split(";"))
        if token.startswith(marker_prefix)
    )
    if marker_tokens != (expected_marker,):
        return False
    return _ORIGINAL_TICKET_MATCHES_ATTEMPT(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )


def _load_pre_action_path(path: Path, *, label: str) -> PaperBook | None:
    if not path.exists():
        return None
    try:
        return PaperBook.load(path)
    except (OSError, TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(f"{label} pre-action PaperBook is unreadable") from exc


def _load_live_pre_action_book(runtime: PaperExecutionAdoptionRuntime) -> PaperBook | None:
    """Return the exact durable pre-action witness for a persistent live decision."""
    path = Path(runtime.paper_book_path).parent / _PRE_ACTION_BOOK_FILE_NAME
    return _load_pre_action_path(path, label="live recovery")


def _exact_assert_recoverable_book_state(
    self: PaperExecutionAdoptionRuntime,
    *,
    pre_action_book: PaperBook,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
) -> None:
    """Accept only baseline or the exact #623-authorized durable exposure delta.

    Ticket ids are intentionally generated UUIDs, so recovery cannot reconstruct a
    post-action PaperBook by opening fresh synthetic tickets and comparing dicts.
    Instead, preserve the complete pre-action state by identity and verify every
    added durable ticket against its exact #623 attempt marker and execution truth.
    """
    if not isinstance(pre_action_book, PaperBook):
        raise TypeError("pre_action_book must be PaperBook")
    if not isinstance(prepared, PreparedPaperExecution):
        raise TypeError("prepared must be PreparedPaperExecution")
    self._require_minted(prepared)
    if type(materialize_exposure) is not bool:
        raise TypeError("materialize_exposure must be bool")

    if self._same_book_state(self.book, pre_action_book):
        return
    if not materialize_exposure:
        raise PaperExecutionAdoptionError(
            "SHADOW recovery PaperBook differs from exact pre-action state"
        )

    run_id = self.expected_run_id(prepared, trigger_id)
    run = self.ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=prepared.execution_plan,
        config=self.config,
        started_at=started_at,
        observation_evidence_ids={},
    )
    if run is None:
        raise PaperExecutionAdoptionError(
            "PaperBook changed before any durable #623 run evidence"
        )

    if self.book.initial_bankroll != pre_action_book.initial_bankroll:
        raise PaperExecutionAdoptionError(
            "PaperBook restart state changed initial bankroll"
        )

    pre_ticket_ids = set(pre_action_book.tickets)
    current_ticket_ids = set(self.book.tickets)
    if not pre_ticket_ids.issubset(current_ticket_ids):
        raise PaperExecutionAdoptionError(
            "PaperBook restart state removed pre-action exposure"
        )
    for ticket_id in pre_ticket_ids:
        if self.book.tickets[ticket_id] != pre_action_book.tickets[ticket_id]:
            raise PaperExecutionAdoptionError(
                "PaperBook restart state changed pre-action exposure"
            )

    accepted_attempts = tuple(
        attempt for attempt in run.attempts if attempt.outcome in _ACCEPTED_EQUIVALENT
    )
    added_ticket_ids = current_ticket_ids - pre_ticket_ids
    if len(added_ticket_ids) != len(accepted_attempts):
        raise PaperExecutionAdoptionError(
            "PaperBook restart state is not the exact pre-action or "
            "#623-authorized post-action state"
        )

    action_by_id = {
        action.action_id: action for action in prepared.execution_plan.actions
    }
    binding_by_id = {
        binding.action_id: binding for binding in prepared.exposure_bindings
    }
    expected_balance = pre_action_book.balance
    expected_lifecycle = list(pre_action_book._lifecycle)
    matched_ticket_ids: set[str] = set()

    for attempt in accepted_attempts:
        action = action_by_id.get(attempt.action_id)
        binding = binding_by_id.get(attempt.action_id)
        if action is None or binding is None:
            raise PaperExecutionAdoptionError(
                "durable attempt is not bound to prepared execution action"
            )
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent durable attempt lacks execution truth"
            )
        marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
        matches = [
            ticket
            for ticket_id, ticket in self.book.tickets.items()
            if ticket_id in added_ticket_ids and marker in ticket.strategy_reason
        ]
        if len(matches) != 1:
            raise PaperExecutionAdoptionError(
                "PaperBook restart state is not the exact pre-action or "
                "#623-authorized post-action state"
            )
        ticket = matches[0]
        if not self._ticket_matches_attempt(
            ticket=ticket,
            attempt=attempt,
            action=action,
            binding=binding,
        ):
            raise PaperExecutionAdoptionError(
                "PaperBook restart exposure conflicts with durable execution attempt"
            )
        if ticket.ticket_id in matched_ticket_ids:
            raise PaperExecutionAdoptionError(
                "PaperBook restart state reuses one ticket for multiple attempts"
            )
        matched_ticket_ids.add(ticket.ticket_id)
        expected_balance = PaperBook._debit_balance(
            expected_balance,
            attempt.execution_stake,
        )
        expected_lifecycle.append(("open", ticket.ticket_id, (), ()))

    if matched_ticket_ids != added_ticket_ids:
        raise PaperExecutionAdoptionError(
            "PaperBook restart state contains exposure outside durable #623 attempts"
        )
    if (
        self.book.balance != expected_balance
        or self.book._lifecycle != expected_lifecycle
        or self.book._settlement_times != pre_action_book._settlement_times
    ):
        raise PaperExecutionAdoptionError(
            "PaperBook restart state is not the exact pre-action or "
            "#623-authorized post-action state"
        )


def _guarded_prepare(
    self: PaperExecutionAdoptionRuntime,
    *,
    plan,
    intents,
    decision_id: str,
):
    prepared = _ORIGINAL_PREPARE(
        self,
        plan=plan,
        intents=intents,
        decision_id=decision_id,
    )
    if (
        prepared is None
        and type(decision_id) is str
        and decision_id.startswith(_LIVE_DECISION_PREFIX)
    ):
        pre_action_book = _load_live_pre_action_book(self)
        if (
            pre_action_book is not None
            and not self._same_book_state(self.book, pre_action_book)
        ):
            raise PaperExecutionAdoptionError(
                "live no-execution recovery PaperBook differs from exact pre-action state"
            )
    return prepared


def _guarded_execute(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations=None,
    evidence_registry=None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    """Fence live replay against unrelated durable PaperBook drift before resume."""
    if type(trigger_id) is str and trigger_id.startswith(_LIVE_DECISION_PREFIX):
        pre_action_book = _load_live_pre_action_book(self)
        if pre_action_book is not None:
            self.assert_recoverable_book_state(
                pre_action_book=pre_action_book,
                prepared=prepared,
                trigger_id=trigger_id,
                started_at=started_at,
                materialize_exposure=materialize_exposure,
            )

    return _ORIGINAL_EXECUTE(
        self,
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
        materialize_exposure=materialize_exposure,
        observations=observations,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )


def _install_runtime_guards() -> None:
    if getattr(PaperExecutionAdoptionRuntime, "_autosport_append_recovery_installed", False):
        return
    PaperExecutionAdoptionRuntime.assert_recoverable_book_state = (
        _exact_assert_recoverable_book_state
    )
    PaperExecutionAdoptionRuntime._ticket_matches_attempt = staticmethod(
        _exact_ticket_matches_attempt
    )
    PaperExecutionAdoptionRuntime.prepare = _guarded_prepare
    PaperExecutionAdoptionRuntime.execute = _guarded_execute
    PaperExecutionAdoptionRuntime._autosport_append_recovery_installed = True


def _install_live_loop_guard() -> None:
    # Import only after the execution runtime is fully patched. Python package
    # imports execute __init__ before submodule resolution, so this also makes the
    # no-runtime APPEND_PENDING fence authoritative for direct live-loop imports.
    from . import live_decision_loop as live

    cls = live.PersistentLiveDecisionLoop
    if getattr(cls, "_autosport_append_recovery_installed", False):
        return
    original = cls._recover_unfinished_progress

    def guarded_recover(loop):
        progress = loop._progress
        if progress is not None and progress.phase == "append_pending":
            try:
                pre_action_book = _load_pre_action_path(
                    loop.pre_action_book_path,
                    label="live append-pending recovery",
                )
            except PaperExecutionAdoptionError as exc:
                raise live.LiveDecisionProgressError(str(exc)) from exc
            if pre_action_book is None:
                raise live.LiveDecisionProgressError(
                    "append-pending recovery requires exact pre-action PaperBook witness"
                )
            if (
                loop.paper_execution is None
                and not loop._same_book_state(loop.book, pre_action_book)
            ):
                raise live.LiveDecisionProgressError(
                    "append-pending PaperBook changed without exact execution authority"
                )
        return original(loop)

    cls._recover_unfinished_progress = guarded_recover
    cls._autosport_append_recovery_installed = True


_install_runtime_guards()
_install_live_loop_guard()
