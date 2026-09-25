from __future__ import annotations

import sys
from functools import wraps
from hashlib import sha256
from types import CodeType
from typing import Any

from . import _paper_execution_reality_legacy as _ledger_impl
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
    current_publish = runtime_type._publish_exposure_scope
    guarded = (
        current_append,
        current_lower_append,
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
    if current_append is not current_lower_append:
        raise RuntimeError("PAPER public/lower ledger append dispatch is inconsistent")

    # Do not retain current_lower_append/current_mint in any installed function state.
    # Python closures/defaults/__wrapped__ are inspectable, so hiding a generic bypass
    # there is not an authority boundary.  Generic append is reproduced below with the
    # existing canonical ledger primitives, while minting performs the tiny canonical
    # registry update inline only from the two exact preparation code paths.
    original_prepare = current_prepare
    original_prepare_paper_value = current_prepare_paper_value
    original_require_minted = runtime_type._require_minted
    scope_descriptor = runtime_type.__dict__.get("_exposure_scope_payload")
    if not isinstance(scope_descriptor, classmethod):
        raise RuntimeError("canonical PAPER exposure-scope payload dispatch is unavailable")
    original_scope_payload = scope_descriptor.__func__

    prepare_codes: tuple[CodeType, CodeType] = (
        original_prepare.__code__,
        original_prepare_paper_value.__code__,
    )
    getframe = sys._getframe
    canonical_json = _ledger_impl._canonical
    fsync = _ledger_impl.os.fsync
    sha256_digest = sha256
    reserved_event_type = _RESERVED_EVENT_TYPE
    reserved_schema = _RESERVED_SCHEMA
    reserved_schema_version = _RESERVED_SCHEMA_VERSION
    publisher_code: CodeType | None = None

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
        # The reserved event payload is validated by this guard before append.  Build
        # the exact canonical event here rather than redispatching through mutable
        # PaperExecutionLedger._event after that validation boundary.
        body = {
            "schema_version": _ledger_impl._SCHEMA_VERSION,
            "event_type": canonical_text(event_type, "event_type"),
            "run_id": canonical_text(run_id, "run_id"),
            "event_key": canonical_text(key, "event_key"),
            "sequence": sequence,
            "previous_sha256": previous_sha256,
            "payload": payload,
        }
        event_sha256 = sha256_digest(
            canonical_json(body).encode("utf-8")
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
        if event_type == reserved_event_type and getframe(1).f_code is not publisher_code:
            raise integrity_error(
                "PAPER_EXPOSURE_SCOPE_BOUND is reserved for canonical adoption authority"
            )

        # Inline the canonical legacy append algorithm rather than retaining the old
        # generic append function as an inspectable closure/default/wrapped callable.
        # The existing ledger owns locking, chain validation and durability barriers.
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
            encoded = canonical_json(event) + "\n"
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
        if getframe(1).f_code not in prepare_codes:
            raise adoption_error(
                "prepared execution mint is reserved for canonical preparation authority"
            )
        if type(prepared) is not PreparedPaperExecution:
            raise TypeError("prepared must be exact PreparedPaperExecution")
        self._prepared_authorities[id(prepared)] = prepared
        return prepared

    def with_mint_authority(method: Any) -> Any:
        @wraps(method)
        def owned(self: PaperExecutionAdoptionRuntime, *args: Any, **kwargs: Any) -> Any:
            if type(self) is not runtime_type:
                raise adoption_error("canonical PAPER preparation requires exact runtime")
            if runtime_type._mint_prepared is not guarded_mint_prepared:
                raise adoption_error("canonical prepared-execution mint dispatch was rebound")
            return method(self, *args, **kwargs)

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

        original_require_minted(self, prepared)
        payload = validate_owned_scope_payload(
            original_scope_payload(runtime_type, prepared)
        )
        guarded_append_event(
            self.ledger,
            event_type=reserved_event_type,
            run_id=run_id,
            key=f"{run_id}:exposure-scope",
            payload=payload,
        )

    publisher_code = publish_owned_exposure_scope.__code__

    for method in (
        guarded_append_event,
        guarded_mint_prepared,
        owned_prepare,
        owned_prepare_paper_value,
        publish_owned_exposure_scope,
    ):
        method._autosport_exposure_scope_provenance_guard = True  # type: ignore[attr-defined]

    # Fence the actual lower owner as well as the public subclass. Otherwise a
    # caller can bypass the reservation by invoking the inherited owner directly.
    append_owner._append_event = guarded_append_event
    ledger_type._append_event = guarded_append_event
    runtime_type._mint_prepared = guarded_mint_prepared
    runtime_type.prepare = owned_prepare
    runtime_type.prepare_paper_value_action = owned_prepare_paper_value
    runtime_type._publish_exposure_scope = publish_owned_exposure_scope


_install_guard()
del _install_guard
