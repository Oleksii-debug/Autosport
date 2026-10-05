from __future__ import annotations

from dataclasses import replace

from . import paper_execution_adoption as _adoption
from .domain import TicketLeg
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from .real_execution_ledger import ExecutionAction


_ORIGINAL_PREPARE = PaperExecutionAdoptionRuntime.prepare
_ORIGINAL_PREPARE_PAPER_VALUE_ACTION = (
    PaperExecutionAdoptionRuntime.prepare_paper_value_action
)
_ORIGINAL_ASSERT_RECOVERABLE_BOOK_STATE = (
    PaperExecutionAdoptionRuntime.assert_recoverable_book_state
)
_ORIGINAL_MATERIALIZE_ATTEMPT = PaperExecutionAdoptionRuntime._materialize_attempt
_ORIGINAL_TICKET_MATCHES_ATTEMPT = PaperExecutionAdoptionRuntime._ticket_matches_attempt


def _normalized_action_side(exchange_side: str | None) -> str:
    if exchange_side is None:
        return "BACK"
    if type(exchange_side) is not str:
        raise PaperExecutionAdoptionError(
            "PAPER adoption exchange side must be an exact canonical string"
        )
    if exchange_side == "back":
        return "BACK"
    if exchange_side == "lay":
        return "LAY"
    raise PaperExecutionAdoptionError(
        "PAPER adoption exchange side is not supported"
    )


def _require_supported_exchange_side(exchange_side: str | None) -> None:
    _normalized_action_side(exchange_side)


def _replace_prepared_actions(
    runtime: PaperExecutionAdoptionRuntime,
    prepared: PreparedPaperExecution,
    sides: tuple[str, ...],
    *,
    paper_value_decision_id: str | None = None,
) -> PreparedPaperExecution:
    actions = prepared.execution_plan.actions
    if len(actions) != len(sides):
        raise PaperExecutionAdoptionError(
            "PAPER execution side vector does not match prepared action vector"
        )
    if all(action.side == side for action, side in zip(actions, sides, strict=True)):
        return prepared

    replaced_actions = tuple(
        replace(action, side=side)
        for action, side in zip(actions, sides, strict=True)
    )
    execution_plan = replace(
        prepared.execution_plan,
        actions=replaced_actions,
    )

    # The portfolio plan id is already side-bound through the intent/quote hashes
    # used to derive stable action ids. The legacy PaperValue plan id, however,
    # hashes the complete action payload, so recompute it after changing BACK->LAY.
    if paper_value_decision_id is not None:
        if len(replaced_actions) != 1:
            raise PaperExecutionAdoptionError(
                "paper-value execution must contain exactly one action"
            )
        execution_plan = replace(
            execution_plan,
            plan_id="paper-value-plan-v1-"
            + _adoption._digest(
                {
                    "decision_id": paper_value_decision_id,
                    "action": replaced_actions[0].to_dict(),
                    "model_fingerprint": runtime.config.fingerprint,
                }
            ),
        )

    runtime._prepared_authorities.pop(id(prepared), None)
    return runtime._mint_prepared(
        replace(prepared, execution_plan=execution_plan)
    )


