from __future__ import annotations

from contextvars import ContextVar
import hashlib
import inspect
import json
import math

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_reality_legacy as _legacy_reality
from . import paper_execution_reality as _paper_reality
from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionRuntime
from .paper_execution_reality import (
    PaperExecutionLedger,
    PaperExecutionStateError,
)


_VERIFIED_SNAPSHOT_SENTINEL = "_autosport_decision_origin_pristine_verified_snapshot"
_RESERVE_SENTINEL = "_autosport_decision_origin_pristine_reserve_run"
_APPEND_SENTINEL = "_autosport_decision_origin_pristine_append_event"
_RUNTIME_EXECUTE_CODE_SENTINEL = (
    "_autosport_decision_origin_instance_guard_pristine_runtime_execute_code"
)
_EXECUTE_PLAN_CODE_SENTINEL = (
    "_autosport_decision_origin_instance_guard_pristine_execute_plan_code"
)
_CALLSITE_EXECUTE_CODE_SENTINEL = (
    "_autosport_decision_origin_pristine_product_callsite_code"
)
_SEAL_MARKER = "autosport.paper_execution_decision_origin.instance_guard.seal.v4"
_SEAL_PREFIX = "autosport.paper_execution_decision_origin.instance_guard.seal."

# Preserve the exact context identities across importlib.reload. Installed guard
# closures keep these objects as their execution-capability channel; replacing a
# module attribute must never manufacture a new authority channel.
if "_PRODUCT_ORIGIN_RUNTIME" not in globals():
    _PRODUCT_ORIGIN_RUNTIME: ContextVar[PaperExecutionAdoptionRuntime | None] = ContextVar(
        "autosport_paper_execution_product_origin_runtime",
        default=None,
    )
if "_PRODUCT_ORIGIN_CALLSITE_CODE" not in globals():
    _PRODUCT_ORIGIN_CALLSITE_CODE: ContextVar[object | None] = ContextVar(
        "autosport_paper_execution_product_origin_callsite_code",
        default=None,
    )


