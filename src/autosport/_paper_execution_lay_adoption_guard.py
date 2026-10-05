from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from . import paper as _paper
from . import paper_execution_adoption as _adoption
from . import paper_execution_reality as _reality
from .domain import TicketLeg
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from .real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    _validate_decimal_text_resource_bound,
)


_ORIGINAL_INIT = PaperExecutionAdoptionRuntime.__init__
_ORIGINAL_PREPARE = PaperExecutionAdoptionRuntime.prepare
_ORIGINAL_PREPARE_PAPER_VALUE_ACTION = (
    PaperExecutionAdoptionRuntime.prepare_paper_value_action
)
_ORIGINAL_ASSERT_RECOVERABLE_BOOK_STATE = (
    PaperExecutionAdoptionRuntime.assert_recoverable_book_state
)
_ORIGINAL_MATERIALIZE_ATTEMPT = PaperExecutionAdoptionRuntime._materialize_attempt
_ORIGINAL_TICKET_MATCHES_ATTEMPT = PaperExecutionAdoptionRuntime._ticket_matches_attempt
_ORIGINAL_MINT_PREPARED = PaperExecutionAdoptionRuntime._mint_prepared
_ORIGINAL_REQUIRE_MINTED = PaperExecutionAdoptionRuntime._require_minted
_ORIGINAL_EXPECTED_RUN_ID = PaperExecutionAdoptionRuntime.expected_run_id
_ORIGINAL_EXECUTE_UNLOCKED = PaperExecutionAdoptionRuntime._execute_unlocked
_ORIGINAL_PAPERBOOK_SAVE = _adoption.PaperBook.save
_ORIGINAL_PAPERBOOK_LOAD = _adoption.PaperBook.load
_ORIGINAL_PAPERBOOK_OPEN_TICKET = _adoption.PaperBook.open_ticket
_ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE = _adoption.PaperBook._validate_loaded_state
_ORIGINAL_LEDGER_EVENTS = _reality.PaperExecutionLedger.events
_ORIGINAL_LEDGER_LOAD_RUN = _reality.PaperExecutionLedger.load_run
_ORIGINAL_LEDGER_APPEND_EVENT = _reality.PaperExecutionLedger._append_event
_ORIGINAL_EXPOSURE_SCOPE_PAYLOAD = PaperExecutionAdoptionRuntime._exposure_scope_payload
_ORIGINAL_REQUIRE_ATTEMPT_ACTION_IDENTITY = PaperExecutionAdoptionRuntime._require_attempt_action_identity
_ORIGINAL_SAME_BOOK_STATE = PaperExecutionAdoptionRuntime._same_book_state
_ORIGINAL_ASSERT_SAME_BOOK_STATE = PaperExecutionAdoptionRuntime._assert_same_book_state
_ORIGINAL_EXECUTE_PAPER_PLAN = _adoption.execute_paper_plan
_ORIGINAL_DEEPCOPY = _adoption.copy.deepcopy
_ORIGINAL_INSTALL_TICKET_OPENING_AUTHORITY = _paper._install_validated_ticket_opening_authority
_ORIGINAL_INSTALL_CAUSAL_HISTORY_AUTHORITY = _paper._install_validated_paperbook_causal_history_authority
_ORIGINAL_REQUIRE_EXECUTION_CONFIG = _reality._require_canonical_execution_config_surface
_ORIGINAL_REQUIRE_EXECUTION_PLAN = _reality._require_canonical_execution_plan_surface
_ORIGINAL_VALIDATE_LAY_EXECUTION_SURFACE = _reality._validate_lay_execution_surface
_ORIGINAL_RUN_ID = _reality._impl._run_id
_ORIGINAL_VERIFY_OBSERVATION_AUTHORITY = _reality._impl._verify_observation_authority
_ORIGINAL_OBSERVED_ATTEMPT = _reality._impl._observed_attempt
_ORIGINAL_REQUIRE_ACTION_SURFACE = _reality._impl._require_canonical_action_surface
_ORIGINAL_ADOPTION_DIGEST = _adoption._digest
_ORIGINAL_ADOPTION_UTC_TIMESTAMP = _adoption._utc_timestamp
_ORIGINAL_ADOPTION_RESULT = _adoption.PaperExecutionAdoptionResult
_PREPARED_WITNESSES: dict[int, tuple[PreparedPaperExecution, str]] = {}
_ACTION_WITNESSES: dict[int, tuple[ExecutionAction, str, str]] = {}
_BINDING_WITNESSES: dict[int, tuple[PaperExposureBinding, tuple[str, str | None, str | None, str | None]]] = {}
_RUNTIME_WITNESSES: dict[int, tuple[object, ...]] = {}





