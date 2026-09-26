from __future__ import annotations

import sys
from hashlib import sha256
from typing import Any, Callable

from . import _paper_execution_append_recovery as _append_recovery
from . import _paper_execution_reality_legacy as _ledger_impl
from . import _paper_value_execution_authority as _value_authority
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from .paper_execution_reality import (
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
)


_RESERVED_EVENT_TYPE = "PAPER_EXPOSURE_SCOPE_BOUND"
_RESERVED_SCHEMA = "autosport.paper_execution.exposure_scope_binding"
_RESERVED_SCHEMA_VERSION = 1
_EMPTY_CELL = object()


def _snapshot_function_globals(
    function: Callable[..., object],
) -> tuple[tuple[tuple[str, object], ...], object]:
    globals_dict = function.__globals__
    bindings = tuple(
        (name, globals_dict[name])
        for name in function.__code__.co_names
        if name in globals_dict
    )
    return bindings, globals_dict.get("__builtins__")


def _function_globals_match(
    function: Callable[..., object],
    snapshot: tuple[tuple[tuple[str, object], ...], object],
) -> bool:
    bindings, builtins_binding = snapshot
    globals_dict = function.__globals__
    if globals_dict.get("__builtins__") is not builtins_binding:
        return False
    return all(
        name in globals_dict and globals_dict[name] is expected
        for name, expected in bindings
    )


def _snapshot_function_metadata(
    function: Callable[..., object],
) -> tuple[object, object, object, object]:
    defaults = function.__defaults__
    frozen_defaults = None if defaults is None else tuple(defaults)
    kwdefaults = function.__kwdefaults__
    frozen_kwdefaults = (
        None
        if kwdefaults is None
        else tuple(sorted(kwdefaults.items(), key=lambda item: item[0]))
    )
    closure = function.__closure__
    if closure is None:
        frozen_closure = None
    else:
        values: list[object] = []
        for cell in closure:
            try:
                values.append(cell.cell_contents)
            except ValueError:
                values.append(_EMPTY_CELL)
        frozen_closure = tuple(values)
    return function.__code__, frozen_defaults, frozen_kwdefaults, frozen_closure


def _function_metadata_match(
    function: Callable[..., object],
    snapshot: tuple[object, object, object, object],
) -> bool:
    expected_code, expected_defaults, expected_kwdefaults, expected_closure = snapshot
    if function.__code__ is not expected_code:
        return False

    live_defaults = function.__defaults__
    if expected_defaults is None:
        if live_defaults is not None:
            return False
    elif live_defaults is None or len(live_defaults) != len(expected_defaults):
        return False
    elif any(
        live is not expected
        for live, expected in zip(live_defaults, expected_defaults, strict=True)
    ):
        return False

    live_kwdefaults = function.__kwdefaults__
    if expected_kwdefaults is None:
        if live_kwdefaults is not None:
            return False
    else:
        expected_kw_map = dict(expected_kwdefaults)
        if live_kwdefaults is None or set(live_kwdefaults) != set(expected_kw_map):
            return False
        if any(
            live_kwdefaults[name] is not expected
            for name, expected in expected_kw_map.items()
        ):
            return False

    live_closure = function.__closure__
    if expected_closure is None:
        return live_closure is None
    if live_closure is None or len(live_closure) != len(expected_closure):
        return False
    for cell, expected in zip(live_closure, expected_closure, strict=True):
        try:
            live = cell.cell_contents
        except ValueError:
            live = _EMPTY_CELL
        if live is not expected:
            return False
    return True