def _instance_shadows(obj: object, method_name: str) -> bool:
    namespace = getattr(obj, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _classmethod_descriptor(name: str) -> classmethod:
    descriptor = JsonlDecisionLedger.__dict__.get(name)
    if not isinstance(descriptor, classmethod):
        raise RuntimeError(f"DecisionLedger canonical classmethod {name} is unavailable")
    return descriptor


def _staticmethod_descriptor(name: str) -> staticmethod:
    descriptor = JsonlDecisionLedger.__dict__.get(name)
    if not isinstance(descriptor, staticmethod):
        raise RuntimeError(f"DecisionLedger canonical staticmethod {name} is unavailable")
    return descriptor


def _verify_bytes_descriptor() -> classmethod:
    return _classmethod_descriptor("_verify_bytes")


def _initial_seal():
    """Re-derive primitive authority from the canonical owning implementations."""

    return (
        _SEAL_MARKER,
        JsonlDecisionLedger.verified_snapshot,
        _verify_bytes_descriptor(),
        _legacy_reality.PaperExecutionLedger.reserve_run,
        _legacy_reality.PaperExecutionLedger._append_event,
        _paper_reality.execute_paper_plan.__code__,
        _staticmethod_descriptor("_json_object_without_duplicate_keys"),
        _staticmethod_descriptor("_reject_non_finite_json"),
        _classmethod_descriptor("_validate_record"),
        _staticmethod_descriptor("_canonical_record"),
        _classmethod_descriptor("_validate_json_value"),
        _staticmethod_descriptor("_require_utf8_text"),
        JsonlDecisionLedger._ENVELOPE_FIELDS,
        JsonlDecisionLedger._LEGACY_RECORD_FIELDS,
        JsonlDecisionLedger._ECONOMIC_RECORD_FIELDS,
        JsonlDecisionLedger._STRING_FIELDS,
    )


def _seal_matches_canonical(value: object) -> bool:
    if type(value) is not tuple or len(value) != 16 or value[0] != _SEAL_MARKER:
        return False
    canonical = _initial_seal()
    return all(value[index] is canonical[index] for index in range(1, len(canonical)))


def _sealed_tuple_from_callable(candidate: object):
    """Recover only an unchanged seal; flag legacy/rebound seal cells for repair."""

    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None, False
    saw_guard_seal = False
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if (
            type(value) is tuple
            and value
            and type(value[0]) is str
            and value[0].startswith(_SEAL_PREFIX)
        ):
            saw_guard_seal = True
            if _seal_matches_canonical(value):
                return value, False
    return None, saw_guard_seal


def _build_guard(seal):
    # Capture the capability contexts in this installed guard closure. Every
    # authority-bearing call revalidates the executable seal against the primitive
    # implementations owned by DecisionLedger / legacy PAPER ledger / execute-plan
    # code. A reflected closure-cell replacement therefore fails closed immediately,
    # and reload discards/rebuilds the altered wrapper instead of trusting it.
    product_origin_runtime = _PRODUCT_ORIGIN_RUNTIME
    product_origin_callsite_code = _PRODUCT_ORIGIN_CALLSITE_CODE

    # The origin verifier intentionally does not invoke JsonlDecisionLedger's public
    # mutable class dispatch once authority is being resolved. Keep one private,
    # closure-owned copy of the canonical byte contract. Public helper descriptors are
    # still part of ``seal`` so any rebind before/during verification fails closed,
    # but a racing attacker callable is never executed by this verifier.
    json_loads = json.loads
    json_dumps = json.dumps
    sha256 = hashlib.sha256
    isfinite = math.isfinite
    envelope_fields = seal[12]
    legacy_record_fields = seal[13]
    economic_record_fields = seal[14]
    string_fields = seal[15]
    forbidden_future_keys = frozenset(
        {"final_result", "result", "winner", "outcome", "settled_outcome", "future_quote"}
    )

    def require_canonical_seal() -> None:
        if not _seal_matches_canonical(seal):
            raise _origin.PaperExecutionDecisionOriginError(
                "decision-origin executable authority seal changed"
            )

    def canonical_text(value: str, *, path: str) -> None:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger JSON text at {path} is not valid UTF-8"
            ) from exc

    def validate_json_value(value: object, *, path: str) -> None:
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, str):
            canonical_text(value, path=path)
            return
        if isinstance(value, float):
            if not isfinite(value):
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON value at {path} is non-finite"
                )
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                validate_json_value(item, path=f"{path}[{index}]")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise DecisionLedgerIntegrityError(
                        f"Decision Ledger JSON object keys at {path} must be strings"
                    )
                canonical_text(key, path=f"{path} object key")
                child_path = "payload" if path == "record" and key == "payload" else f"{path}.{key}"
                validate_json_value(item, path=child_path)
            return
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger JSON value at {path} has unsupported type {type(value).__name__}"
        )

    def contains_forbidden_future_key(value: object) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in forbidden_future_keys:
                    return True
                if contains_forbidden_future_key(child):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(contains_forbidden_future_key(child) for child in value)
        return False

    def validate_record(
        record: object,
        *,
        line_number: int | None = None,
    ) -> dict[str, object]:
        location = f" at line {line_number}" if line_number is not None else ""
        if not isinstance(record, dict):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record schema is invalid{location}"
            )
        fields = frozenset(record)
        if fields not in {legacy_record_fields, economic_record_fields}:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record schema is invalid{location}"
            )
        try:
            validate_json_value(record, path="record")
        except RecursionError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record nesting is too deep{location}"
            ) from exc
        for field_name in string_fields:
            value = record.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger record field {field_name!r} is invalid{location}"
                )
        if "decision_kind" in record and record["decision_kind"] != "ECONOMIC":
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger decision_kind is invalid{location}"
            )
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record field 'payload' is invalid{location}"
            )
        if contains_forbidden_future_key(payload):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger payload contains future-result fields{location}"
            )
        if "decision_kind" in record and "economic_goal_provenance" not in payload:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger ECONOMIC record is missing EconomicGoal provenance{location}"
            )
        return record

    def duplicate_free_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON object contains duplicate key {key!r}"
                )
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger JSON contains non-finite numeric value {value!r}"
        )

    def canonical_record(record: dict[str, object]) -> str:
        try:
            return json_dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger record is not canonical JSON"
            ) from exc

    def verify_bytes_without_public_dispatch(raw: bytes) -> int:
        if not raw:
            return 0
        if not raw.endswith(b"\n"):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger has an unterminated final record"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger is not valid UTF-8"
            ) from exc
        lines = text.split("\n")
        if lines[-1] != "":
            raise DecisionLedgerIntegrityError(
                "Decision Ledger has an unterminated final record"
            )

        seen_decision_ids: set[str] = set()
        line_count = 0
        for line_number, line in enumerate(lines[:-1], start=1):
            if not line:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains a blank record at line {line_number}"
                )
            try:
                envelope = json_loads(
                    line,
                    object_pairs_hook=duplicate_free_object,
                    parse_constant=reject_non_finite,
                )
            except json.JSONDecodeError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains invalid JSON at line {line_number}"
                ) from exc
            except RecursionError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON nesting is too deep at line {line_number}"
                ) from exc
            except DecisionLedgerIntegrityError as exc:
                raise DecisionLedgerIntegrityError(
                    f"{exc} at line {line_number}"
                ) from exc

            if not isinstance(envelope, dict) or set(envelope) != envelope_fields:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger envelope schema is invalid at line {line_number}"
                )
            digest = envelope.get("sha256")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger SHA-256 field is invalid at line {line_number}"
                )

            record = validate_record(envelope.get("record"), line_number=line_number)
            actual_digest = sha256(canonical_record(record).encode("utf-8")).hexdigest()
            if actual_digest != digest:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger SHA-256 mismatch at line {line_number}"
                )
            decision_id = str(record["decision_id"])
            if decision_id in seen_decision_ids:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains duplicate decision_id at line {line_number}"
                )
            seen_decision_ids.add(decision_id)
            line_count += 1
        return line_count

    def verified_decision_origin_without_instance_dispatch(
        ledger: JsonlDecisionLedger,
        decision_id: str,
    ) -> _origin.DecisionRecordOrigin:
        require_canonical_seal()
        if type(ledger) is not JsonlDecisionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin requires exact JsonlDecisionLedger authority"
            )
        for method_name in (
            "verified_snapshot",
            "_verify_bytes",
            "_json_object_without_duplicate_keys",
            "_reject_non_finite_json",
            "_validate_record",
            "_canonical_record",
            "_validate_json_value",
            "_require_utf8_text",
        ):
            if _instance_shadows(ledger, method_name):
                raise _origin.PaperExecutionDecisionOriginError(
                    f"decision ledger shadows authority method {method_name}"
                )
        if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
            raise ValueError("decision_id must be non-empty canonical text")

        try:
            raw = ledger.path.read_bytes()
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc

        # Revalidate after the filesystem read. Verification below is closure-local
        # and never dispatches through the public class, so a helper rebind cannot be
        # executed in the check/use interval. Revalidate once more after semantic
        # verification so a persistent concurrent class mutation still fails closed.
        require_canonical_seal()
        verify_bytes_without_public_dispatch(raw)
        require_canonical_seal()

        matches: list[_origin.DecisionRecordOrigin] = []
        for line in raw.decode("utf-8").splitlines():
            envelope = json_loads(line)
            record = envelope["record"]
            if record.get("decision_id") != decision_id:
                continue
            payload = record.get("payload")
            if type(payload) is not dict:
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin durable DecisionRecord payload is invalid"
                )
            raw_learning = payload.get("learning_observation")
            learning_json: str | None = None
            decision_observed_ts: str | None = None
            if raw_learning is not None:
                if type(raw_learning) is not dict:
                    raise _origin.PaperExecutionDecisionOriginError(
                        "decision origin learning observation commitment is invalid"
                    )
                try:
                    learning_json = json_dumps(
                        raw_learning,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise _origin.PaperExecutionDecisionOriginError(
                        "decision origin learning observation commitment is invalid"
                    ) from exc
                observed_ts = record.get("observed_ts")
                if type(observed_ts) is not str or not observed_ts:
                    raise _origin.PaperExecutionDecisionOriginError(
                        "decision origin durable DecisionRecord time is invalid"
                    )
                decision_observed_ts = observed_ts
            matches.append(
                _origin.DecisionRecordOrigin(
                    decision_id=decision_id,
                    record_sha256=envelope["sha256"],
                    learning_observation_json=learning_json,
                    decision_observed_ts=decision_observed_ts,
                )
            )
        if len(matches) != 1:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin requires exactly one verified durable DecisionLedger record"
            )
        require_canonical_seal()
        return matches[0]

    def require_canonical_product_reservation_path(
        ledger: PaperExecutionLedger,
    ) -> None:
        """Reject caller-injected ambient origin outside the exact product path."""

        require_canonical_seal()
        runtime = product_origin_runtime.get()
        if type(runtime) is not PaperExecutionAdoptionRuntime:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin context is not bound to canonical product execution"
            )
        expected_callsite_code = product_origin_callsite_code.get()
        if expected_callsite_code is None:
            raise _origin.PaperExecutionDecisionOriginError(
                "canonical product callsite identity is unavailable"
            )

        current = inspect.currentframe()
        reserve_frame = None
        execute_plan_frame = None
        cursor = None
        try:
            reserve_frame = current.f_back if current is not None else None
            execute_plan_frame = reserve_frame.f_back if reserve_frame is not None else None
            if (
                execute_plan_frame is None
                or execute_plan_frame.f_code is not seal[5]
                or execute_plan_frame.f_locals.get("ledger") is not ledger
            ):
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin reservation bypassed canonical execute_paper_plan"
                )

            saw_product_wrapper = False
            cursor = execute_plan_frame.f_back
            while cursor is not None:
                if (
                    cursor.f_code is expected_callsite_code
                    and cursor.f_locals.get("self") is runtime
                ):
                    saw_product_wrapper = True
                    break
                cursor = cursor.f_back
            if not saw_product_wrapper:
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin reservation lacks canonical product execution ancestry"
                )
        finally:
            del current
            del reserve_frame
            del execute_plan_frame
            del cursor

    class CanonicalReservationView:
        """Use canonical reserve validation while pinning the durable append sink."""

        __slots__ = ("_ledger", "_origin")

        def __init__(
            self,
            ledger: PaperExecutionLedger,
            origin: _origin.DecisionRecordOrigin,
        ) -> None:
            self._ledger = ledger
            self._origin = origin

        def _append_event(
            self,
            *,
            event_type: str,
            run_id: str,
            key: str,
            payload,
        ) -> None:
            require_canonical_seal()
            if event_type != "RUN_RESERVED":
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reserve path emitted unexpected event type"
                )
            if type(payload) is not dict or "decision_origin" in payload:
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reserve payload cannot predeclare decision origin"
                )
            bound_payload = dict(payload)
            bound_payload["decision_origin"] = self._origin.to_dict()
            seal[4](
                self._ledger,
                event_type=event_type,
                run_id=run_id,
                key=key,
                payload=bound_payload,
            )

    def reserve_run_without_shadowed_append(
        self: PaperExecutionLedger,
        *,
        run_id: str,
        trigger_id: str,
        plan,
        config,
        started_at: str,
        observation_evidence_ids,
    ) -> None:
        require_canonical_seal()
        origin = _origin._DECISION_ORIGIN.get()
        if origin is None:
            return seal[3](
                self,
                run_id=run_id,
                trigger_id=trigger_id,
                plan=plan,
                config=config,
                started_at=started_at,
                observation_evidence_ids=observation_evidence_ids,
            )

        if type(self) is not PaperExecutionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "origin-bound execution requires exact PaperExecutionLedger authority"
            )
        if _instance_shadows(self, "_append_event"):
            raise _origin.PaperExecutionDecisionOriginError(
                "execution ledger shadows authority method _append_event"
            )
        require_canonical_product_reservation_path(self)
        if origin.decision_id != trigger_id or origin.decision_id != plan.decision_id:
            raise PaperExecutionStateError(
                "decision origin does not match execution trigger/plan decision identity"
            )

        return seal[3](
            CanonicalReservationView(self, origin),
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )

    return (
        verified_decision_origin_without_instance_dispatch,
        require_canonical_product_reservation_path,
        CanonicalReservationView,
        reserve_run_without_shadowed_append,
    )