def _init(self: PaperExecutionAdoptionRuntime, *args, **kwargs) -> None:
    _ORIGINAL_INIT(self, *args, **kwargs)
    _ORIGINAL_REQUIRE_EXECUTION_CONFIG(self.config)
    _RUNTIME_WITNESSES[id(self)] = (
        self,
        self.book,
        self.ledger,
        self.config,
        self.config.fingerprint,
        self.paper_book_path,
        self.max_quote_age,
        self._execution_lock,
        self._prepared_authorities,
        self._TICKET_MARKER,
        self._EXPOSURE_SCOPE_EVENT_TYPE,
        self._EXPOSURE_SCOPE_SCHEMA,
    )


def _require_runtime_authority(self: PaperExecutionAdoptionRuntime) -> None:
    witness = _RUNTIME_WITNESSES.get(id(self))
    if witness is None or len(witness) != 12 or witness[0] is not self:
        raise PaperExecutionAdoptionError(
            "PAPER adoption runtime authority witness is unavailable"
        )
    (
        _runtime,
        book,
        ledger,
        config,
        config_fingerprint,
        paper_book_path,
        max_quote_age,
        execution_lock,
        prepared_authorities,
        ticket_marker,
        exposure_scope_event_type,
        exposure_scope_schema,
    ) = witness
    if self.book is not book or self.ledger is not ledger or self.config is not config:
        raise PaperExecutionAdoptionError(
            "PAPER adoption runtime authority object changed after construction"
        )
    if type(self.book) is not _adoption.PaperBook:
        raise PaperExecutionAdoptionError(
            "PAPER adoption runtime book must retain exact PaperBook authority"
        )
    if (
        self.paper_book_path is not paper_book_path
        or self.max_quote_age is not max_quote_age
        or self._execution_lock is not execution_lock
        or self._prepared_authorities is not prepared_authorities
        or type(self._TICKET_MARKER) is not str
        or self._TICKET_MARKER != ticket_marker
        or type(self._EXPOSURE_SCOPE_EVENT_TYPE) is not str
        or self._EXPOSURE_SCOPE_EVENT_TYPE != exposure_scope_event_type
        or type(self._EXPOSURE_SCOPE_SCHEMA) is not str
        or self._EXPOSURE_SCOPE_SCHEMA != exposure_scope_schema
    ):
        raise PaperExecutionAdoptionError(
            "PAPER adoption runtime configuration changed after construction"
        )
    _ORIGINAL_REQUIRE_EXECUTION_CONFIG(self.config)
    if self.config.fingerprint != config_fingerprint:
        raise PaperExecutionAdoptionError(
            "PAPER adoption execution config changed after construction"
        )

