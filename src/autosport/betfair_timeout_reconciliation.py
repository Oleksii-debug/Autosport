"""Betfair ambiguous-placement-timeout readback resolution.

Complete current+cleared readback is not, by itself, enough to issue NOT_FOUND
immediately after a `placeOrders` ambiguity. Betfair allows up to 15 seconds for a
timed-out order to become visible. This module composes that provider horizon with
the existing sealed #530 Betfair readback authority and the durable execution ledger.

The timeout boundary is never accepted from a caller. It is derived from the
ledger-verified ATTEMPT_UNKNOWN event recorded by the canonical Betfair execution
path, and the provider order reference is reloaded from the same durable attempt.

Definitive absence is also an in-process capability. A complete-empty provider
capture is not enough when it carries the product-issued provider reference: the
exact absence object must have passed this resolver after the durable visibility
deadline before generic execution reconciliation may consume it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import threading
from time import monotonic_ns
from weakref import ref

from .betfair_account_readonly import (
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyClient,
)
from .bookmaker_capability import BookmakerCapabilityProfile
from .real_execution_ledger import ExecutionAction, RealExecutionLedger
from .supervised_provider_evidence import (
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
    VerifiedProviderState,
    _register_betfair_timeout_absence_authority_assertion,
    verify_betfair_provider_state,
)

BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS = 15
BETFAIR_CLEARED_HISTORY_MAX_AGE_DAYS = 90
_CANONICAL_DATETIME = datetime
_BETFAIR_AMBIGUOUS_UNKNOWN_REASON = (
    "betfair_placeOrders_ambiguous_effect_requires_readback"
)


class BetfairTimeoutResolutionError(RuntimeError):
    """Raised when timeout/readback evidence cannot safely resolve provider state."""


class BetfairTimeoutResolutionKind(str, Enum):
    EFFECT_PRESENT = "effect_present"
    INDETERMINATE_BEFORE_VISIBILITY_HORIZON = (
        "indeterminate_before_visibility_horizon"
    )
    INDETERMINATE_OUTSIDE_CLEARED_HISTORY = (
        "indeterminate_outside_cleared_history"
    )
    ABSENT_AFTER_VISIBILITY_HORIZON = "absent_after_visibility_horizon"


@dataclass(frozen=True, slots=True)
class BetfairTimeoutResolution:
    kind: BetfairTimeoutResolutionKind
    timeout_boundary_at: str
    observed_at: str
    visibility_deadline: str
    ledger_snapshot_sha256: str
    evidence: VerifiedProviderState | None

    @property
    def definitive(self) -> bool:
        return self.kind in (
            BetfairTimeoutResolutionKind.EFFECT_PRESENT,
            BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON,
        )


def _time(value: str, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairTimeoutResolutionError(f"{name} must be non-empty canonical text")
    try:
        parsed = _CANONICAL_DATETIME.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairTimeoutResolutionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairTimeoutResolutionError(f"{name} must be timezone-aware")
    return parsed


def _install_betfair_readback_capture_start_authority() -> None:
    """Bind canonical readback object identity to its system capture-start instant.

    The existing BetfairReadOnlyClient origin seal remains authoritative for the
    readback payload itself.  This second, narrower seal records when that exact
    canonical capture began so a request started before the provider visibility
    horizon cannot become negative authority merely because transport latency makes
    its response timestamps cross the deadline.
    """

    issued: dict[int, tuple[object, str, int]] = {}
    raw_read = BetfairReadOnlyClient.read_execution_readback
    capture_datetime = datetime
    capture_timezone_utc = timezone.utc
    capture_monotonic_ns = monotonic_ns

    def authoritative_read(
        self: BetfairReadOnlyClient,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> BetfairExecutionReadbackEnvelope:
        capture_started_at = capture_datetime.now(capture_timezone_utc).isoformat()
        capture_started_monotonic_ns = capture_monotonic_ns()
        capture = raw_read(
            self,
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
            max_pages=max_pages,
        )
        capture_id = id(capture)

        def forget(_weakref: object, *, key: int = capture_id) -> None:
            issued.pop(key, None)

        issued[capture_id] = (
            ref(capture, forget),
            capture_started_at,
            capture_started_monotonic_ns,
        )
        return capture

    def capture_started_at(
        readback: BetfairExecutionReadbackEnvelope,
    ) -> str | None:
        if not isinstance(readback, BetfairExecutionReadbackEnvelope):
            return None
        record = issued.get(id(readback))
        if record is None or record[0]() is not readback:
            return None
        return record[1]

    def capture_started_monotonic_ns(
        readback: BetfairExecutionReadbackEnvelope,
    ) -> int | None:
        if not isinstance(readback, BetfairExecutionReadbackEnvelope):
            return None
        record = issued.get(id(readback))
        if record is None or record[0]() is not readback:
            return None
        return record[2]

    BetfairReadOnlyClient.read_execution_readback = authoritative_read
    globals()["_betfair_readback_capture_started_at"] = capture_started_at
    globals()[
        "_betfair_readback_capture_started_monotonic_ns"
    ] = capture_started_monotonic_ns


_install_betfair_readback_capture_start_authority()
del _install_betfair_readback_capture_start_authority


_timeout_elapsed_visibility_lock = threading.RLock()
_timeout_elapsed_visibility_anchors: dict[
    tuple[int, str], tuple[object, int]
] = {}


def _timeout_elapsed_visibility_ready(
    ledger: RealExecutionLedger,
    attempt_id: str,
    capture_started_monotonic_ns: int | None,
) -> bool:
    """Require one full in-process monotonic horizon before negative absence.

    Durable UTC timestamps remain the audit chronology, but a wall clock can jump
    forward.  The first canonical negative capture seen for one exact live ledger
    instance/attempt therefore establishes a process-local monotonic anchor and is
    never enough by itself.  Only a later fresh capture whose sealed request start is
    at least the provider visibility horizon after that anchor may contribute
    negative absence authority.

    The anchor is intentionally process-local.  Reopening the durable ledger after a
    restart creates a new object and therefore requires a fresh full monotonic
    horizon instead of pretending that monotonic time survived the process boundary.
    """

    if type(attempt_id) is not str or not attempt_id or attempt_id != attempt_id.strip():
        raise BetfairTimeoutResolutionError(
            "elapsed visibility requires canonical attempt_id"
        )
    if (
        type(capture_started_monotonic_ns) is not int
        or capture_started_monotonic_ns < 0
    ):
        return False

    key = (id(ledger), attempt_id)
    with _timeout_elapsed_visibility_lock:
        record = _timeout_elapsed_visibility_anchors.get(key)
        if record is None or record[0]() is not ledger:
            def forget(_weakref: object, *, anchor_key: tuple[int, str] = key) -> None:
                with _timeout_elapsed_visibility_lock:
                    _timeout_elapsed_visibility_anchors.pop(anchor_key, None)

            _timeout_elapsed_visibility_anchors[key] = (
                ref(ledger, forget),
                capture_started_monotonic_ns,
            )
            return False

        anchor_ns = record[1]
        if capture_started_monotonic_ns < anchor_ns:
            raise BetfairTimeoutResolutionError(
                "monotonic capture clock regressed within one process"
            )
        required_ns = BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS * 1_000_000_000
        return capture_started_monotonic_ns - anchor_ns >= required_ns


def _retire_timeout_elapsed_visibility_anchor(
    ledger: RealExecutionLedger,
    attempt_id: str,
) -> None:
    """Release one process-local elapsed-time anchor after its authority is terminal."""

    key = (id(ledger), attempt_id)
    with _timeout_elapsed_visibility_lock:
        record = _timeout_elapsed_visibility_anchors.get(key)
        if record is not None and record[0]() is ledger:
            _timeout_elapsed_visibility_anchors.pop(key, None)


def _absence_capture_page_times(
    readback: BetfairExecutionReadbackEnvelope,
) -> list[str]:
    """Return all canonical current/cleared page observation times."""

    if not isinstance(readback, BetfairExecutionReadbackEnvelope):
        raise BetfairTimeoutResolutionError(
            "absence visibility requires canonical Betfair execution readback"
        )
    observed: list[str] = [
        page.evidence.observed_at for page in readback.current_pages
    ]
    for _, pages in readback.cleared_pages_by_status:
        observed.extend(page.evidence.observed_at for page in pages)
    if not observed:
        raise BetfairTimeoutResolutionError(
            "absence visibility requires non-empty current/cleared page evidence"
        )
    return observed


def _absence_capture_floor(readback: BetfairExecutionReadbackEnvelope) -> str:
    """Return the earliest order-scope read in one canonical absence capture."""

    return min(
        _absence_capture_page_times(readback),
        key=lambda value: _time(value, "provider order-scope page observed_at"),
    )


def _absence_capture_ceiling(readback: BetfairExecutionReadbackEnvelope) -> str:
    """Return the latest order-scope read in one canonical absence capture."""

    return max(
        _absence_capture_page_times(readback),
        key=lambda value: _time(value, "provider order-scope page observed_at"),
    )


def _durable_timeout_authority(
    ledger: RealExecutionLedger,
    action: ExecutionAction,
    attempt_id: str,
) -> tuple[str, str, str]:
    """Return provider ref, timeout boundary and SHA from one verified ledger view.

    `recorded_at` is deliberately used instead of the ATTEMPT_UNKNOWN payload's
    caller-supplied `observed_at`. The ledger writes `recorded_at` itself while
    appending the durable event; using that later boundary can only delay absence.

    All authority-bearing attempt facts are derived from the same immutable verified
    snapshot.  In particular, no separate state/reference read may be combined with
    a later snapshot, because a concurrent terminal transition would create a
    mixed-time authority view.
    """

    if type(ledger) is not RealExecutionLedger:
        raise BetfairTimeoutResolutionError("ledger must be exact RealExecutionLedger")
    if type(action) is not ExecutionAction:
        raise BetfairTimeoutResolutionError("action must be exact ExecutionAction")
    if type(attempt_id) is not str or not attempt_id or attempt_id != attempt_id.strip():
        raise BetfairTimeoutResolutionError("attempt_id must be non-empty canonical text")

    snapshot = ledger.verified_snapshot()
    plan_events: list[dict[str, object]] = []
    attempt_events: list[dict[str, object]] = []
    unknown_events: list[dict[str, object]] = []
    reserved_events: list[dict[str, object]] = []
    submitted_events: list[dict[str, object]] = []
    provider_reference_events: list[dict[str, object]] = []
    terminal_or_found_events: list[dict[str, object]] = []
    try:
        for raw_line in snapshot.payload.splitlines():
            envelope = json.loads(raw_line.decode("utf-8"))
            event = envelope["event"]
            event_type = event.get("event_type")
            if event_type == "PLAN_RESERVED":
                plan_events.append(event)
            if event.get("attempt_id") != attempt_id:
                continue
            attempt_events.append(event)
            if event_type == "ATTEMPT_RESERVED":
                reserved_events.append(event)
            elif event_type == "ATTEMPT_SUBMITTED":
                submitted_events.append(event)
            elif event_type == "ATTEMPT_UNKNOWN":
                unknown_events.append(event)
            elif event_type == "PROVIDER_ORDER_REFERENCE_BOUND":
                provider_reference_events.append(event)
            elif event_type in {
                "RECONCILED_FOUND",
                "EXTERNAL_ACKNOWLEDGEMENT",
                "RECONCILED_NOT_FOUND",
            }:
                terminal_or_found_events.append(event)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise BetfairTimeoutResolutionError(
            "verified execution ledger snapshot cannot be decoded"
        ) from exc

    if not attempt_events:
        raise BetfairTimeoutResolutionError("timeout attempt is not durable")
    if terminal_or_found_events:
        raise BetfairTimeoutResolutionError(
            "verified ledger snapshot is no longer a durable UNKNOWN attempt"
        )
    if len(reserved_events) != 1 or reserved_events[0].get("action_id") != action.action_id:
        raise BetfairTimeoutResolutionError(
            "timeout attempt does not bind the exact execution action"
        )
    reserved = reserved_events[0]
    plan_id = reserved.get("plan_id")
    matching_plans = [event for event in plan_events if event.get("plan_id") == plan_id]
    if len(matching_plans) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout attempt does not resolve one durable execution plan"
        )
    plan_payload = matching_plans[0].get("payload")
    if not isinstance(plan_payload, dict):
        raise BetfairTimeoutResolutionError(
            "durable execution plan payload is malformed"
        )
    durable_plan = plan_payload.get("plan")
    if not isinstance(durable_plan, dict):
        raise BetfairTimeoutResolutionError(
            "durable execution plan body is malformed"
        )
    durable_actions = durable_plan.get("actions")
    if not isinstance(durable_actions, list):
        raise BetfairTimeoutResolutionError(
            "durable execution plan actions are malformed"
        )
    action_matches = [
        item
        for item in durable_actions
        if isinstance(item, dict) and item.get("action_id") == action.action_id
    ]
    if len(action_matches) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout attempt does not resolve one durable execution action"
        )
    durable_action = action_matches[0]
    if durable_action != action.to_dict():
        raise BetfairTimeoutResolutionError(
            "caller execution action differs from durable execution plan action"
        )
    durable_bookmaker_id = durable_action.get("bookmaker_id")
    durable_account_id = durable_action.get("account_id")
    if not isinstance(durable_bookmaker_id, str):
        raise BetfairTimeoutResolutionError(
            "durable execution action bookmaker identity is malformed"
        )
    if not isinstance(durable_account_id, str):
        raise BetfairTimeoutResolutionError(
            "durable execution action account identity is malformed"
        )

    if len(provider_reference_events) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout attempt lacks one durable provider order reference"
        )
    provider_payload = provider_reference_events[0].get("payload")
    if not isinstance(provider_payload, dict):
        raise BetfairTimeoutResolutionError(
            "durable provider order reference payload is malformed"
        )
    provider_order_ref = provider_payload.get("provider_order_ref")
    if (
        provider_payload.get("provider_id") != durable_bookmaker_id
        or provider_payload.get("account_id") != durable_account_id
        or not isinstance(provider_order_ref, str)
        or not provider_order_ref
        or provider_order_ref != provider_order_ref.strip()
    ):
        raise BetfairTimeoutResolutionError(
            "durable provider order reference mismatches execution action"
        )

    if len(submitted_events) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout authority requires one durable provider submission boundary"
        )
    if len(unknown_events) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout resolution requires one durable UNKNOWN attempt boundary"
        )
    unknown = unknown_events[0]
    payload = unknown.get("payload")
    if not isinstance(payload, dict) or payload.get("reason") != _BETFAIR_AMBIGUOUS_UNKNOWN_REASON:
        raise BetfairTimeoutResolutionError(
            "UNKNOWN attempt is not a canonical ambiguous Betfair placeOrders timeout"
        )
    recorded_at = unknown.get("recorded_at")
    if not isinstance(recorded_at, str):
        raise BetfairTimeoutResolutionError(
            "durable timeout boundary lacks ledger recorded_at"
        )
    _time(recorded_at, "durable timeout recorded_at")
    return provider_order_ref, recorded_at, snapshot.sha256

def _resolve_betfair_timeout_provider_state_core(
    ledger: RealExecutionLedger,
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    attempt_id: str,
    expected_profile_sha256: str,
    readback: BetfairExecutionReadbackEnvelope,
) -> BetfairTimeoutResolution:
    """Resolve one ambiguous Betfair placement without premature NOT_FOUND.

    Positive canonical evidence wins immediately. Canonical complete-empty evidence
    remains indeterminate until the fixed Betfair visibility horizon has elapsed and
    every order-scope page in the capture was observed at/after that deadline.
    The exact boundary is inclusive.

    Provider/readback incompleteness, identity conflicts, wrong cleared-status
    coverage, or stale evidence continue to fail closed in the canonical verifier.
    """

    provider_order_ref, timeout_boundary_at, ledger_sha = _durable_timeout_authority(
        ledger, action, attempt_id
    )
    timeout_boundary = _time(timeout_boundary_at, "durable timeout recorded_at")
    evidence = verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=expected_profile_sha256,
        readback=readback,
        expected_provider_order_ref=provider_order_ref,
    )
    observed = _time(evidence.observed_at, "provider observed_at")
    if observed < timeout_boundary:
        raise BetfairTimeoutResolutionError(
            "provider readback predates durable ambiguous placement boundary"
        )

    deadline = timeout_boundary + timedelta(
        seconds=BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS
    )
    deadline_raw = deadline.isoformat()

    if isinstance(evidence, VerifiedProviderEffectEvidence):
        _retire_timeout_elapsed_visibility_anchor(ledger, attempt_id)
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.EFFECT_PRESENT,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            evidence,
        )
    if not isinstance(evidence, VerifiedProviderAbsenceEvidence):
        raise BetfairTimeoutResolutionError("provider verifier returned non-canonical state")

    capture_started_raw = _betfair_readback_capture_started_at(readback)
    capture_started = (
        None
        if capture_started_raw is None
        else _time(
            capture_started_raw,
            "provider order-scope capture started_at",
        )
    )
    capture_started_monotonic_ns = (
        _betfair_readback_capture_started_monotonic_ns(readback)
    )
    capture_floor = _time(
        _absence_capture_floor(readback),
        "provider order-scope capture floor",
    )
    capture_ceiling = _time(
        _absence_capture_ceiling(readback),
        "provider order-scope capture ceiling",
    )
    if (
        capture_started is None
        or capture_started < deadline
        or observed < deadline
        or capture_floor < deadline
    ):
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            None,
        )

    elapsed_visibility_ready = _timeout_elapsed_visibility_ready(
        ledger,
        attempt_id,
        capture_started_monotonic_ns,
    )
    if not elapsed_visibility_ready:
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            None,
        )

    cleared_history_deadline = timeout_boundary + timedelta(
        days=BETFAIR_CLEARED_HISTORY_MAX_AGE_DAYS
    )
    if (
        capture_started >= cleared_history_deadline
        or observed >= cleared_history_deadline
        or capture_ceiling >= cleared_history_deadline
    ):
        _retire_timeout_elapsed_visibility_anchor(ledger, attempt_id)
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.INDETERMINATE_OUTSIDE_CLEARED_HISTORY,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            None,
        )

    return BetfairTimeoutResolution(
        BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON,
        timeout_boundary_at,
        evidence.observed_at,
        deadline_raw,
        ledger_sha,
        evidence,
    )


# A raw complete-empty Betfair capture is not retry authority for the product-issued
# per-instruction reference. The exact absence object is separately sealed only when
# the durable timeout resolver has observed it at/after the provider visibility
# deadline. Legacy generic evidence with no provider-order binding keeps its existing
# semantics; if a ledger attempt has a durable ref, downstream exact-ref validation
# rejects such unbound evidence before a transition.
def _install_betfair_timeout_absence_authority() -> None:
    issued: dict[
        int,
        tuple[
            object,
            tuple[int, str],
            object,
        ],
    ] = {}
    issued_lock = threading.RLock()
    raw_resolve = _resolve_betfair_timeout_provider_state_core
    raw_resolve_code = raw_resolve.__code__
    sealed_error = BetfairTimeoutResolutionError
    sealed_kind = BetfairTimeoutResolutionKind
    sealed_absence_type = VerifiedProviderAbsenceEvidence
    sealed_retire_anchor = _retire_timeout_elapsed_visibility_anchor
    sealed_ledger_descriptors = {
        "attempt_state": RealExecutionLedger.attempt_state,
        "verified_snapshot": RealExecutionLedger.verified_snapshot,
        "provider_order_reference": RealExecutionLedger.provider_order_reference,
    }
    sealed_action_descriptors = {
        "to_dict": ExecutionAction.to_dict,
    }
    sealed_ledger_descriptor_codes = {
        name: getattr(value, "__code__", None)
        for name, value in sealed_ledger_descriptors.items()
    }
    sealed_action_descriptor_codes = {
        name: getattr(value, "__code__", None)
        for name, value in sealed_action_descriptors.items()
    }
    missing = object()

    def seal_function_graph(root: object) -> dict[str, tuple[object, object | None]]:
        module_globals = globals()
        sealed: dict[str, tuple[object, object | None]] = {}
        pending = [root]
        visited: set[int] = set()
        while pending:
            function = pending.pop()
            if id(function) in visited:
                continue
            visited.add(id(function))
            code = getattr(function, "__code__", None)
            function_globals = getattr(function, "__globals__", None)
            if code is None or function_globals is not module_globals:
                continue
            for name in code.co_names:
                if name not in module_globals or name in sealed:
                    continue
                value = module_globals[name]
                value_code = getattr(value, "__code__", None)
                sealed[name] = (value, value_code)
                if (
                    value_code is not None
                    and getattr(value, "__globals__", None) is module_globals
                ):
                    pending.append(value)
        return sealed

    sealed_resolver_graph = seal_function_graph(raw_resolve)
    sealed_wrapper_bindings = {
        "BetfairTimeoutResolutionError": sealed_error,
        "BetfairTimeoutResolutionKind": sealed_kind,
        "VerifiedProviderAbsenceEvidence": sealed_absence_type,
        "_retire_timeout_elapsed_visibility_anchor": sealed_retire_anchor,
    }

    def assert_executable_authority_intact() -> None:
        module_globals = globals()
        if raw_resolve.__code__ is not raw_resolve_code:
            raise sealed_error("timeout resolver executable code changed")
        for name, (expected, expected_code) in sealed_resolver_graph.items():
            current = module_globals.get(name, missing)
            if current is not expected:
                raise sealed_error(
                    f"timeout resolver executable authority changed: {name}"
                )
            if (
                expected_code is not None
                and getattr(current, "__code__", None) is not expected_code
            ):
                raise sealed_error(
                    f"timeout resolver executable code changed: {name}"
                )
        for name, expected in sealed_wrapper_bindings.items():
            if module_globals.get(name, missing) is not expected:
                raise sealed_error(
                    f"timeout resolver authority binding changed: {name}"
                )
        for name, expected in sealed_ledger_descriptors.items():
            current = getattr(RealExecutionLedger, name, missing)
            if (
                current is not expected
                or getattr(current, "__code__", None)
                is not sealed_ledger_descriptor_codes[name]
            ):
                raise sealed_error(
                    f"timeout ledger authority method changed: {name}"
                )
        for name, expected in sealed_action_descriptors.items():
            current = getattr(ExecutionAction, name, missing)
            if (
                current is not expected
                or getattr(current, "__code__", None)
                is not sealed_action_descriptor_codes[name]
            ):
                raise sealed_error(
                    f"timeout action authority method changed: {name}"
                )

    def authoritative_resolve(
        ledger: RealExecutionLedger,
        action: ExecutionAction,
        profile: BookmakerCapabilityProfile,
        *,
        attempt_id: str,
        expected_profile_sha256: str,
        readback: BetfairExecutionReadbackEnvelope,
    ) -> BetfairTimeoutResolution:
        assert_executable_authority_intact()
        result = raw_resolve(
            ledger,
            action,
            profile,
            attempt_id=attempt_id,
            expected_profile_sha256=expected_profile_sha256,
            readback=readback,
        )
        assert_executable_authority_intact()
        evidence = result.evidence
        if (
            result.kind is sealed_kind.ABSENT_AFTER_VISIBILITY_HORIZON
            and isinstance(evidence, sealed_absence_type)
        ):
            evidence_key = id(evidence)
            anchor_key = (id(ledger), attempt_id)
            ledger_weakref = ref(ledger)

            def forget(
                _weakref: object,
                *,
                key: int = evidence_key,
                elapsed_anchor_key: tuple[int, str] = anchor_key,
                elapsed_ledger_ref: object = ledger_weakref,
                elapsed_attempt_id: str = attempt_id,
            ) -> None:
                with issued_lock:
                    issued.pop(key, None)
                    current_ledger = elapsed_ledger_ref()
                    if current_ledger is None:
                        return
                    if any(
                        live_anchor_key == elapsed_anchor_key
                        and live_ledger_ref() is current_ledger
                        and live_evidence_ref() is not None
                        for (
                            live_evidence_ref,
                            live_anchor_key,
                            live_ledger_ref,
                        ) in issued.values()
                    ):
                        return
                    sealed_retire_anchor(
                        current_ledger,
                        elapsed_attempt_id,
                    )

            with issued_lock:
                issued[evidence_key] = (
                    ref(evidence, forget),
                    anchor_key,
                    ledger_weakref,
                )
        return result

    def assert_betfair_timeout_absence_authoritative(
        evidence: VerifiedProviderAbsenceEvidence,
    ) -> None:
        assert_executable_authority_intact()
        if not isinstance(evidence, sealed_absence_type):
            raise sealed_error("timeout absence evidence type is not canonical")
        if evidence.provider_order_ref is None:
            return
        with issued_lock:
            record = issued.get(id(evidence))
            if record is None or record[0]() is not evidence:
                raise sealed_error(
                    "provider absence did not pass durable Betfair timeout visibility authority"
                )

    _register_betfair_timeout_absence_authority_assertion(
        assert_betfair_timeout_absence_authoritative
    )
    globals()["resolve_betfair_timeout_provider_state"] = authoritative_resolve
    globals()[
        "assert_betfair_timeout_absence_authoritative"
    ] = assert_betfair_timeout_absence_authoritative


_install_betfair_timeout_absence_authority()
del _install_betfair_timeout_absence_authority