def _install() -> None:
    already_installed = bool(
        getattr(PaperExecutionLedger, "_autosport_decision_origin_instance_guard", False)
    )
    recovered_seal, stale_or_tampered = _sealed_tuple_from_callable(
        PaperExecutionLedger.reserve_run
    )
    reinstall = already_installed and stale_or_tampered
    seal = recovered_seal if recovered_seal is not None else _initial_seal()
    if not _seal_matches_canonical(seal):
        raise RuntimeError("decision-origin canonical executable seal is unavailable")

    # These are compatibility/debug mirrors only. Authority paths validate and use
    # the re-derived canonical seal, never these writable mirrors.
    setattr(JsonlDecisionLedger, _VERIFIED_SNAPSHOT_SENTINEL, seal[1])
    setattr(PaperExecutionLedger, _RESERVE_SENTINEL, seal[3])
    setattr(PaperExecutionLedger, _APPEND_SENTINEL, seal[4])
    runtime_execute_code = getattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_CODE_SENTINEL,
        _origin._ORIGINAL_RUNTIME_EXECUTE.__code__,
    )
    setattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_CODE_SENTINEL,
        runtime_execute_code,
    )
    setattr(PaperExecutionAdoptionRuntime, _EXECUTE_PLAN_CODE_SENTINEL, seal[5])

    global _STABLE_VERIFIED_SNAPSHOT
    global _STABLE_VERIFY_BYTES_DESCRIPTOR
    global _STABLE_RESERVE_RUN
    global _STABLE_APPEND_EVENT
    global _STABLE_RUNTIME_EXECUTE_CODE
    global _EXECUTE_PAPER_PLAN_CODE
    global _verified_decision_origin_without_instance_dispatch
    global _require_canonical_product_reservation_path
    global _CanonicalReservationView
    global _reserve_run_without_shadowed_append
    _STABLE_VERIFIED_SNAPSHOT = seal[1]
    _STABLE_VERIFY_BYTES_DESCRIPTOR = seal[2]
    _STABLE_RESERVE_RUN = seal[3]
    _STABLE_APPEND_EVENT = seal[4]
    _STABLE_RUNTIME_EXECUTE_CODE = runtime_execute_code
    _EXECUTE_PAPER_PLAN_CODE = seal[5]
    (
        _verified_decision_origin_without_instance_dispatch,
        _require_canonical_product_reservation_path,
        _CanonicalReservationView,
        _reserve_run_without_shadowed_append,
    ) = _build_guard(seal)

    _origin.verified_decision_origin = _verified_decision_origin_without_instance_dispatch
    if already_installed and not reinstall:
        return
    PaperExecutionLedger.reserve_run = _reserve_run_without_shadowed_append
    PaperExecutionLedger._autosport_decision_origin_instance_guard = True


_install()


__all__ = []