def _exact_text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not value or value.strip() != value:
        raise PaperExecutionAdoptionError(
            f"{name} must retain exact canonical text authority"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PaperExecutionAdoptionError(
            f"{name} must retain UTF-8 text authority"
        ) from exc
    return value


def _prepared_authority_payload(
    prepared: PreparedPaperExecution,
) -> dict[str, object]:
    """Return a hook-free snapshot of every authority-bearing prepared field."""
    if type(prepared) is not PreparedPaperExecution:
        raise TypeError("prepared must be exact PreparedPaperExecution")
    plan = prepared.execution_plan
    if type(plan) is not ExecutionPlan:
        raise PaperExecutionAdoptionError(
            "prepared execution plan must retain exact ExecutionPlan authority"
        )
    if type(plan.schema_version) is not int:
        raise PaperExecutionAdoptionError(
            "prepared execution plan schema_version must retain exact integer authority"
        )
    if type(plan.actions) is not tuple or not plan.actions:
        raise PaperExecutionAdoptionError(
            "prepared execution actions must retain canonical tuple authority"
        )

    plan_payload: dict[str, object] = {
        "plan_id": _exact_text(plan.plan_id, "prepared plan_id"),
        "bookmaker_profile_version": _exact_text(
            plan.bookmaker_profile_version,
            "prepared bookmaker_profile_version",
        ),
        "decision_id": _exact_text(plan.decision_id, "prepared decision_id"),
        "approval_id": _exact_text(plan.approval_id, "prepared approval_id"),
        "created_at": _exact_text(plan.created_at, "prepared created_at"),
        "schema_version": plan.schema_version,
        "actions": [],
    }
    action_payloads: list[dict[str, object]] = []
    for action in plan.actions:
        if type(action) is not ExecutionAction:
            raise PaperExecutionAdoptionError(
                "prepared action must retain exact ExecutionAction authority"
            )
        for name in (
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
            "quote_observed_at",
            "expires_at",
        ):
            _exact_text(getattr(action, name), f"prepared action {name}")
        if (
            type(action.requested_odds) is not Decimal
            or type(action.requested_stake) is not Decimal
        ):
            raise PaperExecutionAdoptionError(
                "prepared action economics must retain exact Decimal authority"
            )
        if (
            not action.requested_odds.is_finite()
            or action.requested_odds <= 0
            or not action.requested_stake.is_finite()
            or action.requested_stake <= 0
        ):
            raise PaperExecutionAdoptionError(
                "prepared action economics left canonical positive finite authority"
            )
        try:
            _validate_decimal_text_resource_bound(action.requested_odds)
            _validate_decimal_text_resource_bound(action.requested_stake)
        except ValueError as exc:
            raise PaperExecutionAdoptionError(
                "prepared action economics exceed canonical Decimal resource bounds"
            ) from exc
        action_payloads.append(action.to_dict())
    plan_payload["actions"] = action_payloads

    bindings = prepared.exposure_bindings
    if type(bindings) is not tuple or len(bindings) != len(plan.actions):
        raise PaperExecutionAdoptionError(
            "prepared exposure bindings must retain exact action cardinality"
        )
    binding_payloads: list[dict[str, object]] = []
    for binding in bindings:
        if type(binding) is not PaperExposureBinding:
            raise PaperExecutionAdoptionError(
                "prepared exposure binding must retain exact binding authority"
            )
        binding_payloads.append(
            {
                "action_id": _exact_text(
                    binding.action_id,
                    "prepared binding action_id",
                ),
                "sport": _exact_text(
                    binding.sport,
                    "prepared binding sport",
                    optional=True,
                ),
                "bankroll_id": _exact_text(
                    binding.bankroll_id,
                    "prepared binding bankroll_id",
                    optional=True,
                ),
                "currency": _exact_text(
                    binding.currency,
                    "prepared binding currency",
                    optional=True,
                ),
            }
        )
    if tuple(item["action_id"] for item in binding_payloads) != tuple(
        action.action_id for action in plan.actions
    ):
        raise PaperExecutionAdoptionError(
            "prepared exposure binding order no longer matches execution actions"
        )

    intent_evidence_json = _exact_text(
        prepared.intent_evidence_json,
        "prepared intent_evidence_json",
    )
    return {
        "execution_plan": plan_payload,
        "exposure_bindings": binding_payloads,
        "intent_evidence_json": intent_evidence_json,
    }


def _prepared_authority_witness(prepared: PreparedPaperExecution) -> str:
    return _ORIGINAL_ADOPTION_DIGEST(_prepared_authority_payload(prepared))


def _action_authority_witness(action: ExecutionAction) -> str:
    if type(action) is not ExecutionAction:
        raise PaperExecutionAdoptionError(
            "materialization action must retain exact ExecutionAction authority"
        )
    _ORIGINAL_REQUIRE_ACTION_SURFACE(action)
    return _ORIGINAL_ADOPTION_DIGEST(action.to_dict())


def _binding_authority_witness(
    binding: PaperExposureBinding,
) -> tuple[str, str | None, str | None, str | None]:
    if type(binding) is not PaperExposureBinding:
        raise PaperExecutionAdoptionError(
            "materialization binding must retain exact PaperExposureBinding authority"
        )
    return (
        _exact_text(binding.action_id, "materialization binding action_id"),
        _exact_text(binding.sport, "materialization binding sport", optional=True),
        _exact_text(
            binding.bankroll_id,
            "materialization binding bankroll_id",
            optional=True,
        ),
        _exact_text(binding.currency, "materialization binding currency", optional=True),
    )


def _require_materialization_authority(
    action: ExecutionAction,
    binding: PaperExposureBinding,
) -> str:
    action_witness = _ACTION_WITNESSES.get(id(action))
    if (
        action_witness is None
        or len(action_witness) != 3
        or action_witness[0] is not action
        or action_witness[1] != _action_authority_witness(action)
        or type(action_witness[2]) is not str
    ):
        raise PaperExecutionAdoptionError(
            "prepared action authority changed after mint"
        )
    binding_witness = _BINDING_WITNESSES.get(id(binding))
    if (
        binding_witness is None
        or binding_witness[0] is not binding
        or binding_witness[1] != _binding_authority_witness(binding)
    ):
        raise PaperExecutionAdoptionError(
            "prepared exposure binding authority changed after mint"
        )
    if binding.action_id != action.action_id:
        raise PaperExecutionAdoptionError(
            "prepared exposure binding no longer matches execution action"
        )
    return action_witness[2]


def _mint_prepared(
    self: PaperExecutionAdoptionRuntime,
    prepared: PreparedPaperExecution,
) -> PreparedPaperExecution:
    _require_runtime_authority(self)
    witness = _prepared_authority_witness(prepared)
    minted = _ORIGINAL_MINT_PREPARED(self, prepared)
    _PREPARED_WITNESSES[id(minted)] = (minted, witness)
    decision_id = _exact_text(
        minted.execution_plan.decision_id,
        "prepared decision_id",
    )
    for action in minted.execution_plan.actions:
        _ACTION_WITNESSES[id(action)] = (
            action,
            _action_authority_witness(action),
            decision_id,
        )
    for binding in minted.exposure_bindings:
        _BINDING_WITNESSES[id(binding)] = (
            binding,
            _binding_authority_witness(binding),
        )
    return minted


def _require_minted(
    self: PaperExecutionAdoptionRuntime,
    prepared: PreparedPaperExecution,
) -> None:
    _require_runtime_authority(self)
    _ORIGINAL_REQUIRE_MINTED(self, prepared)
    witness = _PREPARED_WITNESSES.get(id(prepared))
    if (
        witness is None
        or len(witness) != 2
        or witness[0] is not prepared
        or type(witness[1]) is not str
    ):
        raise PaperExecutionAdoptionError(
            "prepared execution authority witness is unavailable"
        )
    expected = witness[1]
    current = _prepared_authority_witness(prepared)
    if current != expected:
        raise PaperExecutionAdoptionError(
            "prepared execution authority changed after mint"
        )




def _expected_run_id(
    self: PaperExecutionAdoptionRuntime,
    prepared: PreparedPaperExecution,
    trigger_id: str,
) -> str:
    # Adoption publishes the exposure-scope event immediately after deriving this
    # identity. Revalidate mutable frozen surfaces before any fingerprint/run-id
    # calculation can influence that durable key.
    _require_minted(self, prepared)
    _ORIGINAL_REQUIRE_EXECUTION_PLAN(prepared.execution_plan)
    _ORIGINAL_REQUIRE_EXECUTION_CONFIG(self.config)
    return _ORIGINAL_RUN_ID(
        prepared.execution_plan,
        _exact_text(trigger_id, "trigger_id"),
        self.config,
    )

def _preflight_adoption_inputs(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations: Mapping[str, _reality.ObservedPaperExecution] | None,
    evidence_registry: _reality.PaperExecutionEvidenceRegistry | None,
    suspended_action_ids: frozenset[str],
) -> dict[str, _reality.ObservedPaperExecution]:
    """Validate/snapshot execution inputs before EXPOSURE_SCOPE becomes durable."""
    _require_minted(self, prepared)
    _ORIGINAL_REQUIRE_EXECUTION_PLAN(prepared.execution_plan)
    _ORIGINAL_REQUIRE_EXECUTION_CONFIG(self.config)
    _exact_text(trigger_id, "trigger_id")
    _ORIGINAL_ADOPTION_UTC_TIMESTAMP(started_at, "started_at")
    if type(materialize_exposure) is not bool:
        raise TypeError("materialize_exposure must be bool")
    if observations is None:
        observation_snapshot: dict[str, _reality.ObservedPaperExecution] = {}
    else:
        if not isinstance(observations, Mapping):
            raise TypeError("observations must be a mapping")
        try:
            observation_snapshot = dict(observations)
        except (TypeError, ValueError) as exc:
            raise TypeError("observations must be a stable mapping") from exc
    if any(type(action_id) is not str for action_id in observation_snapshot):
        raise TypeError("observation keys must be exact str action ids")
    # Mapping iteration is caller code. Revalidate the minted authority after
    # that callback boundary before reading any prepared execution fields.
    _require_minted(self, prepared)
    if type(suspended_action_ids) is not frozenset or any(
        type(item) is not str for item in suspended_action_ids
    ):
        raise TypeError("suspended_action_ids must be a frozenset[str]")
    action_ids = {action.action_id for action in prepared.execution_plan.actions}
    if set(observation_snapshot) - action_ids:
        raise _reality.PaperExecutionStateError(
            "observations contain action outside execution plan"
        )
    if set(suspended_action_ids) - action_ids:
        raise _reality.PaperExecutionStateError(
            "suspended_action_ids contain action outside execution plan"
        )
    if observation_snapshot:
        if type(evidence_registry) is not _reality.PaperExecutionEvidenceRegistry:
            raise _reality.PaperExecutionStateError(
                "configured/empirical observations require the exact durable evidence registry authority"
            )
        if evidence_registry.authority_ledger is not self.ledger:
            raise _reality.PaperExecutionStateError(
                "execution evidence registry must be bound to the exact runtime ledger"
            )
        action_by_id = {
            action.action_id: action
            for action in prepared.execution_plan.actions
        }
        canonical_observations: dict[str, _reality.ObservedPaperExecution] = {}
        for action_id, observation in observation_snapshot.items():
            record = _ORIGINAL_VERIFY_OBSERVATION_AUTHORITY(
                action=action_by_id[action_id],
                observation=observation,
                registry=evidence_registry,
            )
            canonical_observations[action_id] = record.as_observation()
        observation_snapshot = canonical_observations
        # Registry resolution is another callback boundary. Reject any mutation
        # of the minted plan/bindings before those values can affect durable
        # exposure scope or materialization.
        _require_minted(self, prepared)
    _ORIGINAL_VALIDATE_LAY_EXECUTION_SURFACE(
        plan=prepared.execution_plan,
        observations=observation_snapshot,
        suspended_action_ids=suspended_action_ids,
    )
    if observation_snapshot:
        _require_minted(self, prepared)
        run_id = _ORIGINAL_RUN_ID(
            prepared.execution_plan,
            trigger_id,
            self.config,
        )
        for sequence, action in enumerate(prepared.execution_plan.actions):
            observation = observation_snapshot.get(action.action_id)
            if observation is None:
                continue
            _ORIGINAL_OBSERVED_ATTEMPT(
                run_id=run_id,
                plan=prepared.execution_plan,
                action=action,
                sequence=sequence,
                config=self.config,
                observation=observation,
                started_at=started_at,
            )
    return observation_snapshot


def _authorized_paperbook_copy(book: _adoption.PaperBook) -> _adoption.PaperBook:
    """Clone validated economic state and mint authority for detached simulation."""
    if type(book) is not _adoption.PaperBook:
        raise TypeError("book must be exact PaperBook")
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(book)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook copy source is not canonical"
        ) from exc
    shadow = _ORIGINAL_DEEPCOPY(book)
    if type(shadow) is not _adoption.PaperBook:
        raise PaperExecutionAdoptionError(
            "PaperBook copy must retain exact PaperBook authority"
        )
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(shadow)
        _ORIGINAL_INSTALL_TICKET_OPENING_AUTHORITY(shadow)
        _ORIGINAL_INSTALL_CAUSAL_HISTORY_AUTHORITY(shadow)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook copy could not acquire validated product authority"
        ) from exc
    return shadow