def _prepare(
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
    if prepared is None:
        return None

    sides: list[str] = []
    for intent, stake in zip(intents, plan.stakes, strict=True):
        if stake == 0:
            continue
        context = intent.risk_context
        if len(context.quotes) != 1:
            raise PaperExecutionAdoptionError(
                "positive PAPER execution requires one exact execution quote"
            )
        sides.append(_normalized_action_side(context.quotes[0].exchange_side))
    return _replace_prepared_actions(self, prepared, tuple(sides))


def _prepare_paper_value_action(
    self: PaperExecutionAdoptionRuntime,
    *,
    event,
    stake,
    decision_id: str,
    account_id: str,
    bankroll_id: str | None,
    currency: str | None,
):
    prepared = _ORIGINAL_PREPARE_PAPER_VALUE_ACTION(
        self,
        event=event,
        stake=stake,
        decision_id=decision_id,
        account_id=account_id,
        bankroll_id=bankroll_id,
        currency=currency,
    )
    side = _normalized_action_side(event.exchange_side)
    return _replace_prepared_actions(
        self,
        prepared,
        (side,),
        paper_value_decision_id=decision_id if side == "LAY" else None,
    )


def _require_action_side(action: ExecutionAction) -> str:
    if type(action) is not ExecutionAction or type(action.side) is not str:
        raise PaperExecutionAdoptionError(
            "PaperBook materialization requires canonical ExecutionAction side authority"
        )
    if action.side not in {"BACK", "LAY"}:
        raise PaperExecutionAdoptionError(
            "PaperBook materialization requires canonical BACK or LAY execution side"
        )
    return action.side


def _ticket_matches_attempt(
    *,
    ticket,
    attempt,
    action: ExecutionAction,
    binding: PaperExposureBinding,
) -> bool:
    try:
        side = _require_action_side(action)
    except PaperExecutionAdoptionError:
        return False
    if (
        attempt.action_id != action.action_id
        or attempt.side != side
        or attempt.bookmaker_id != action.bookmaker_id
        or attempt.account_id != action.account_id
        or attempt.event_id != action.event_id
        or attempt.market_id != action.market_id
        or attempt.selection_id != action.selection_id
        or ticket.stake != attempt.execution_stake
        or ticket.placed_at != attempt.execution_observed_at
        or len(ticket.legs) != 1
        or ticket.provider_source_ids != (attempt.bookmaker_id,)
        or ticket.provider_accounts
        != ((attempt.bookmaker_id, attempt.account_id),)
        or ticket.bankroll_id != binding.bankroll_id
        or ticket.currency != binding.currency
        or attempt.decision_quote_id != action.quote_id
        or attempt.decision_odds != action.requested_odds
        or attempt.requested_stake != action.requested_stake
    ):
        return False
    leg = ticket.legs[0]
    return (
        leg.event_id == attempt.event_id
        and leg.market_id == attempt.market_id
        and leg.selection_id == attempt.selection_id
        and leg.locked_odds == attempt.execution_odds
        and leg.sport == binding.sport
        and leg.exchange_side == side.lower()
    )


def _materialize_attempt(
    self: PaperExecutionAdoptionRuntime,
    *,
    attempt,
    action: ExecutionAction,
    binding: PaperExposureBinding,
    decision_id: str,
):
    side = _require_action_side(action)
    self._require_attempt_action_identity(attempt, action)
    if attempt.execution_odds is None or attempt.execution_stake is None:
        raise PaperExecutionAdoptionError(
            "accepted-equivalent attempt lacks execution odds/stake"
        )
    marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
    matches = [
        ticket
        for ticket in self.book.tickets.values()
        if marker in ticket.strategy_reason
    ]
    if len(matches) > 1:
        raise PaperExecutionAdoptionError(
            "PaperBook contains duplicate exposure for one execution attempt"
        )
    if matches:
        ticket = matches[0]
        if not self._ticket_matches_attempt(
            ticket=ticket,
            attempt=attempt,
            action=action,
            binding=binding,
        ):
            raise PaperExecutionAdoptionError(
                "existing PaperBook exposure conflicts with durable execution attempt"
            )
        return ticket

    return self.book.open_ticket(
        [
            TicketLeg(
                event_id=attempt.event_id,
                market_id=attempt.market_id,
                selection_id=attempt.selection_id,
                locked_odds=attempt.execution_odds,
                sport=binding.sport,
                exchange_side=side.lower(),
            )
        ],
        attempt.execution_stake,
        reason=(
            f"paper execution adoption; decision_id={decision_id}; "
            f"run_id={attempt.run_id}; {marker}"
        ),
        placed_at=attempt.execution_observed_at,
        provider_source_ids=(attempt.bookmaker_id,),
        provider_accounts=((attempt.bookmaker_id, attempt.account_id),),
        bankroll_id=binding.bankroll_id,
        currency=binding.currency,
    )


def _assert_recoverable_book_state(
    self: PaperExecutionAdoptionRuntime,
    *,
    pre_action_book,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
) -> None:
    if not isinstance(prepared, PreparedPaperExecution):
        raise TypeError("prepared must be PreparedPaperExecution")
    self._require_minted(prepared)
    canonical_sides = tuple(
        _require_action_side(action)
        for action in prepared.execution_plan.actions
    )
    if all(side == "BACK" for side in canonical_sides):
        return _ORIGINAL_ASSERT_RECOVERABLE_BOOK_STATE(
            self,
            pre_action_book=pre_action_book,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
        )

    if not isinstance(pre_action_book, _adoption.PaperBook):
        raise TypeError("pre_action_book must be PaperBook")
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

    expected = _adoption.copy.deepcopy(pre_action_book)
    action_by_id = {
        action.action_id: action for action in prepared.execution_plan.actions
    }
    binding_by_id = {
        binding.action_id: binding for binding in prepared.exposure_bindings
    }
    for attempt in run.attempts:
        if attempt.outcome not in {
            _adoption.PaperAttemptOutcome.ACCEPTED,
            _adoption.PaperAttemptOutcome.PARTIAL,
        }:
            continue
        action = action_by_id.get(attempt.action_id)
        binding = binding_by_id.get(attempt.action_id)
        if action is None or binding is None:
            raise PaperExecutionAdoptionError(
                "durable attempt is not bound to prepared execution action"
            )
        side = _require_action_side(action)
        self._require_attempt_action_identity(attempt, action)
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent durable attempt lacks execution truth"
            )
        expected.open_ticket(
            [
                TicketLeg(
                    event_id=attempt.event_id,
                    market_id=attempt.market_id,
                    selection_id=attempt.selection_id,
                    locked_odds=attempt.execution_odds,
                    sport=binding.sport,
                    exchange_side=side.lower(),
                )
            ],
            attempt.execution_stake,
            reason=(
                f"paper execution adoption; "
                f"decision_id={prepared.execution_plan.decision_id}; "
                f"run_id={attempt.run_id}; "
                f"{self._TICKET_MARKER}{attempt.attempt_id}"
            ),
            placed_at=attempt.execution_observed_at,
            provider_source_ids=(attempt.bookmaker_id,),
            provider_accounts=((attempt.bookmaker_id, attempt.account_id),),
            bankroll_id=binding.bankroll_id,
            currency=binding.currency,
        )

    if not self._same_book_state(self.book, expected):
        raise PaperExecutionAdoptionError(
            "PaperBook restart state is not the exact pre-action or "
            "#623-authorized post-action state"
        )


def _install() -> None:
    marker = "_autosport_lay_adoption_guard"
    if getattr(PaperExecutionAdoptionRuntime, marker, False):
        return
    PaperExecutionAdoptionRuntime._require_back_compatible_exchange_side = staticmethod(
        _require_supported_exchange_side
    )
    PaperExecutionAdoptionRuntime.prepare = _prepare
    PaperExecutionAdoptionRuntime.prepare_paper_value_action = (
        _prepare_paper_value_action
    )
    PaperExecutionAdoptionRuntime._materialize_attempt = _materialize_attempt
    PaperExecutionAdoptionRuntime._ticket_matches_attempt = staticmethod(
        _ticket_matches_attempt
    )
    PaperExecutionAdoptionRuntime.assert_recoverable_book_state = (
        _assert_recoverable_book_state
    )
    setattr(PaperExecutionAdoptionRuntime, marker, True)


_install()


__all__ = []