def bind_canonical_execute(execute_function):
    """Bind scope publication to the existing final PAPER execute authority.

    The caller-facing execute wrapper is still the decision-origin/callsite
    authority. This adds one outer frame witness and replaces only the dedicated
    scope publisher. It does not replace execute_paper_plan, reserve_run, the ledger
    append authority, recovery, or any real-money path.
    """

    runtime_type = PaperExecutionAdoptionRuntime
    ledger_type = PaperExecutionLedger
    prepared_type = PreparedPaperExecution

    if getattr(execute_function, "_autosport_exposure_scope_execute_guard", False):
        return execute_function

    mint_guard = runtime_type._mint_prepared
    prepare_guard = runtime_type.prepare
    prepare_paper_value_guard = runtime_type.prepare_paper_value_action
    require_minted = runtime_type._require_minted
    require_minted_globals = _snapshot_function_globals(require_minted)
    require_minted_metadata = _snapshot_function_metadata(require_minted)

    scope_descriptor = runtime_type.__dict__.get("_exposure_scope_payload")
    if not isinstance(scope_descriptor, classmethod):
        raise RuntimeError("canonical PAPER exposure-scope payload dispatch is unavailable")
    scope_function = scope_descriptor.__func__
    scope_globals = _snapshot_function_globals(scope_function)
    scope_metadata = _snapshot_function_metadata(scope_function)

    baseline_execute = _append_recovery._ORIGINAL_EXECUTE
    baseline_execute_globals = _snapshot_function_globals(baseline_execute)
    baseline_execute_metadata = _snapshot_function_metadata(baseline_execute)
    unlocked_execute = runtime_type._execute_unlocked
    unlocked_execute_globals = _snapshot_function_globals(unlocked_execute)
    unlocked_execute_metadata = _snapshot_function_metadata(unlocked_execute)
    expected_run_id = runtime_type.expected_run_id
    expected_run_id_globals = _snapshot_function_globals(expected_run_id)
    expected_run_id_metadata = _snapshot_function_metadata(expected_run_id)
    paper_value_execute = _value_authority._execute
    paper_value_execute_globals = _snapshot_function_globals(paper_value_execute)
    paper_value_execute_metadata = _snapshot_function_metadata(paper_value_execute)

    append_owner = next(
        (
            base
            for base in ledger_type.__mro__[1:]
            if "_append_event" in base.__dict__
        ),
        None,
    )
    if append_owner is None:
        raise RuntimeError("canonical PAPER lower ledger append is unavailable")
    lower_append = append_owner.__dict__["_append_event"]
    event_descriptor = append_owner.__dict__.get("_event")
    if not isinstance(event_descriptor, staticmethod):
        raise RuntimeError("canonical PAPER event constructor is unavailable")
    canonical_event = event_descriptor.__func__
    event_globals = _snapshot_function_globals(canonical_event)
    event_metadata = _snapshot_function_metadata(canonical_event)

    canonical_json = _ledger_impl._canonical
    canonical_json_globals = _snapshot_function_globals(canonical_json)
    canonical_json_metadata = _snapshot_function_metadata(canonical_json)
    ledger_schema_version = _ledger_impl._SCHEMA_VERSION
    fsync = _ledger_impl.os.fsync
    sha256_digest = sha256

    ledger_methods = {
        name: getattr(ledger_type, name)
        for name in (
            "_ensure_existing_path_durable",
            "_load_unlocked",
            "_write_anchor_unlocked",
            "_sync_parent_directory",
            "_with_writer_lock",
        )
    }

    getframe = sys._getframe

    def checked_canonical_json(value: object) -> str:
        if _ledger_impl._canonical is not canonical_json:
            raise PaperExecutionIntegrityError(
                "canonical PAPER ledger serializer dispatch was rebound"
            )
        if not _function_globals_match(canonical_json, canonical_json_globals):
            raise PaperExecutionIntegrityError(
                "canonical PAPER ledger serializer globals were rebound"
            )
        if not _function_metadata_match(canonical_json, canonical_json_metadata):
            raise PaperExecutionIntegrityError(
                "canonical PAPER ledger serializer metadata were rebound"
            )
        return canonical_json(value)

    def validate_payload(payload: object) -> dict[str, Any]:
        expected = {
            "schema",
            "schema_version",
            "plan_id",
            "plan_fingerprint",
            "intent_evidence_sha256",
            "bindings",
            "binding_sha256",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope payload schema is invalid"
            )
        if (
            payload["schema"] != _RESERVED_SCHEMA
            or payload["schema_version"] != _RESERVED_SCHEMA_VERSION
        ):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope payload version is invalid"
            )
        bindings = payload["bindings"]
        if type(bindings) is not list or not bindings:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope bindings must be non-empty"
            )
        expected_binding_keys = {"action_id", "sport", "bankroll_id", "currency"}
        for binding in bindings:
            if type(binding) is not dict or set(binding) != expected_binding_keys:
                raise PaperExecutionIntegrityError(
                    "canonical PAPER exposure-scope binding schema is invalid"
                )
            action_id = binding["action_id"]
            if (
                type(action_id) is not str
                or not action_id
                or action_id.strip() != action_id
            ):
                raise PaperExecutionIntegrityError(
                    "canonical PAPER exposure-scope action_id is invalid"
                )
            for name in ("sport", "bankroll_id", "currency"):
                value = binding[name]
                if value is not None and (
                    type(value) is not str
                    or not value
                    or value.strip() != value
                ):
                    raise PaperExecutionIntegrityError(
                        f"canonical PAPER exposure-scope {name} is invalid"
                    )
            if (binding["bankroll_id"] is None) != (binding["currency"] is None):
                raise PaperExecutionIntegrityError(
                    "canonical PAPER exposure-scope bankroll/currency binding is incomplete"
                )
        for name in (
            "plan_id",
            "plan_fingerprint",
            "intent_evidence_sha256",
            "binding_sha256",
        ):
            value = payload[name]
            if type(value) is not str or not value or value.strip() != value:
                raise PaperExecutionIntegrityError(
                    f"canonical PAPER exposure-scope {name} is invalid"
                )
        return payload

    # Reuse the already-canonical decision-origin execute function verbatim.
    # Adding another wrapper frame here changes the direct-caller authority
    # seen by the decision-origin guard and incorrectly turns valid product
    # execution into nested execution. Exposure-scope publication consumes
    # that authority; it must never replace it.
    canonical_execute = execute_function
    canonical_execute_code = execute_function.__code__

    def publish_owned_exposure_scope(
        self: PaperExecutionAdoptionRuntime,
        *,
        prepared: PreparedPaperExecution,
        run_id: str,
    ) -> None:
        if type(self) is not runtime_type or type(self.ledger) is not ledger_type:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure scope requires exact adoption runtime and ledger"
            )
        if runtime_type.execute is not canonical_execute:
            raise PaperExecutionIntegrityError(
                "canonical PAPER execution dispatch was rebound"
            )
        if runtime_type._publish_exposure_scope is not publish_owned_exposure_scope:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope publisher dispatch was rebound"
            )
        if runtime_type._mint_prepared is not mint_guard:
            raise PaperExecutionIntegrityError(
                "canonical prepared-execution mint dispatch was rebound"
            )
        if (
            runtime_type.prepare is not prepare_guard
            or runtime_type.prepare_paper_value_action is not prepare_paper_value_guard
        ):
            raise PaperExecutionIntegrityError(
                "canonical PAPER preparation dispatch was rebound"
            )
        if runtime_type._require_minted is not require_minted:
            raise PaperExecutionIntegrityError(
                "canonical prepared-execution verification was rebound"
            )
        if not _function_globals_match(require_minted, require_minted_globals):
            raise PaperExecutionAdoptionError(
                "canonical prepared-execution verification globals were rebound"
            )
        if not _function_metadata_match(require_minted, require_minted_metadata):
            raise PaperExecutionAdoptionError(
                "canonical prepared-execution verification metadata were rebound"
            )
        if type(prepared) is not prepared_type:
            raise TypeError("prepared must be exact PreparedPaperExecution")

        live_scope_descriptor = runtime_type.__dict__.get("_exposure_scope_payload")
        if (
            not isinstance(live_scope_descriptor, classmethod)
            or live_scope_descriptor.__func__ is not scope_function
        ):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope payload authority was rebound"
            )
        if not _function_globals_match(scope_function, scope_globals):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope payload globals were rebound"
            )
        if not _function_metadata_match(scope_function, scope_metadata):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope payload metadata were rebound"
            )

        if (
            ledger_type._append_event is not lower_append
            or append_owner.__dict__.get("_append_event") is not lower_append
        ):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope ledger dispatch was rebound"
            )
        if (
            append_owner.__dict__.get("_event") is not event_descriptor
            or "_event" in ledger_type.__dict__
        ):
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope event constructor dispatch was rebound"
            )
        if not _function_globals_match(canonical_event, event_globals):
            raise PaperExecutionIntegrityError(
                "canonical PAPER event constructor globals were rebound"
            )
        if not _function_metadata_match(canonical_event, event_metadata):
            raise PaperExecutionIntegrityError(
                "canonical PAPER event constructor metadata were rebound"
            )
        if _ledger_impl.os.fsync is not fsync:
            raise PaperExecutionIntegrityError(
                "canonical PAPER ledger durability dispatch was rebound"
            )
        for name, expected_method in ledger_methods.items():
            if (
                getattr(ledger_type, name) is not expected_method
                or name in getattr(self.ledger, "__dict__", {})
            ):
                raise PaperExecutionIntegrityError(
                    f"canonical PAPER ledger {name} dispatch was rebound"
                )

        if not _function_globals_match(baseline_execute, baseline_execute_globals):
            raise PaperExecutionAdoptionError(
                "canonical PAPER execution globals were rebound"
            )
        if not _function_metadata_match(baseline_execute, baseline_execute_metadata):
            raise PaperExecutionAdoptionError(
                "canonical PAPER execution metadata were rebound"
            )
        if not _function_globals_match(unlocked_execute, unlocked_execute_globals):
            raise PaperExecutionAdoptionError(
                "canonical PAPER unlocked execution globals were rebound"
            )
        if not _function_metadata_match(unlocked_execute, unlocked_execute_metadata):
            raise PaperExecutionAdoptionError(
                "canonical PAPER unlocked execution metadata were rebound"
            )
        if runtime_type._execute_unlocked is not unlocked_execute:
            raise PaperExecutionAdoptionError(
                "canonical PAPER unlocked execution dispatch was rebound"
            )
        if runtime_type.expected_run_id is not expected_run_id:
            raise PaperExecutionIntegrityError(
                "canonical PAPER run-id dispatch was rebound"
            )
        if not _function_globals_match(expected_run_id, expected_run_id_globals):
            raise PaperExecutionIntegrityError(
                "canonical PAPER run-id globals were rebound"
            )
        if not _function_metadata_match(expected_run_id, expected_run_id_metadata):
            raise PaperExecutionIntegrityError(
                "canonical PAPER run-id metadata were rebound"
            )

        current = getframe(0)
        unlocked_frame = current.f_back
        execute_frame = None if unlocked_frame is None else unlocked_frame.f_back
        cursor = None if execute_frame is None else execute_frame.f_back
        try:
            if (
                unlocked_frame is None
                or unlocked_frame.f_code is not unlocked_execute.__code__
                or unlocked_frame.f_locals.get("self") is not self
                or unlocked_frame.f_locals.get("prepared") is not prepared
                or execute_frame is None
                or execute_frame.f_code is not baseline_execute.__code__
                or execute_frame.f_locals.get("self") is not self
                or execute_frame.f_locals.get("prepared") is not prepared
            ):
                raise PaperExecutionIntegrityError(
                    "PAPER exposure-scope publication is reserved for canonical execution authority"
                )

            trigger_id = None
            saw_canonical_execute = False
            saw_paper_value_bridge = False
            while cursor is not None:
                if (
                    cursor.f_code is paper_value_execute.__code__
                    and cursor.f_locals.get("self") is self
                    and cursor.f_locals.get("authorized") is prepared
                ):
                    if (
                        _value_authority._execute is not paper_value_execute
                        or not _function_globals_match(
                            paper_value_execute,
                            paper_value_execute_globals,
                        )
                        or not _function_metadata_match(
                            paper_value_execute,
                            paper_value_execute_metadata,
                        )
                    ):
                        raise PaperExecutionIntegrityError(
                            "canonical paper-value execution bridge was rebound"
                        )
                    saw_paper_value_bridge = True
                if (
                    cursor.f_code is canonical_execute_code
                    and cursor.f_locals.get("self") is self
                    and (
                        cursor.f_locals.get("prepared") is prepared
                        or saw_paper_value_bridge
                    )
                ):
                    trigger_id = cursor.f_locals.get("trigger_id")
                    saw_canonical_execute = True
                    break
                cursor = cursor.f_back
            if not saw_canonical_execute or type(trigger_id) is not str:
                raise PaperExecutionIntegrityError(
                    "PAPER exposure-scope publication is reserved for canonical execution authority"
                )
        finally:
            del current
            del unlocked_frame
            del execute_frame
            del cursor

        expected_run = expected_run_id(self, prepared, trigger_id)
        if run_id != expected_run:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope run identity changed"
            )
        require_minted(self, prepared)

        body: dict[str, object] = {
            "schema": _RESERVED_SCHEMA,
            "schema_version": _RESERVED_SCHEMA_VERSION,
            "plan_id": prepared.execution_plan.plan_id,
            "plan_fingerprint": prepared.execution_plan.fingerprint,
            "intent_evidence_sha256": sha256_digest(
                prepared.intent_evidence_json.encode("utf-8")
            ).hexdigest(),
            "bindings": [
                {
                    "action_id": binding.action_id,
                    "sport": binding.sport,
                    "bankroll_id": binding.bankroll_id,
                    "currency": binding.currency,
                }
                for binding in prepared.exposure_bindings
            ],
        }
        payload = validate_payload(
            {
                **body,
                "binding_sha256": sha256_digest(
                    checked_canonical_json(body).encode("utf-8")
                ).hexdigest(),
            }
        )
        event_key = f"{run_id}:exposure-scope"

        ensure_existing = ledger_methods["_ensure_existing_path_durable"]
        load_unlocked = ledger_methods["_load_unlocked"]
        write_anchor = ledger_methods["_write_anchor_unlocked"]
        sync_parent = ledger_methods["_sync_parent_directory"]
        with_writer_lock = ledger_methods["_with_writer_lock"]

        def mutate_reserved_scope() -> None:
            ensure_existing(self.ledger)
            events = load_unlocked(self.ledger)
            by_key = {item["event_key"]: item for item in events}
            prior = by_key.get(event_key)
            sequence = len(events)
            previous_sha256 = None if not events else events[-1]["event_sha256"]
            event = canonical_event(
                event_type=_RESERVED_EVENT_TYPE,
                run_id=run_id,
                key=event_key,
                payload=payload,
                sequence=sequence,
                previous_sha256=previous_sha256,
            )
            if prior is not None:
                comparable = dict(prior)
                comparable.pop("sequence", None)
                comparable.pop("previous_sha256", None)
                comparable.pop("event_sha256", None)
                proposed = dict(event)
                proposed.pop("sequence", None)
                proposed.pop("previous_sha256", None)
                proposed.pop("event_sha256", None)
                if comparable != proposed:
                    raise PaperExecutionIntegrityError(
                        "event_key already has different payload"
                    )
                return

            encoded = checked_canonical_json(event) + "\n"
            path_existed_before = self.ledger.path.exists()
            try:
                with self.ledger.path.open(
                    "a",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(encoded)
                    handle.flush()
                    fsync(handle.fileno())
                if not path_existed_before or not self.ledger._path_durable:
                    sync_parent(self.ledger)
                write_anchor(self.ledger, events + [event])
            except OSError as exc:
                self.ledger._path_durable = False
                raise PaperExecutionIntegrityError(
                    "PAPER execution ledger durability barrier failed"
                ) from exc
            self.ledger._path_durable = True

        with_writer_lock(self.ledger, mutate_reserved_scope)

    canonical_execute._autosport_exposure_scope_execute_guard = True  # type: ignore[attr-defined]
    publish_owned_exposure_scope._autosport_exposure_scope_provenance_guard = True  # type: ignore[attr-defined]
    runtime_type._publish_exposure_scope = publish_owned_exposure_scope
    return canonical_execute


__all__ = ["bind_canonical_execute"]