def _preflight_materialization_batch(
    self: PaperExecutionAdoptionRuntime,
    candidates: list[tuple[object, ExecutionAction, PaperExposureBinding]],
    *,
    decision_id: str,
) -> None:
    """Prove the complete accepted batch against a detached PaperBook first."""
    if not candidates:
        return
    _require_runtime_authority(self)
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook state is invalid before batch materialization preflight"
        ) from exc

    shadow = _authorized_paperbook_copy(self.book)

    for attempt, action, binding in candidates:
        _require_materialization_authority(action, binding)
        side = _require_action_side(action)
        _ORIGINAL_REQUIRE_ATTEMPT_ACTION_IDENTITY(self, attempt, action)
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent attempt lacks execution odds/stake"
            )

        marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
        matches = [
            ticket
            for ticket in shadow.tickets.values()
            if marker in ticket.strategy_reason
        ]
        if len(matches) > 1:
            raise PaperExecutionAdoptionError(
                "PaperBook contains duplicate exposure for one execution attempt"
            )
        if matches:
            if not _ticket_matches_attempt(
                ticket=matches[0],
                attempt=attempt,
                action=action,
                binding=binding,
            ):
                raise PaperExecutionAdoptionError(
                    "existing PaperBook exposure conflicts with durable execution attempt"
                )
            continue

        try:
            _ORIGINAL_PAPERBOOK_OPEN_TICKET(shadow,
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
        except (TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "accepted PAPER batch cannot be materialized atomically"
            ) from exc

    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(shadow)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "batch materialization preflight produced invalid PaperBook state"
        ) from exc



