from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

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
    """Reserve exposure-scope publication without leaving a caller-visible bypass."""

    ledger_type = PaperExecutionLedger
    runtime_type = PaperExecutionAdoptionRuntime
    integrity_error = PaperExecutionIntegrityError
    adoption_error = PaperExecutionAdoptionError

    current_append = ledger_type._append_event
    current_mint = runtime_type._mint_prepared
    current_prepare = runtime_type.prepare
    current_prepare_paper_value = runtime_type.prepare_paper_value_action
    current_publish = runtime_type._publish_exposure_scope
    guarded = (
        current_append,
        current_mint,
        current_prepare,
        current_prepare_paper_value,
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

    # All authority-bearing bypass callables stay closure-hidden.  In particular,
    # never publish the original ledger append/mint methods back onto a public class
    # or module global: doing so would recreate the exact capability this guard is
    # meant to remove.
    original_append = current_append
    original_mint = current_mint
    original_prepare = current_prepare
    original_prepare_paper_value = current_prepare_paper_value
    original_require_minted = runtime_type._require_minted
    original_scope_payload = runtime_type._exposure_scope_payload
    mint_authority: ContextVar[PaperExecutionAdoptionRuntime | None] = ContextVar(
        "autosport_paper_exposure_scope_mint_authority",
        default=None,
    )
    reserved_event_type = _RESERVED_EVENT_TYPE
    reserved_schema = _RESERVED_SCHEMA
    reserved_schema_version = _RESERVED_SCHEMA_VERSION

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
        return original_append(
            self,
            event_type=event_type,
            run_id=run_id,
            key=key,
            payload=payload,
        )

    def guarded_mint_prepared(
        self: PaperExecutionAdoptionRuntime,
        prepared: PreparedPaperExecution,
    ) -> PreparedPaperExecution:
        if mint_authority.get() is not self:
            raise adoption_error(
                "prepared execution mint is reserved for canonical preparation authority"
            )
        if runtime_type._mint_prepared is not guarded_mint_prepared:
            raise adoption_error("canonical prepared-execution mint dispatch was rebound")
        return original_mint(self, prepared)

    def with_mint_authority(method: Any) -> Any:
        @wraps(method)
        def owned(self: PaperExecutionAdoptionRuntime, *args: Any, **kwargs: Any) -> Any:
            if type(self) is not runtime_type:
                raise adoption_error("canonical PAPER preparation requires exact runtime")
            if runtime_type._mint_prepared is not guarded_mint_prepared:
                raise adoption_error("canonical prepared-execution mint dispatch was rebound")
            token = mint_authority.set(self)
            try:
                return method(self, *args, **kwargs)
            finally:
                mint_authority.reset(token)

        return owned

    owned_prepare = with_mint_authority(original_prepare)
    owned_prepare_paper_value = with_mint_authority(original_prepare_paper_value)

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
        if ledger_type._append_event is not guarded_append_event:
            raise integrity_error("canonical PAPER exposure-scope ledger dispatch was rebound")
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
        if runtime_type._exposure_scope_payload is not original_scope_payload:
            raise integrity_error("canonical PAPER exposure-scope payload authority was rebound")

        original_require_minted(self, prepared)
        payload = validate_owned_scope_payload(original_scope_payload(self, prepared))
        original_append(
            self.ledger,
            event_type=reserved_event_type,
            run_id=run_id,
            key=f"{run_id}:exposure-scope",
            payload=payload,
        )

    for method in (
        guarded_append_event,
        guarded_mint_prepared,
        owned_prepare,
        owned_prepare_paper_value,
        publish_owned_exposure_scope,
    ):
        method._autosport_exposure_scope_provenance_guard = True  # type: ignore[attr-defined]

    ledger_type._append_event = guarded_append_event
    runtime_type._mint_prepared = guarded_mint_prepared
    runtime_type.prepare = owned_prepare
    runtime_type.prepare_paper_value_action = owned_prepare_paper_value
    runtime_type._publish_exposure_scope = publish_owned_exposure_scope


_install_guard()
del _install_guard
