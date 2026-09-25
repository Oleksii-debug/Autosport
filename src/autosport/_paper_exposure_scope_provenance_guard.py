from __future__ import annotations

import sys
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
from typing import Any

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


def _install_guard() -> None:
    """Reserve exposure-scope publication without retaining a generic bypass callable."""

    ledger_type = PaperExecutionLedger
    runtime_type = PaperExecutionAdoptionRuntime
    prepared_type = PreparedPaperExecution
    integrity_error = PaperExecutionIntegrityError
    adoption_error = PaperExecutionAdoptionError

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

    event_descriptor = append_owner.__dict__.get("_event")
    if not isinstance(event_descriptor, staticmethod):
        raise RuntimeError("canonical PAPER event constructor is unavailable")
    canonical_event = event_descriptor.__func__
    if "_event" in ledger_type.__dict__ or ledger_type._event is not canonical_event:
        raise RuntimeError("canonical PAPER event constructor dispatch is inconsistent")

    current_append = ledger_type._append_event
    current_lower_append = append_owner.__dict__["_append_event"]
    current_mint = runtime_type._mint_prepared
    current_prepare = runtime_type.prepare
    current_prepare_paper_value = runtime_type.prepare_paper_value_action
    current_execute = runtime_type.execute
    current_execute_unlocked = runtime_type._execute_unlocked
    current_publish = runtime_type._publish_exposure_scope
    guarded = (
        current_append,
        current_lower_append,
        current_mint,
        current_prepare,
        current_prepare_paper_value,
        current_execute,
        current_execute_unlocked,
        current_publish,
    )
    installed = tuple(
        bool(getattr(method, "_autosport_exposure_scope_provenance_guard", False))
        for method in guarded
    )
    if all(installed):
        return
    if any(installed):
        raise RuntimeError("PAPER exposure-scope guard installation is inconsistent")
    if current_append is not current_lower_append:
        raise RuntimeError("PAPER public/lower ledger append dispatch is inconsistent")

    original_prepare = current_prepare
    original_prepare_paper_value = current_prepare_paper_value
    original_execute = current_execute
    original_require_minted = runtime_type._require_minted
    original_execute_unlocked = current_execute_unlocked
    scope_descriptor = runtime_type.__dict__.get("_exposure_scope_payload")
    if not isinstance(scope_descriptor, classmethod):
        raise RuntimeError("canonical PAPER exposure-scope payload dispatch is unavailable")
    original_scope_payload = scope_descriptor.__func__

    nested_paper_value_prepare_code = (
        _value_authority._ORIGINAL_PREPARE_PAPER_VALUE_ACTION.__code__
    )
    authorize_descriptor_code = _value_authority._authorize_descriptor.__code__
    verify_general_risk_admission_code = (
        _value_authority._verify_general_risk_admission.__code__
    )
    value_authority_globals = _value_authority.__dict__
    mint_token = object()
    mint_context: ContextVar[object | None] = ContextVar(
        "autosport_paper_exposure_scope_prepared_mint",
        default=None,
    )
    execution_token = object()
    execution_context: ContextVar[object | None] = ContextVar(
        "autosport_paper_exposure_scope_execution",
        default=None,
    )
    getframe = sys._getframe
    canonical_json = _ledger_impl._canonical
    ledger_schema_version = _ledger_impl._SCHEMA_VERSION
    fsync = _ledger_impl.os.fsync
    sha256_digest = sha256
    reserved_event_type = _RESERVED_EVENT_TYPE
    reserved_schema = _RESERVED_SCHEMA
    reserved_schema_version = _RESERVED_SCHEMA_VERSION
    
    def snapshot_function_globals(
        function: Any,
    ) -> tuple[tuple[tuple[str, object], ...], object]:
        globals_dict = function.__globals__
        bindings = tuple(
            (name, globals_dict[name])
            for name in function.__code__.co_names
            if name in globals_dict
        )
        return bindings, globals_dict.get("__builtins__")

    def function_globals_match(
        function: Any,
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

    original_prepare_globals = snapshot_function_globals(original_prepare)
    original_prepare_paper_value_globals = snapshot_function_globals(
        original_prepare_paper_value
    )
    original_execute_globals = snapshot_function_globals(original_execute)
    original_require_minted_globals = snapshot_function_globals(original_require_minted)
    original_execute_unlocked_globals = snapshot_function_globals(original_execute_unlocked)
    original_scope_payload_globals = snapshot_function_globals(original_scope_payload)
    canonical_json_globals = snapshot_function_globals(canonical_json)

    def checked_canonical_json(value: object) -> str:
        if not function_globals_match(canonical_json, canonical_json_globals):
            raise integrity_error("canonical PAPER ledger serializer globals were rebound")
        return canonical_json(value)

    def canonical_text(value: object, name: str) -> str:
        if (
            type(value) is not str
            or not value
            or value.strip() != value
            or "\x00" in value
        ):
            raise ValueError(f"{name} must be non-empty canonical text")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{name} must be UTF-8 encodable") from exc
        return value

    def build_event(
        *,
        event_type: str,
        run_id: str,
        key: str,
        payload: dict[str, Any],
        sequence: int,
        previous_sha256: str | None,
    ) -> dict[str, Any]:
        body = {
            "schema_version": ledger_schema_version,
            "event_type": canonical_text(event_type, "event_type"),
            "run_id": canonical_text(run_id, "run_id"),
            "event_key": canonical_text(key, "event_key"),
            "sequence": sequence,
            "previous_sha256": previous_sha256,
            "payload": payload,
        }
        event_sha256 = sha256_digest(
            checked_canonical_json(body).encode("utf-8")
        ).hexdigest()
        return {**body, "event_sha256": event_sha256}

    def validate_owned_scope_payload(payload: object) -> dict[str, Any]:
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
            raise integrity_error("canonical PAPER exposure-scope payload schema is invalid")
        if (
            payload["schema"] != reserved_schema
            or payload["schema_version"] != reserved_schema_version
        ):
            raise integrity_error("canonical PAPER exposure-scope payload version is invalid")
        bindings = payload["bindings"]
        if type(bindings) is not list or not bindings:
            raise integrity_error("canonical PAPER exposure-scope bindings must be non-empty")
        expected_binding_keys = {"action_id", "sport", "bankroll_id", "currency"}
        for binding in bindings:
            if type(binding) is not dict or set(binding) != expected_binding_keys:
                raise integrity_error("canonical PAPER exposure-scope binding schema is invalid")
            action_id = binding["action_id"]
            if type(action_id) is not str or not action_id or action_id.strip() != action_id:
                raise integrity_error("canonical PAPER exposure-scope action_id is invalid")
            for name in ("sport", "bankroll_id", "currency"):
                value = binding[name]
                if value is not None and (
                    type(value) is not str or not value or value.strip() != value
                ):
                    raise integrity_error(
                        f"canonical PAPER exposure-scope {name} is invalid"
                    )
            if (binding["bankroll_id"] is None) != (binding["currency"] is None):
                raise integrity_error(
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
                raise integrity_error(f"canonical PAPER exposure-scope {name} is invalid")
        return payload

    def guarded_append_event(
        self: PaperExecutionLedger,
        *,
        event_type: str,
        run_id: str,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == reserved_event_type:
            raise integrity_error(
                "PAPER_EXPOSURE_SCOPE_BOUND is reserved for canonical adoption authority"
            )

        def mutate() -> None:
            self._ensure_existing_path_durable()
            events = self._load_unlocked()
            by_key = {item["event_key"]: item for item in events}
            prior = by_key.get(key)
            sequence = len(events)
            previous_sha256 = None if not events else events[-1]["event_sha256"]
            event = build_event(
                event_type=event_type,
                run_id=run_id,
                key=key,
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
                    raise integrity_error("event_key already has different payload")
                return
            encoded = checked_canonical_json(event) + "\n"
            path_existed_before = self.path.exists()
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    fsync(handle.fileno())
                if not path_existed_before or not self._path_durable:
                    self._sync_parent_directory()
                self._write_anchor_unlocked(events + [event])
            except OSError as exc:
                self._path_durable = False
                raise integrity_error(
                    "PAPER execution ledger durability barrier failed"
                ) from exc
            self._path_durable = True

        self._with_writer_lock(mutate)

    def guarded_mint_prepared(
        self: PaperExecutionAdoptionRuntime,
        prepared: PreparedPaperExecution,
    ) -> PreparedPaperExecution:
        if type(self) is not runtime_type:
            raise adoption_error("canonical PAPER preparation requires exact runtime")
        if runtime_type._mint_prepared is not guarded_mint_prepared:
            raise adoption_error("canonical prepared-execution mint dispatch was rebound")
        caller = getframe(1)
        caller_code = caller.f_code
        if caller_code is original_prepare.__code__:
            if mint_context.get() is not mint_token:
                raise adoption_error(
                    "prepared execution mint is reserved for canonical preparation authority"
                )
            globals_ok = function_globals_match(
                original_prepare,
                original_prepare_globals,
            )
        elif caller_code is original_prepare_paper_value.__code__:
            if mint_context.get() is not mint_token:
                raise adoption_error(
                    "prepared execution mint is reserved for canonical preparation authority"
                )
            globals_ok = function_globals_match(
                original_prepare_paper_value,
                original_prepare_paper_value_globals,
            )
        elif caller_code is nested_paper_value_prepare_code:
            if (
                mint_context.get() is not mint_token
                or caller.f_globals
                is not _value_authority._ORIGINAL_PREPARE_PAPER_VALUE_ACTION.__globals__
            ):
                raise adoption_error(
                    "prepared execution mint is reserved for canonical preparation authority"
                )
            globals_ok = True
        elif caller_code is authorize_descriptor_code:
            if (
                caller.f_globals is not value_authority_globals
                or _value_authority._authorize_descriptor.__code__
                is not authorize_descriptor_code
            ):
                raise adoption_error(
                    "canonical PAPER descriptor authorization dispatch changed"
                )
            globals_ok = True
        elif caller_code is verify_general_risk_admission_code:
            if (
                caller.f_globals is not value_authority_globals
                or _value_authority._verify_general_risk_admission.__code__
                is not verify_general_risk_admission_code
            ):
                raise adoption_error(
                    "canonical PAPER recovery authorization dispatch changed"
                )
            globals_ok = True
        else:
            raise adoption_error(
                "prepared execution mint is reserved for canonical preparation authority"
            )
        if not globals_ok:
            raise adoption_error("canonical PAPER preparation globals were rebound")
        if type(prepared) is not prepared_type:
            raise TypeError("prepared must be exact PreparedPaperExecution")
        self._prepared_authorities[id(prepared)] = prepared
        return prepared

    def with_mint_authority(method: Any) -> Any:
        method_globals = (
            original_prepare_globals
            if method is original_prepare
            else original_prepare_paper_value_globals
        )

        @wraps(method)
        def owned(self: PaperExecutionAdoptionRuntime, *args: Any, **kwargs: Any) -> Any:
            if type(self) is not runtime_type:
                raise adoption_error("canonical PAPER preparation requires exact runtime")
            if runtime_type._mint_prepared is not guarded_mint_prepared:
                raise adoption_error("canonical prepared-execution mint dispatch was rebound")
            if not function_globals_match(method, method_globals):
                raise adoption_error("canonical PAPER preparation globals were rebound")
            marker = mint_context.set(mint_token)
            try:
                return method(self, *args, **kwargs)
            finally:
                mint_context.reset(marker)

        return owned

    owned_prepare = with_mint_authority(original_prepare)
    owned_prepare_paper_value = with_mint_authority(original_prepare_paper_value)

    @wraps(original_execute)
    def owned_execute(
        self: PaperExecutionAdoptionRuntime,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if type(self) is not runtime_type:
            raise adoption_error("canonical PAPER execution requires exact runtime")
        if runtime_type.execute is not owned_execute:
            raise adoption_error("canonical PAPER execution dispatch was rebound")
        if runtime_type._execute_unlocked is not owned_execute_unlocked:
            raise adoption_error("canonical PAPER unlocked execution dispatch was rebound")
        if not function_globals_match(original_execute, original_execute_globals):
            raise adoption_error("canonical PAPER execution globals were rebound")
        marker = execution_context.set(execution_token)
        try:
            return original_execute(self, *args, **kwargs)
        finally:
            execution_context.reset(marker)

    @wraps(original_execute_unlocked)
    def owned_execute_unlocked(
        self: PaperExecutionAdoptionRuntime,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if type(self) is not runtime_type:
            raise adoption_error("canonical PAPER execution requires exact runtime")
        if runtime_type.execute is not owned_execute:
            raise adoption_error("canonical PAPER execution dispatch was rebound")
        if runtime_type._execute_unlocked is not owned_execute_unlocked:
            raise adoption_error("canonical PAPER unlocked execution dispatch was rebound")
        if execution_context.get() is not execution_token:
            raise adoption_error(
                "unlocked PAPER execution is reserved for canonical execute authority"
            )
        caller = getframe(1)
        if (
            caller.f_code is not original_execute.__code__
            or caller.f_globals is not original_execute.__globals__
        ):
            raise adoption_error(
                "unlocked PAPER execution is reserved for canonical execute authority"
            )
        if not function_globals_match(
            original_execute_unlocked,
            original_execute_unlocked_globals,
        ):
            raise adoption_error("canonical PAPER unlocked execution globals were rebound")
        return original_execute_unlocked(self, *args, **kwargs)

    def publish_owned_exposure_scope(
        self: PaperExecutionAdoptionRuntime,
        *,
        prepared: PreparedPaperExecution,
        run_id: str,
    ) -> None:
        if type(self) is not runtime_type or type(self.ledger) is not ledger_type:
            raise integrity_error(
                "canonical PAPER exposure scope requires exact adoption runtime and ledger"
            )
        if runtime_type.execute is not owned_execute:
            raise integrity_error("canonical PAPER execution dispatch was rebound")
        if runtime_type._execute_unlocked is not owned_execute_unlocked:
            raise integrity_error("canonical PAPER unlocked execution dispatch was rebound")
        if execution_context.get() is not execution_token:
            raise integrity_error(
                "PAPER exposure-scope publication is reserved for canonical execution authority"
            )
        caller = getframe(1)
        if (
            caller.f_code is not original_execute_unlocked.__code__
            or caller.f_globals is not original_execute_unlocked.__globals__
        ):
            raise integrity_error(
                "PAPER exposure-scope publication is reserved for canonical execution authority"
            )
        if not function_globals_match(
            original_execute_unlocked,
            original_execute_unlocked_globals,
        ):
            raise integrity_error("canonical PAPER execution globals were rebound")
        if (
            ledger_type._append_event is not guarded_append_event
            or append_owner.__dict__.get("_append_event") is not guarded_append_event
        ):
            raise integrity_error("canonical PAPER exposure-scope ledger dispatch was rebound")
        if (
            append_owner.__dict__.get("_event") is not event_descriptor
            or "_event" in ledger_type.__dict__
            or "_event" in self.ledger.__dict__
        ):
            raise integrity_error(
                "canonical PAPER exposure-scope event constructor dispatch was rebound"
            )
        if runtime_type._publish_exposure_scope is not publish_owned_exposure_scope:
            raise integrity_error("canonical PAPER exposure-scope publisher dispatch was rebound")
        if runtime_type._mint_prepared is not guarded_mint_prepared:
            raise integrity_error("canonical prepared-execution mint dispatch was rebound")
        if runtime_type.prepare is not owned_prepare:
            raise integrity_error("canonical PAPER preparation dispatch was rebound")
        if runtime_type.prepare_paper_value_action is not owned_prepare_paper_value:
            raise integrity_error("canonical PAPER value preparation dispatch was rebound")
        if runtime_type._require_minted is not original_require_minted:
            raise integrity_error("canonical prepared-execution verification was rebound")
        live_scope_descriptor = runtime_type.__dict__.get("_exposure_scope_payload")
        if (
            not isinstance(live_scope_descriptor, classmethod)
            or live_scope_descriptor.__func__ is not original_scope_payload
        ):
            raise integrity_error("canonical PAPER exposure-scope payload authority was rebound")
        if not function_globals_match(
            original_require_minted,
            original_require_minted_globals,
        ):
            raise integrity_error(
                "canonical prepared-execution verification globals were rebound"
            )
        if not function_globals_match(
            original_scope_payload,
            original_scope_payload_globals,
        ):
            raise integrity_error(
                "canonical PAPER exposure-scope payload globals were rebound"
            )

        original_require_minted(self, prepared)

        # Re-derive the reserved payload inside the canonical publisher.  The
        # public classmethod remains a dispatch/tamper sentinel only; it is not
        # executed as authority because its module globals are mutable Python
        # state.  These exact captured primitives match paper_execution_adoption._digest.
        body: dict[str, object] = {
            "schema": reserved_schema,
            "schema_version": reserved_schema_version,
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
        payload = validate_owned_scope_payload(
            {
                **body,
                "binding_sha256": sha256_digest(
                    checked_canonical_json(body).encode("utf-8")
                ).hexdigest(),
            }
        )
        event_key = f"{run_id}:exposure-scope"

        # The generic ledger append rejects the reserved event unconditionally.
        # Persist the one product-owned reserved event here so there is no
        # inspectable generic reserved-event capability or writable publisher
        # code cell that an ordinary caller can retarget.
        def mutate_reserved_scope() -> None:
            self.ledger._ensure_existing_path_durable()
            events = self.ledger._load_unlocked()
            by_key = {item["event_key"]: item for item in events}
            prior = by_key.get(event_key)
            sequence = len(events)
            previous_sha256 = None if not events else events[-1]["event_sha256"]
            event = build_event(
                event_type=reserved_event_type,
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
                    raise integrity_error("event_key already has different payload")
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
                    self.ledger._sync_parent_directory()
                self.ledger._write_anchor_unlocked(events + [event])
            except OSError as exc:
                self.ledger._path_durable = False
                raise integrity_error(
                    "PAPER execution ledger durability barrier failed"
                ) from exc
            self.ledger._path_durable = True

        self.ledger._with_writer_lock(mutate_reserved_scope)

    for method in (
        guarded_append_event,
        guarded_mint_prepared,
        owned_prepare,
        owned_prepare_paper_value,
        owned_execute,
        owned_execute_unlocked,
        publish_owned_exposure_scope,
    ):
        method._autosport_exposure_scope_provenance_guard = True  # type: ignore[attr-defined]

    append_owner._append_event = guarded_append_event
    ledger_type._append_event = guarded_append_event
    runtime_type._mint_prepared = guarded_mint_prepared
    runtime_type.prepare = owned_prepare
    runtime_type.prepare_paper_value_action = owned_prepare_paper_value
    runtime_type.execute = owned_execute
    runtime_type._execute_unlocked = owned_execute_unlocked
    runtime_type._publish_exposure_scope = publish_owned_exposure_scope


_install_guard()
del _install_guard