def _restore_paperbook_from_snapshot(
    target: _adoption.PaperBook,
    snapshot: _adoption.PaperBook,
) -> None:
    """Restore exact validated economic state after failed live batch materialization."""
    if type(target) is not _adoption.PaperBook or type(snapshot) is not _adoption.PaperBook:
        raise PaperExecutionAdoptionError(
            "PaperBook rollback requires exact PaperBook authority"
        )
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(snapshot)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook rollback snapshot is not canonical"
        ) from exc

    target.initial_bankroll = snapshot.initial_bankroll
    target.balance = snapshot.balance
    target.tickets = _ORIGINAL_DEEPCOPY(snapshot.tickets)
    target._lifecycle = list(snapshot._lifecycle)
    target._settlement_times = dict(snapshot._settlement_times)
    try:
        _ORIGINAL_INSTALL_TICKET_OPENING_AUTHORITY(target)
        _ORIGINAL_INSTALL_CAUSAL_HISTORY_AUTHORITY(target)
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(target)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook rollback could not restore canonical authority"
        ) from exc




def _publish_exposure_scope_authority(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared: PreparedPaperExecution,
    run_id: str,
) -> None:
    """Publish exposure scope through captured canonical ledger authority."""
    _require_minted(self, prepared)
    _require_runtime_authority(self)
    canonical_run_id = _exact_text(run_id, "run_id")
    payload = _ORIGINAL_EXPOSURE_SCOPE_PAYLOAD(prepared)
    _require_minted(self, prepared)
    _ORIGINAL_LEDGER_APPEND_EVENT(
        self.ledger,
        event_type=self._EXPOSURE_SCOPE_EVENT_TYPE,
        run_id=canonical_run_id,
        key=f"{canonical_run_id}:exposure-scope",
        payload=payload,
    )

