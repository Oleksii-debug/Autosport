from __future__ import annotations

from typing import Any

from .paper_execution_adoption import (
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

_ORIGINAL_LEDGER_APPEND = getattr(
    PaperExecutionLedger,
    "_autosport_exposure_scope_original_append_event",
    PaperExecutionLedger._append_event,
)
_ORIGINAL_RUNTIME_PUBLISH = getattr(
    PaperExecutionAdoptionRuntime,
    "_autosport_exposure_scope_original_publish",
    PaperExecutionAdoptionRuntime._publish_exposure_scope,
)


def _guarded_append_event(
    self: PaperExecutionLedger,
    *,
    event_type: str,
    run_id: str,
    key: str,
    payload: dict[str, Any],
) -> None:
    """Keep the exposure-scope event outside the generic ledger append surface."""

    if event_type == _RESERVED_EVENT_TYPE:
        raise PaperExecutionIntegrityError(
            "PAPER_EXPOSURE_SCOPE_BOUND is reserved for canonical adoption authority"
        )
    return _ORIGINAL_LEDGER_APPEND(
        self,
        event_type=event_type,
        run_id=run_id,
        key=key,
        payload=payload,
    )


def _validate_owned_scope_payload(payload: object) -> dict[str, Any]:
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
        if type(action_id) is not str or not action_id or action_id.strip() != action_id:
            raise PaperExecutionIntegrityError(
                "canonical PAPER exposure-scope action_id is invalid"
            )
        for name in ("sport", "bankroll_id", "currency"):
            value = binding[name]
            if value is not None and (
                type(value) is not str or not value or value.strip() != value
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


def _publish_owned_exposure_scope(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared: PreparedPaperExecution,
    run_id: str,
) -> None:
    """Publish one reserved scope only from the canonical adoption runtime path.

    The runtime's existing minted-prepared capability is checked before this guard
    bypasses the now-reserved generic append surface. The payload is then restricted
    to the exact v1 schema produced by the owning adoption runtime. Stable event-key
    conflict handling and durability remain owned by the existing canonical ledger.
    """

    if type(self.ledger) is not PaperExecutionLedger:
        raise PaperExecutionIntegrityError(
            "canonical PAPER exposure scope requires exact PaperExecutionLedger"
        )
    self._require_minted(prepared)
    payload = _validate_owned_scope_payload(self._exposure_scope_payload(prepared))
    _ORIGINAL_LEDGER_APPEND(
        self.ledger,
        event_type=_RESERVED_EVENT_TYPE,
        run_id=run_id,
        key=f"{run_id}:exposure-scope",
        payload=payload,
    )


if not getattr(
    PaperExecutionLedger,
    "_autosport_exposure_scope_provenance_guard_installed",
    False,
):
    PaperExecutionLedger._autosport_exposure_scope_original_append_event = (  # type: ignore[attr-defined]
        _ORIGINAL_LEDGER_APPEND
    )
    PaperExecutionLedger._append_event = _guarded_append_event
    PaperExecutionLedger._autosport_exposure_scope_provenance_guard_installed = True  # type: ignore[attr-defined]

if not getattr(
    PaperExecutionAdoptionRuntime,
    "_autosport_exposure_scope_provenance_guard_installed",
    False,
):
    PaperExecutionAdoptionRuntime._autosport_exposure_scope_original_publish = (  # type: ignore[attr-defined]
        _ORIGINAL_RUNTIME_PUBLISH
    )
    PaperExecutionAdoptionRuntime._publish_exposure_scope = _publish_owned_exposure_scope
    PaperExecutionAdoptionRuntime._autosport_exposure_scope_provenance_guard_installed = True  # type: ignore[attr-defined]