def _execute(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations: Mapping[str, _reality.ObservedPaperExecution] | None = None,
    evidence_registry: _reality.PaperExecutionEvidenceRegistry | None = None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    """Canonical entrypoint that cannot be redirected through instance _execute_unlocked."""
    _require_runtime_authority(self)
    execution_lock = self._execution_lock
    with execution_lock:
        _require_runtime_authority(self)
        return _execute_unlocked(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )

def _execute_unlocked(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations: Mapping[str, _reality.ObservedPaperExecution] | None = None,
    evidence_registry: _reality.PaperExecutionEvidenceRegistry | None = None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    observation_snapshot = _preflight_adoption_inputs(
        self,
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
        materialize_exposure=materialize_exposure,
        observations=observations,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )
    _require_minted(self, prepared)
    expected_run_id = _expected_run_id(self, prepared, trigger_id)
    _publish_exposure_scope_authority(
        self,
        prepared=prepared,
        run_id=expected_run_id,
    )

    # Durable ledger execution can cross filesystem and registry boundaries.
    # Re-prove runtime/prepared authority before any post-run materialization
    # reads the caller-visible prepared value again.
    returned_run = _ORIGINAL_EXECUTE_PAPER_PLAN(
        plan=prepared.execution_plan,
        trigger_id=trigger_id,
        config=self.config,
        ledger=self.ledger,
        started_at=started_at,
        observations=observation_snapshot,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )
    _require_minted(self, prepared)
    if (
        type(returned_run) is not _reality.PaperExecutionRun
        or type(returned_run.run_id) is not str
        or returned_run.run_id != expected_run_id
    ):
        raise PaperExecutionAdoptionError(
            "canonical execution returned unexpected run identity"
        )

    # Durable ledger state is execution truth. Reconstruct from the reservation,
    # attempt and completion evidence before reading execution economics.
    action_ids = tuple(
        action.action_id for action in prepared.execution_plan.actions
    )
    observation_evidence_ids = _durable_observation_evidence_ids(
        self.ledger,
        run_id=expected_run_id,
        action_ids=action_ids,
    )
    run = _ORIGINAL_LEDGER_LOAD_RUN(
        self.ledger,
        run_id=expected_run_id,
        trigger_id=trigger_id,
        plan=prepared.execution_plan,
        config=self.config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
    )
    _require_minted(self, prepared)
    if run is None or type(run) is not _reality.PaperExecutionRun:
        raise PaperExecutionAdoptionError(
            "canonical durable execution run is unavailable after execution"
        )
    decision_id = _exact_text(
        prepared.execution_plan.decision_id,
        "prepared decision_id",
    )
    if not materialize_exposure:
        return _ORIGINAL_ADOPTION_RESULT(run=run, ticket_ids=())

    action_by_id = {
        action.action_id: action for action in prepared.execution_plan.actions
    }
    binding_by_id = {
        binding.action_id: binding for binding in prepared.exposure_bindings
    }
    accepted_attempts: list[
        tuple[object, ExecutionAction, PaperExposureBinding]
    ] = []
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
        accepted_attempts.append((attempt, action, binding))

    _preflight_materialization_batch(
        self,
        accepted_attempts,
        decision_id=decision_id,
    )
    # Detached preflight executes domain callbacks. Re-prove the complete minted
    # authority before the first live economic mutation and retain the pinned
    # post-run decision identity for all ticket provenance.
    _require_minted(self, prepared)

    ticket_ids: list[str] = []
    materialization_book = self.book
    pre_materialization_book = (
        _authorized_paperbook_copy(materialization_book) if accepted_attempts else None
    )
    try:
        for attempt, action, binding in accepted_attempts:
            ticket = _materialize_attempt(self,
                attempt=attempt,
                action=action,
                binding=binding,
                decision_id=decision_id,
            )
            ticket_ids.append(ticket.ticket_id)

        if accepted_attempts:
            # A materialization callback can mutate a later action/binding after
            # the detached preflight. Re-prove the whole minted batch before any
            # partially-mutated live book is allowed to approach publication.
            _require_minted(self, prepared)
            for _attempt, action, binding in accepted_attempts:
                _require_materialization_authority(action, binding)
            try:
                _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
            except (TypeError, ValueError) as exc:
                raise PaperExecutionAdoptionError(
                    "PaperBook state is invalid after batch materialization"
                ) from exc
    except Exception:
        if pre_materialization_book is not None:
            _restore_paperbook_from_snapshot(
                materialization_book,
                pre_materialization_book,
            )
        raise

    if accepted_attempts:
        # Materialization/domain code is another mutation boundary. Validate the
        # complete economic snapshot and pin exact persistence authority before I/O.
        _require_minted(self, prepared)
        try:
            _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
        except (TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "PaperBook state is invalid before durable publication"
            ) from exc
        book = self.book
        paper_book_path = self.paper_book_path
        try:
            # Persistence is economic authority. Dispatch through the exact
            # validated PaperBook class so caller-owned instance attributes cannot
            # replace save() and turn a successful live debit into an undurable one.
            _ORIGINAL_PAPERBOOK_SAVE(book, paper_book_path)
        except Exception:
            # Canonical PaperBook.save publishes only via an atomic os.replace.
            # Any exception means that replacement did not complete, so restore
            # the already-materialized live economics to the pinned pre-batch state.
            if pre_materialization_book is not None:
                _restore_paperbook_from_snapshot(book, pre_materialization_book)
            raise

        # Save is an external durability boundary. Re-prove both runtime/prepared
        # authority and the in-memory economic snapshot before selecting reload path.
        _require_minted(self, prepared)
        if self.book is not book or self.paper_book_path is not paper_book_path:
            raise PaperExecutionAdoptionError(
                "PAPER adoption persistence authority changed during save"
            )
        try:
            _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(book)
        except (TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "PaperBook state changed or became invalid during durable publication"
            ) from exc

        durable_book = _ORIGINAL_PAPERBOOK_LOAD(paper_book_path)

        # Load is another callback/I/O boundary. Do not compare or accept the
        # reloaded state using pre-load runtime or prepared assumptions.
        _require_minted(self, prepared)
        if self.book is not book or self.paper_book_path is not paper_book_path:
            raise PaperExecutionAdoptionError(
                "PAPER adoption persistence authority changed during load"
            )
        if type(durable_book) is not _adoption.PaperBook:
            raise PaperExecutionAdoptionError(
                "durable PaperBook must retain exact PaperBook authority"
            )
        try:
            _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(durable_book)
            _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(book)
        except (TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "PaperBook changed or became invalid across durable publication"
            ) from exc
        _ORIGINAL_ASSERT_SAME_BOOK_STATE(
            durable_book,
            book,
            "PaperBook changed across atomic durable publication",
        )
        for attempt, action, binding in accepted_attempts:
            _require_materialization_authority(action, binding)
            marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
            matches = [
                ticket
                for ticket in durable_book.tickets.values()
                if marker in ticket.strategy_reason
            ]
            if len(matches) != 1 or not _ticket_matches_attempt(
                ticket=matches[0],
                attempt=attempt,
                action=action,
                binding=binding,
            ):
                raise PaperExecutionAdoptionError(
                    "durable PaperBook does not bind exact execution attempt"
                )

    return _ORIGINAL_ADOPTION_RESULT(
        run=run,
        ticket_ids=tuple(ticket_ids),
    )

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
            + _ORIGINAL_ADOPTION_DIGEST(
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
        authority_decision_id = _require_materialization_authority(action, binding)
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
        or ticket.strategy_reason
        != (
            f"paper execution adoption; decision_id={authority_decision_id}; "
            f"run_id={attempt.run_id}; "
            f"{PaperExecutionAdoptionRuntime._TICKET_MARKER}{attempt.attempt_id}"
        )
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
    _require_runtime_authority(self)
    side = _require_action_side(action)
    authority_decision_id = _require_materialization_authority(action, binding)
    if _exact_text(decision_id, "materialization decision_id") != authority_decision_id:
        raise PaperExecutionAdoptionError(
            "materialization decision_id changed after prepared authority mint"
        )
    _ORIGINAL_REQUIRE_ATTEMPT_ACTION_IDENTITY(self, attempt, action)
    # Existing PaperBook state is durable economic authority. Revalidate the
    # complete canonical snapshot before searching marker strings or comparing
    # ticket fields so a mutated/faulty in-memory ticket cannot execute custom
    # equality/hash hooks or participate in restart idempotence decisions.
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PaperBook state is invalid before execution materialization"
        ) from exc
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
        if not _ticket_matches_attempt(
            ticket=ticket,
            attempt=attempt,
            action=action,
            binding=binding,
        ):
            raise PaperExecutionAdoptionError(
                "existing PaperBook exposure conflicts with durable execution attempt"
            )
        return ticket

    return _ORIGINAL_PAPERBOOK_OPEN_TICKET(self.book,
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


def _durable_observation_evidence_ids(
    ledger: _reality.PaperExecutionLedger,
    *,
    run_id: str,
    action_ids: tuple[str, ...],
) -> dict[str, str]:
    if type(ledger) is not _reality.PaperExecutionLedger:
        raise PaperExecutionAdoptionError(
            "durable observation evidence lookup requires exact ledger authority"
        )
    reservations = [
        event
        for event in _ORIGINAL_LEDGER_EVENTS(ledger, run_id)
        if event["event_type"] == "RUN_RESERVED"
    ]
    if len(reservations) != 1:
        raise PaperExecutionAdoptionError(
            "recovery requires exactly one durable run reservation"
        )
    raw = reservations[0]["payload"].get("observation_evidence_ids")
    if type(raw) is not dict:
        raise PaperExecutionAdoptionError(
            "durable reservation observation evidence map is invalid"
        )
    canonical: dict[str, str] = {}
    permitted = set(action_ids)
    for action_id, evidence_id in raw.items():
        if (
            type(action_id) is not str
            or not action_id
            or action_id.strip() != action_id
            or type(evidence_id) is not str
            or not evidence_id
            or evidence_id.strip() != evidence_id
        ):
            raise PaperExecutionAdoptionError(
                "durable reservation observation evidence identity is invalid"
            )
        if action_id not in permitted:
            raise PaperExecutionAdoptionError(
                "durable reservation observation evidence is outside prepared plan"
            )
        canonical[action_id] = evidence_id
    return dict(sorted(canonical.items()))


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
    _require_minted(self, prepared)
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

    if type(pre_action_book) is not _adoption.PaperBook:
        raise TypeError("pre_action_book must be exact PaperBook")
    if type(materialize_exposure) is not bool:
        raise TypeError("materialize_exposure must be bool")

    # Recovery equality decides whether durable economic state already equals
    # the pre-action snapshot. Validate both exact PaperBook values before any
    # field/container equality so corrupted frozen values cannot execute custom
    # comparison hooks or mint an idempotent recovery result.
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(pre_action_book)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PAPER recovery requires canonical validated PaperBook state"
        ) from exc

    if _ORIGINAL_SAME_BOOK_STATE(self.book, pre_action_book):
        return
    if not materialize_exposure:
        raise PaperExecutionAdoptionError(
            "SHADOW recovery PaperBook differs from exact pre-action state"
        )

    run_id = _expected_run_id(self, prepared, trigger_id)
    action_ids = tuple(
        action.action_id for action in prepared.execution_plan.actions
    )
    observation_evidence_ids = _durable_observation_evidence_ids(
        self.ledger,
        run_id=run_id,
        action_ids=action_ids,
    )
    run = _ORIGINAL_LEDGER_LOAD_RUN(
        self.ledger,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=prepared.execution_plan,
        config=self.config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )
    # Durable ledger reads are callback boundaries. Re-prove the runtime and
    # minted plan/bindings before re-reading them to reconstruct economic state.
    _require_minted(self, prepared)
    _ORIGINAL_REQUIRE_EXECUTION_PLAN(prepared.execution_plan)
    _ORIGINAL_REQUIRE_EXECUTION_CONFIG(self.config)
    if run is None:
        raise PaperExecutionAdoptionError(
            "PaperBook changed before any durable #623 run evidence"
        )

    expected = _authorized_paperbook_copy(pre_action_book)
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
        _require_materialization_authority(action, binding)
        side = _require_action_side(action)
        _ORIGINAL_REQUIRE_ATTEMPT_ACTION_IDENTITY(self, attempt, action)
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent durable attempt lacks execution truth"
            )
        _ORIGINAL_PAPERBOOK_OPEN_TICKET(expected,
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

    # Reconstruction itself invokes PaperBook domain code. Re-prove both the
    # live and reconstructed economic states immediately before the final
    # equality decision so a callback/concurrent mutation cannot be laundered
    # into a successful restart reconciliation.
    _require_minted(self, prepared)
    try:
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(self.book)
        _ORIGINAL_PAPERBOOK_VALIDATE_LOADED_STATE(expected)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "PAPER recovery state changed or became invalid during reconstruction"
        ) from exc
    if not _ORIGINAL_SAME_BOOK_STATE(self.book, expected):
        raise PaperExecutionAdoptionError(
            "PaperBook restart state is not the exact pre-action or "
            "#623-authorized post-action state"
        )


def _install() -> None:
    marker = "_autosport_lay_adoption_guard"
    if getattr(PaperExecutionAdoptionRuntime, marker, False):
        return
    PaperExecutionAdoptionRuntime.__init__ = _init
    PaperExecutionAdoptionRuntime._mint_prepared = _mint_prepared
    PaperExecutionAdoptionRuntime._require_minted = _require_minted
    PaperExecutionAdoptionRuntime.expected_run_id = _expected_run_id
    PaperExecutionAdoptionRuntime.execute = _execute
    PaperExecutionAdoptionRuntime._execute_unlocked = _execute_unlocked
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
