from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .decision_ledger import (
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .domain import MarketEvent, TicketLeg
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalContractError, EconomicGoalStore
from .integrity import ensure_durable_file
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
    RecoveryDisposition,
)
from .paper import PaperBook
from .risk import PaperRiskPolicy, ProposedTicketRiskContext, StakeVectorDecision
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.proposal-risk-target-precommit.v2"
_ACTION = "PROPOSAL_RISK_TARGET_PRECOMMIT"
_AGENT = "autosport.proposal-risk-target-authority.v1"
_ALLOCATION_ALGORITHM = (
    "autosport.paper-risk-policy.derive-goal-stake-vector.relaxed-ruin-internal.v1"
)
_AUTHORITY_DOMAIN = "proposal-risk-target-precommit-v1"
_WORKSPACE_BINDING_KEY = "workspace-binding-v1"
_TARGET_CHAIN_KEY = "current-target-chain-v1"
_TARGET_AUTHORITY_PREFIX = "target-v1:"
_HEX = frozenset("0123456789abcdef")
_MAX_DECIMAL_TEXT = 256

_PATH_TYPE = type(Path("."))
_PATH_METHOD_WITNESSES = tuple(
    (
        name,
        getattr(_PATH_TYPE, name),
        getattr(getattr(_PATH_TYPE, name), "__code__", None),
    )
    for name in ("is_absolute", "resolve", "is_dir", "is_symlink")
)
_PATH_METHOD_WITNESSES_EXPECTED = _PATH_METHOD_WITNESSES

_POLICY_TYPE = PaperRiskPolicy
_CONTEXT_TYPE = ProposedTicketRiskContext
_VECTOR_TYPE = StakeVectorDecision
_BOOK_TYPE = PaperBook
_GOAL_TYPE = EconomicGoalContract
_GOAL_STORE_TYPE = EconomicGoalStore
_LEDGER_TYPE = JsonlDecisionLedger
_LOCK_TYPE = WorkspaceEconomicLock
_LOCK_METHOD_WITNESSES = tuple(
    (
        name,
        WorkspaceEconomicLock.__dict__[name],
        getattr(
            getattr(
                WorkspaceEconomicLock.__dict__[name],
                "__func__",
                WorkspaceEconomicLock.__dict__[name],
            ),
            "__code__",
            None,
        ),
    )
    for name in (
        "__init__",
        "acquire",
        "release",
        "__enter__",
        "__exit__",
        "_open_lock_handle",
        "_open_new_lock_handle",
        "_validate_existing_lock_path",
        "_validate_open_handle_identity",
        "_require_regular_file",
        "_require_single_link",
        "_lock_handle",
        "_unlock_handle",
    )
)
_LOCK_METHOD_WITNESSES_EXPECTED = _LOCK_METHOD_WITNESSES
_AUTHORITY_TYPE = MonotonicWorkspaceAuthority
_MARKET_EVENT_TYPE = MarketEvent
_TICKET_LEG_TYPE = TicketLeg
_DECISION_RECORD_TYPE = DecisionRecord
_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256
_MARKET_EVENT_METHOD_WITNESSES = tuple(
    (
        name,
        MarketEvent.__dict__[name],
        getattr(
            getattr(MarketEvent.__dict__[name], "__func__", MarketEvent.__dict__[name]),
            "__code__",
            None,
        ),
    )
    for name in ("to_dict", "from_dict")
)

_DERIVE_VECTOR_DESCRIPTOR = PaperRiskPolicy.__dict__["derive_goal_stake_vector"]
_DERIVE_VECTOR = _DERIVE_VECTOR_DESCRIPTOR
_DERIVE_VECTOR_CODE = getattr(_DERIVE_VECTOR, "__code__", None)

_PORTFOLIO_SHA_DESCRIPTOR = PaperRiskPolicy.__dict__["risk_of_ruin_portfolio_sha256"]
_PORTFOLIO_SHA = _PORTFOLIO_SHA_DESCRIPTOR.__func__
_PORTFOLIO_SHA_CODE = getattr(_PORTFOLIO_SHA, "__code__", None)

_CANDIDATE_SHA_DESCRIPTOR = PaperRiskPolicy.__dict__["risk_of_ruin_candidate_sha256"]
_CANDIDATE_SHA = _CANDIDATE_SHA_DESCRIPTOR.__func__
_CANDIDATE_SHA_CODE = getattr(_CANDIDATE_SHA, "__code__", None)

_VECTOR_SHA_DESCRIPTOR = PaperRiskPolicy.__dict__["risk_of_ruin_candidate_vector_sha256"]
_VECTOR_SHA = _VECTOR_SHA_DESCRIPTOR.__func__
_VECTOR_SHA_CODE = getattr(_VECTOR_SHA, "__code__", None)

_POLICY_PROVENANCE_PAYLOAD_DESCRIPTOR = PaperRiskPolicy.__dict__["provenance_payload"]
_POLICY_PROVENANCE_PAYLOAD = _POLICY_PROVENANCE_PAYLOAD_DESCRIPTOR
_POLICY_PROVENANCE_PAYLOAD_CODE = getattr(_POLICY_PROVENANCE_PAYLOAD, "__code__", None)
_POLICY_PROVENANCE_SHA_DESCRIPTOR = PaperRiskPolicy.__dict__["provenance_sha256"]
_POLICY_PROVENANCE_SHA_GETTER = _POLICY_PROVENANCE_SHA_DESCRIPTOR.fget
_POLICY_PROVENANCE_SHA_CODE = getattr(_POLICY_PROVENANCE_SHA_GETTER, "__code__", None)

_POLICY_PROVENANCE_PAYLOAD_GLOBALS = _POLICY_PROVENANCE_PAYLOAD.__globals__
_POLICY_PROVENANCE_FOR = _POLICY_PROVENANCE_PAYLOAD_GLOBALS.get("provenance_for")
_POLICY_PROVENANCE_FOR_CODE = getattr(_POLICY_PROVENANCE_FOR, "__code__", None)
_POLICY_PROVENANCE_SHA_GLOBALS = _POLICY_PROVENANCE_SHA_GETTER.__globals__
_POLICY_PROVENANCE_SHA_HELPER = _POLICY_PROVENANCE_SHA_GLOBALS.get("_sha256_payload")
_POLICY_PROVENANCE_SHA_HELPER_CODE = getattr(_POLICY_PROVENANCE_SHA_HELPER, "__code__", None)

_GOAL_LOAD = EconomicGoalStore.load
_GOAL_LOAD_CODE = getattr(_GOAL_LOAD, "__code__", None)
_BOOK_LOAD_DESCRIPTOR = PaperBook.__dict__["load"]
_BOOK_LOAD_FUNC = _BOOK_LOAD_DESCRIPTOR.__func__
_BOOK_LOAD_CODE = getattr(_BOOK_LOAD_FUNC, "__code__", None)
_BOOK_LOAD = PaperBook.load
_LEDGER_APPEND = JsonlDecisionLedger.append_economic
_LEDGER_APPEND_CODE = getattr(_LEDGER_APPEND, "__code__", None)
_LEDGER_RESOLVE = JsonlDecisionLedger.verified_economic_decision_for_material_action
_LEDGER_RESOLVE_CODE = getattr(_LEDGER_RESOLVE, "__code__", None)
_LEDGER_RECORDS = JsonlDecisionLedger.verified_records
_LEDGER_RECORDS_CODE = getattr(_LEDGER_RECORDS, "__code__", None)
_LEDGER_APPEND_GLOBALS = _LEDGER_APPEND.__globals__
_LEDGER_BIND_ECONOMIC_GOAL = _LEDGER_APPEND_GLOBALS.get("bind_economic_goal")
_LEDGER_BIND_ECONOMIC_GOAL_CODE = getattr(_LEDGER_BIND_ECONOMIC_GOAL, "__code__", None)
_LEDGER_RESOLVE_GLOBALS = _LEDGER_RESOLVE.__globals__
_LEDGER_VERIFY_ECONOMIC_GOAL_BINDING = _LEDGER_RESOLVE_GLOBALS.get(
    "verify_economic_goal_binding"
)
_LEDGER_VERIFY_ECONOMIC_GOAL_BINDING_CODE = getattr(
    _LEDGER_VERIFY_ECONOMIC_GOAL_BINDING, "__code__", None
)
_LEDGER_INTERNAL_METHOD_WITNESSES = tuple(
    (
        name,
        JsonlDecisionLedger.__dict__[name],
        getattr(
            getattr(JsonlDecisionLedger.__dict__[name], "__func__", JsonlDecisionLedger.__dict__[name]),
            "__code__",
            None,
        ),
    )
    for name in (
        "_require_utf8_text",
        "_validate_json_value",
        "_canonical_record",
        "_validate_record",
        "_json_object_without_duplicate_keys",
        "_reject_non_finite_json",
        "_append_validated",
        "_verify_bytes",
        "verified_snapshot",
        "verify_integrity",
    )
)
_DECISION_RECORD_METHOD_WITNESSES = tuple(
    (
        name,
        DecisionRecord.__dict__[name],
        getattr(DecisionRecord.__dict__[name], "__code__", None),
    )
    for name in ("__post_init__", "to_dict")
)
_PROVENANCE_FOR = provenance_for
_PROVENANCE_FOR_CODE = getattr(_PROVENANCE_FOR, "__code__", None)
_ENSURE_DURABLE_FILE = ensure_durable_file
_ENSURE_DURABLE_FILE_CODE = getattr(_ENSURE_DURABLE_FILE, "__code__", None)
_REPLACE = replace
_AUTHORITY_METHOD_WITNESSES = tuple(
    (
        name,
        MonotonicWorkspaceAuthority.__dict__[name],
        getattr(MonotonicWorkspaceAuthority.__dict__[name], "__code__", None),
    )
    for name in ("prepare", "commit", "recover", "read_history")
)
_AUTHORITY_METHOD_WITNESSES_EXPECTED = _AUTHORITY_METHOD_WITNESSES
_CONSTRUCTOR_WITNESSES = tuple(
    (
        label,
        owner,
        owner.__init__,
        getattr(owner.__init__, "__code__", None),
    )
    for label, owner in (
        ("EconomicGoalStore", EconomicGoalStore),
        ("PaperRiskPolicy", PaperRiskPolicy),
        ("JsonlDecisionLedger", JsonlDecisionLedger),
        ("MonotonicWorkspaceAuthority", MonotonicWorkspaceAuthority),
    )
)
_CONSTRUCTOR_WITNESSES_EXPECTED = _CONSTRUCTOR_WITNESSES


def _make_target_identity_capability():
    """Return closure-private bind/check functions for resolver-issued DTO truth."""

    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_proposal_target_identity_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_proposal_target_identity_capability", token)

    return proven, bind


_TARGET_IDENTITY_PROVEN, _BIND_TARGET_IDENTITY = _make_target_identity_capability()
del _make_target_identity_capability


class ProductProposalRiskTargetError(RuntimeError):
    """A proposal-specific pre-risk target cannot be issued or re-resolved safely."""


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskTarget:
    """Durable identity for the exact stake vector that risk must evaluate.

    This is intentionally a non-authorizing precommit. It proves only that the
    product durably fixed one exact base portfolio/candidate/signal/stake target
    before downstream counterfactual risk evaluation. It is not RiskOfRuinEvidence,
    a PortfolioPlan, a ticket, an execution instruction, or real-money authority.
    """

    workspace_instance_id: str
    decision_id: str
    decision_ts: str
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    economic_goal_contract_sha256: str
    risk_policy_sha256: str
    candidate_sha256s: tuple[str, ...]
    candidate_context_json: tuple[str, ...]
    candidate_vector_sha256: str
    signal_strengths: tuple[Decimal, ...]
    evaluated_stakes: tuple[Decimal, ...]
    target_sha256: str
    _proposal_target_identity_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(cls, *args: object, **kwargs: object) -> "ProductProposalRiskTarget":
        raise TypeError(
            "ProductProposalRiskTarget is product-issued; use "
            "issue_product_proposal_risk_target or resolve_product_proposal_risk_target"
        )

    @property
    def proposal_target_identity_proven(
        self,
        _proven=_TARGET_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def proposal_target_counterfactual_execution_proven(self) -> bool:
        return False

    @property
    def risk_upper_bound_for_target(self) -> bool:
        return False

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_TARGET_FIELDS = (
    "workspace_instance_id",
    "decision_id",
    "decision_ts",
    "bankroll_id",
    "currency",
    "base_portfolio_sha256",
    "economic_goal_contract_sha256",
    "risk_policy_sha256",
    "candidate_sha256s",
    "candidate_context_json",
    "candidate_vector_sha256",
    "signal_strengths",
    "evaluated_stakes",
    "target_sha256",
)


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductProposalRiskTargetError(f"{name} must be lowercase SHA-256 hex")
    return value


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskTargetError(f"{name} must be non-empty canonical text")
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise ProductProposalRiskTargetError(f"{name} contains unsupported characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskTargetError(f"{name} must be valid UTF-8 text") from exc
    return value


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalRiskTargetError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalRiskTargetError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskTargetError(f"{name} must be a finite exact Decimal")
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    exponent = int(parts.exponent)
    digits = len(parts.digits)
    sign = 1 if parts.sign else 0
    if exponent >= 0:
        length = sign + digits + exponent
    elif digits + exponent > 0:
        length = sign + digits + 1
    else:
        length = sign + 2 - exponent
    if length > _MAX_DECIMAL_TEXT:
        raise ProductProposalRiskTargetError(
            f"{name} exceeds supported canonical decimal size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _signal(value: object, index: int) -> Decimal:
    if type(value) is Decimal:
        parsed = value
    elif type(value) is str:
        if not value or value != value.strip():
            raise ProductProposalRiskTargetError(
                f"signal_strengths[{index}] must be canonical Decimal text"
            )
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ProductProposalRiskTargetError(
                f"signal_strengths[{index}] must be Decimal text"
            ) from exc
    else:
        raise ProductProposalRiskTargetError(
            f"signal_strengths[{index}] must be an exact Decimal or string"
        )
    if type(parsed) is not Decimal or not parsed.is_finite():
        raise ProductProposalRiskTargetError(
            f"signal_strengths[{index}] must be finite"
        )
    _decimal_text(parsed, f"signal_strengths[{index}]")
    return parsed


def _canonical_json(value: object) -> bytes:
    if (
        _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is not _JSON_DUMPS_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: canonical JSON serializer"
        )
    try:
        return _JSON_DUMPS(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    if (
        _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: target SHA-256 implementation"
        )
    return _HASHLIB_SHA256(_canonical_json(value)).hexdigest()


def _workspace_path(workspace: object) -> Path:
    if type(workspace) is not _PATH_TYPE or not workspace.is_absolute():
        raise ProductProposalRiskTargetError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskTargetError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != workspace or not workspace.is_dir() or workspace.is_symlink():
        raise ProductProposalRiskTargetError(
            "workspace must be the canonical non-symlink directory path"
        )
    return workspace


def _require_dispatch() -> None:
    checks = (
        (_POLICY_TYPE is PaperRiskPolicy, "PaperRiskPolicy type"),
        (_CONTEXT_TYPE is ProposedTicketRiskContext, "ProposedTicketRiskContext type"),
        (_VECTOR_TYPE is StakeVectorDecision, "StakeVectorDecision type"),
        (_BOOK_TYPE is PaperBook, "PaperBook type"),
        (_GOAL_TYPE is EconomicGoalContract, "EconomicGoalContract type"),
        (_GOAL_STORE_TYPE is EconomicGoalStore, "EconomicGoalStore type"),
        (_LEDGER_TYPE is JsonlDecisionLedger, "JsonlDecisionLedger type"),
        (_PATH_TYPE is type(Path(".")), "native pathlib concrete type"),
        (_PATH_METHOD_WITNESSES is _PATH_METHOD_WITNESSES_EXPECTED, "path method witness root"),
        (_LOCK_TYPE is WorkspaceEconomicLock, "WorkspaceEconomicLock type"),
        (_LOCK_METHOD_WITNESSES is _LOCK_METHOD_WITNESSES_EXPECTED, "WorkspaceEconomicLock method witness root"),
        (_AUTHORITY_TYPE is MonotonicWorkspaceAuthority, "MonotonicWorkspaceAuthority type"),
        (_MARKET_EVENT_TYPE is MarketEvent, "MarketEvent type"),
        (_TICKET_LEG_TYPE is TicketLeg, "TicketLeg type"),
        (_DECISION_RECORD_TYPE is DecisionRecord, "DecisionRecord type"),
        (
            _JSON_DUMPS is _JSON_DUMPS_EXPECTED
            and json.dumps is _JSON_DUMPS_EXPECTED
            and getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is _JSON_DUMPS_CODE,
            "canonical JSON serializer",
        ),
        (
            _HASHLIB_SHA256 is _HASHLIB_SHA256_EXPECTED
            and hashlib.sha256 is _HASHLIB_SHA256_EXPECTED,
            "target SHA-256 implementation",
        ),
        (_REPLACE is replace, "dataclasses.replace helper"),
        (_PROVENANCE_FOR is provenance_for, "economic-goal provenance helper"),
        (_ENSURE_DURABLE_FILE is ensure_durable_file, "durable-file helper"),
        (
            _CONSTRUCTOR_WITNESSES is _CONSTRUCTOR_WITNESSES_EXPECTED,
            "constructor witness root",
        ),
    )
    for valid, name in checks:
        if not valid:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {name}"
            )

    for name, expected, code in _PATH_METHOD_WITNESSES_EXPECTED:
        current = getattr(_PATH_TYPE, name, None)
        if current is not expected or getattr(current, "__code__", None) is not code:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: pathlib.{name}"
            )

    for name, expected, code in _LOCK_METHOD_WITNESSES_EXPECTED:
        current = WorkspaceEconomicLock.__dict__.get(name)
        current_function = getattr(current, "__func__", current)
        if (
            current is not expected
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: WorkspaceEconomicLock.{name}"
            )

    for label, owner, expected, code in _CONSTRUCTOR_WITNESSES_EXPECTED:
        current = owner.__init__
        if current is not expected or getattr(current, "__code__", None) is not code:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {label}.__init__"
            )

    descriptors = (
        (
            PaperRiskPolicy.__dict__.get("derive_goal_stake_vector"),
            _DERIVE_VECTOR_DESCRIPTOR,
            _DERIVE_VECTOR,
            _DERIVE_VECTOR_CODE,
            False,
            "stake-vector derivation",
        ),
        (
            PaperRiskPolicy.__dict__.get("risk_of_ruin_portfolio_sha256"),
            _PORTFOLIO_SHA_DESCRIPTOR,
            _PORTFOLIO_SHA,
            _PORTFOLIO_SHA_CODE,
            True,
            "portfolio identity",
        ),
        (
            PaperRiskPolicy.__dict__.get("risk_of_ruin_candidate_sha256"),
            _CANDIDATE_SHA_DESCRIPTOR,
            _CANDIDATE_SHA,
            _CANDIDATE_SHA_CODE,
            True,
            "candidate identity",
        ),
        (
            PaperRiskPolicy.__dict__.get("risk_of_ruin_candidate_vector_sha256"),
            _VECTOR_SHA_DESCRIPTOR,
            _VECTOR_SHA,
            _VECTOR_SHA_CODE,
            True,
            "candidate-vector identity",
        ),
    )
    for current, expected, function, code, wrapped, name in descriptors:
        if current is not expected:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {name}"
            )
        current_function = current.__func__ if wrapped else current
        if (
            current_function is not function
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {name}"
            )

    current_policy_provenance_payload = PaperRiskPolicy.__dict__.get(
        "provenance_payload"
    )
    if (
        current_policy_provenance_payload is not _POLICY_PROVENANCE_PAYLOAD_DESCRIPTOR
        or current_policy_provenance_payload is not _POLICY_PROVENANCE_PAYLOAD
        or getattr(current_policy_provenance_payload, "__code__", None)
        is not _POLICY_PROVENANCE_PAYLOAD_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: risk-policy provenance payload"
        )
    current_policy_provenance_sha = PaperRiskPolicy.__dict__.get(
        "provenance_sha256"
    )
    if (
        current_policy_provenance_sha is not _POLICY_PROVENANCE_SHA_DESCRIPTOR
        or getattr(current_policy_provenance_sha, "fget", None)
        is not _POLICY_PROVENANCE_SHA_GETTER
        or getattr(_POLICY_PROVENANCE_SHA_GETTER, "__code__", None)
        is not _POLICY_PROVENANCE_SHA_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: risk-policy provenance digest"
        )

    if (
        _POLICY_PROVENANCE_PAYLOAD.__globals__ is not _POLICY_PROVENANCE_PAYLOAD_GLOBALS
        or _POLICY_PROVENANCE_PAYLOAD_GLOBALS.get("provenance_for")
        is not _POLICY_PROVENANCE_FOR
        or _POLICY_PROVENANCE_FOR is not _PROVENANCE_FOR
        or getattr(_POLICY_PROVENANCE_FOR, "__code__", None)
        is not _POLICY_PROVENANCE_FOR_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: risk-policy provenance dependency"
        )
    if (
        _POLICY_PROVENANCE_SHA_GETTER.__globals__ is not _POLICY_PROVENANCE_SHA_GLOBALS
        or _POLICY_PROVENANCE_SHA_GLOBALS.get("_sha256_payload")
        is not _POLICY_PROVENANCE_SHA_HELPER
        or getattr(_POLICY_PROVENANCE_SHA_HELPER, "__code__", None)
        is not _POLICY_PROVENANCE_SHA_HELPER_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: risk-policy provenance hash helper"
        )

    current_book_load_descriptor = PaperBook.__dict__.get("load")
    if (
        current_book_load_descriptor is not _BOOK_LOAD_DESCRIPTOR
        or current_book_load_descriptor.__func__ is not _BOOK_LOAD_FUNC
        or getattr(current_book_load_descriptor.__func__, "__code__", None)
        is not _BOOK_LOAD_CODE
        or getattr(_BOOK_LOAD, "__func__", None) is not _BOOK_LOAD_FUNC
        or getattr(_BOOK_LOAD, "__self__", None) is not PaperBook
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: PaperBook loader"
        )

    method_checks = (
        (
            EconomicGoalStore.load,
            _GOAL_LOAD,
            _GOAL_LOAD_CODE,
            "economic-goal loader",
        ),
        (
            JsonlDecisionLedger.append_economic,
            _LEDGER_APPEND,
            _LEDGER_APPEND_CODE,
            "economic decision append",
        ),
        (
            JsonlDecisionLedger.verified_economic_decision_for_material_action,
            _LEDGER_RESOLVE,
            _LEDGER_RESOLVE_CODE,
            "economic decision resolver",
        ),
        (
            JsonlDecisionLedger.verified_records,
            _LEDGER_RECORDS,
            _LEDGER_RECORDS_CODE,
            "verified Decision Ledger record reader",
        ),
    )
    for current, expected, code, name in method_checks:
        if current is not expected:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {name}"
            )
        function = getattr(current, "__func__", current)
        if getattr(function, "__code__", None) is not code:
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: {name}"
            )
    if (
        _LEDGER_APPEND.__globals__ is not _LEDGER_APPEND_GLOBALS
        or _LEDGER_APPEND_GLOBALS.get("bind_economic_goal")
        is not _LEDGER_BIND_ECONOMIC_GOAL
        or getattr(_LEDGER_BIND_ECONOMIC_GOAL, "__code__", None)
        is not _LEDGER_BIND_ECONOMIC_GOAL_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: economic ledger binding"
        )
    if (
        _LEDGER_RESOLVE.__globals__ is not _LEDGER_RESOLVE_GLOBALS
        or _LEDGER_RESOLVE_GLOBALS.get("verify_economic_goal_binding")
        is not _LEDGER_VERIFY_ECONOMIC_GOAL_BINDING
        or getattr(_LEDGER_VERIFY_ECONOMIC_GOAL_BINDING, "__code__", None)
        is not _LEDGER_VERIFY_ECONOMIC_GOAL_BINDING_CODE
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: economic ledger resolver binding"
        )
    for name, expected, code in _LEDGER_INTERNAL_METHOD_WITNESSES:
        current = JsonlDecisionLedger.__dict__.get(name)
        current_function = getattr(current, "__func__", current)
        if (
            current is not expected
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: Decision Ledger {name}"
            )
    for name, expected, code in _DECISION_RECORD_METHOD_WITNESSES:
        current = DecisionRecord.__dict__.get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: DecisionRecord {name}"
            )
    if getattr(_PROVENANCE_FOR, "__code__", None) is not _PROVENANCE_FOR_CODE:
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: economic-goal provenance"
        )
    if getattr(_ENSURE_DURABLE_FILE, "__code__", None) is not _ENSURE_DURABLE_FILE_CODE:
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch authority changed: durable-file helper"
        )
    if (
        _AUTHORITY_METHOD_WITNESSES is not _AUTHORITY_METHOD_WITNESSES_EXPECTED
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target monotonic authority witness root changed"
        )
    for name, expected, code in _AUTHORITY_METHOD_WITNESSES_EXPECTED:
        current = MonotonicWorkspaceAuthority.__dict__.get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: monotonic authority {name}"
            )
    for name, expected, code in _MARKET_EVENT_METHOD_WITNESSES:
        current = MarketEvent.__dict__.get(name)
        current_function = getattr(current, "__func__", current)
        if (
            current is not expected
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target dispatch authority changed: MarketEvent {name}"
            )

    helper_witnesses = globals().get("_PROPOSAL_TARGET_HELPER_WITNESSES")
    expected_helper_witnesses = globals().get(
        "_PROPOSAL_TARGET_HELPER_WITNESSES_EXPECTED"
    )
    if helper_witnesses is not expected_helper_witnesses:
        raise ProductProposalRiskTargetError(
            "proposal-risk target internal helper witness root changed"
        )
    if type(helper_witnesses) is not tuple:
        raise ProductProposalRiskTargetError(
            "proposal-risk target internal helper witness set is unavailable"
        )
    for name, expected, code in expected_helper_witnesses:
        current = globals().get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
        ):
            raise ProductProposalRiskTargetError(
                f"proposal-risk target internal helper authority changed: {name}"
            )


_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch


def _context_payload(context: ProposedTicketRiskContext) -> dict[str, object]:
    if type(context) is not _CONTEXT_TYPE:
        raise ProductProposalRiskTargetError(
            "candidate contexts must be exact ProposedTicketRiskContext values"
        )
    if any(type(leg) is not _TICKET_LEG_TYPE for leg in context.legs):
        raise ProductProposalRiskTargetError(
            "candidate context contains non-canonical TicketLeg"
        )
    if any(type(quote) is not _MARKET_EVENT_TYPE for quote in context.quotes):
        raise ProductProposalRiskTargetError(
            "candidate context contains non-canonical MarketEvent"
        )
    if (
        context.risk_of_ruin_upper_bound is not None
        or context.risk_of_ruin_evidence is not None
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target inputs must exclude risk-of-ruin result evidence"
        )
    return {
        "legs": [
            {
                "event_id": leg.event_id,
                "market_id": leg.market_id,
                "selection_id": leg.selection_id,
                "locked_odds": _decimal_text(leg.locked_odds, "locked_odds"),
                "sport": leg.sport,
                "exchange_side": leg.exchange_side,
                "market_semantics_id": leg.market_semantics_id,
            }
            for leg in context.legs
        ],
        "quotes": [quote.to_dict() for quote in context.quotes],
        "provider_accounts": [
            [source_id, account_id]
            for source_id, account_id in context.provider_accounts
        ],
        "bankroll_id": context.bankroll_id,
        "currency": context.currency,
        "measurement_window_start": context.measurement_window_start,
        "measurement_window_end": context.measurement_window_end,
        "proposal_ts": context.proposal_ts,
    }


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(child) for child in value]
    return value


def _context_from_payload(raw: object) -> ProposedTicketRiskContext:
    if not isinstance(raw, Mapping):
        raise ProductProposalRiskTargetError("persisted candidate context is invalid")
    expected = {
        "legs",
        "quotes",
        "provider_accounts",
        "bankroll_id",
        "currency",
        "measurement_window_start",
        "measurement_window_end",
        "proposal_ts",
    }
    if set(raw) != expected:
        raise ProductProposalRiskTargetError(
            "persisted candidate context schema is invalid"
        )
    legs_raw = raw["legs"]
    quotes_raw = raw["quotes"]
    accounts_raw = raw["provider_accounts"]
    if not isinstance(legs_raw, (list, tuple)) or not legs_raw:
        raise ProductProposalRiskTargetError("persisted candidate legs are invalid")
    if not isinstance(quotes_raw, (list, tuple)) or not quotes_raw:
        raise ProductProposalRiskTargetError("persisted candidate quotes are invalid")
    if not isinstance(accounts_raw, (list, tuple)):
        raise ProductProposalRiskTargetError(
            "persisted provider accounts are invalid"
        )

    legs: list[TicketLeg] = []
    for item in legs_raw:
        if not isinstance(item, Mapping) or set(item) != {
            "event_id",
            "market_id",
            "selection_id",
            "locked_odds",
            "sport",
            "exchange_side",
            "market_semantics_id",
        }:
            raise ProductProposalRiskTargetError(
                "persisted TicketLeg schema is invalid"
            )
        try:
            odds = Decimal(_text(item["locked_odds"], "locked_odds"))
        except InvalidOperation as exc:
            raise ProductProposalRiskTargetError(
                "persisted locked_odds is invalid"
            ) from exc
        _decimal_text(odds, "locked_odds")
        legs.append(
            _TICKET_LEG_TYPE(
                event_id=_text(item["event_id"], "event_id"),
                market_id=_text(item["market_id"], "market_id"),
                selection_id=_text(item["selection_id"], "selection_id"),
                locked_odds=odds,
                sport=item["sport"],
                exchange_side=item["exchange_side"],
                market_semantics_id=item["market_semantics_id"],
            )
        )

    quotes: list[MarketEvent] = []
    for item in quotes_raw:
        if not isinstance(item, Mapping):
            raise ProductProposalRiskTargetError("persisted MarketEvent is invalid")
        try:
            plain = _plain_json(item)
            if type(plain) is not dict:
                raise ProductProposalRiskTargetError(
                    "persisted MarketEvent is invalid"
                )
            quotes.append(_MARKET_EVENT_TYPE.from_dict(plain))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTargetError(
                "persisted MarketEvent is invalid"
            ) from exc

    accounts: list[tuple[str, str]] = []
    for item in accounts_raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ProductProposalRiskTargetError(
                "persisted provider account is invalid"
            )
        accounts.append(
            (
                _text(item[0], "provider source_id"),
                _text(item[1], "provider account_id"),
            )
        )
    try:
        return _CONTEXT_TYPE(
            legs=tuple(legs),
            quotes=tuple(quotes),
            provider_accounts=tuple(accounts),
            bankroll_id=raw["bankroll_id"],
            currency=raw["currency"],
            measurement_window_start=raw["measurement_window_start"],
            measurement_window_end=raw["measurement_window_end"],
            proposal_ts=raw["proposal_ts"],
            risk_of_ruin_upper_bound=None,
            risk_of_ruin_evidence=None,
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "persisted candidate context cannot be reconstructed canonically"
        ) from exc


def _validate_context_vector(
    contexts: object,
    goal: EconomicGoalContract,
) -> tuple[ProposedTicketRiskContext, ...]:
    if type(contexts) is not tuple or not contexts:
        raise ProductProposalRiskTargetError(
            "contexts must be a non-empty exact tuple"
        )
    validated: list[ProposedTicketRiskContext] = []
    decision_ts: str | None = None
    identities: set[str] = set()
    for index, context in enumerate(contexts):
        if type(context) is not _CONTEXT_TYPE:
            raise ProductProposalRiskTargetError(
                f"contexts[{index}] must be an exact ProposedTicketRiskContext"
            )
        _context_payload(context)
        if (
            context.bankroll_id != goal.bankroll_id
            or context.currency != goal.currency
            or not context.quotes
            or context.proposal_ts is None
        ):
            raise ProductProposalRiskTargetError(
                "every proposal-target candidate must bind the current bankroll, "
                "currency, quote evidence and proposal time"
            )
        _instant(context.proposal_ts, f"contexts[{index}].proposal_ts")
        if decision_ts is None:
            decision_ts = context.proposal_ts
        elif context.proposal_ts != decision_ts:
            raise ProductProposalRiskTargetError(
                "one proposal-risk target requires one exact shared proposal timestamp"
            )
        candidate_sha = _CANDIDATE_SHA(context)
        if candidate_sha is None:
            raise ProductProposalRiskTargetError(
                "candidate identity cannot be derived from canonical context"
            )
        if candidate_sha in identities:
            raise ProductProposalRiskTargetError(
                "proposal-risk target contains duplicate candidate identity"
            )
        identities.add(candidate_sha)
        validated.append(context)
    return tuple(validated)


def _target_material(
    *,
    workspace_instance_id: str,
    decision_ts: str,
    bankroll_id: str,
    currency: str,
    base_portfolio_sha256: str,
    economic_goal_contract_sha256: str,
    risk_policy_sha256: str,
    candidate_sha256s: tuple[str, ...],
    candidate_vector_sha256: str,
    signal_strengths: tuple[Decimal, ...],
    evaluated_stakes: tuple[Decimal, ...],
    candidate_contexts: tuple[ProposedTicketRiskContext, ...],
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "workspace_instance_id": _text(
            workspace_instance_id, "workspace_instance_id", max_length=256
        ),
        "allocation_algorithm": _ALLOCATION_ALGORITHM,
        "decision_ts": _text(decision_ts, "decision_ts"),
        "bankroll_id": _text(bankroll_id, "bankroll_id"),
        "currency": _text(currency, "currency"),
        "base_portfolio_sha256": _sha(
            base_portfolio_sha256, "base_portfolio_sha256"
        ),
        "economic_goal_contract_sha256": _sha(
            economic_goal_contract_sha256, "economic_goal_contract_sha256"
        ),
        "risk_policy_sha256": _sha(risk_policy_sha256, "risk_policy_sha256"),
        "candidate_sha256s": [
            _sha(value, "candidate_sha256") for value in candidate_sha256s
        ],
        "candidate_vector_sha256": _sha(
            candidate_vector_sha256, "candidate_vector_sha256"
        ),
        "signal_strengths": [
            _decimal_text(value, "signal_strength") for value in signal_strengths
        ],
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake") for value in evaluated_stakes
        ],
        "candidate_contexts": [
            _context_payload(context) for context in candidate_contexts
        ],
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }


def _authority_for_workspace(workspace: Path) -> MonotonicWorkspaceAuthority:
    try:
        return _AUTHORITY_TYPE(
            workspace=workspace,
            domain=_AUTHORITY_DOMAIN,
            key=_WORKSPACE_BINDING_KEY,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target workspace identity authority is unavailable"
        ) from exc


def _target_authority(
    workspace: Path,
    workspace_instance_id: str,
) -> MonotonicWorkspaceAuthority:
    """Return the single monotonic current-target chain for this workspace."""

    try:
        return _AUTHORITY_TYPE(
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            domain=_AUTHORITY_DOMAIN,
            key=_TARGET_CHAIN_KEY,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target independent authority is unavailable"
        ) from exc


def _verified_chain_target_record(
    ledger: JsonlDecisionLedger,
    workspace_instance_id: str,
    target_sha256: str,
) -> DecisionRecord | None:
    """Resolve the exact historical target record without requiring a current goal.

    The machine-side target chain already commits the target digest. This reader is
    used only to prove that the exact append-only Decision Ledger record anchoring a
    prior chain tip still exists. It never restores current sizing or risk authority.
    """

    target_sha256 = _sha(target_sha256, "target_sha256")
    action_id = _TARGET_AUTHORITY_PREFIX + target_sha256
    try:
        records = _LEDGER_RECORDS(ledger)
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target chain cannot verify the Decision Ledger"
        ) from exc
    matched = tuple(record for record in records if record.decision_id == action_id)
    if not matched:
        return None
    if len(matched) != 1:
        raise ProductProposalRiskTargetError(
            "proposal-risk target chain contains duplicate decision identity"
        )
    record = matched[0]
    payload = record.payload
    if (
        record.action != _ACTION
        or record.agent != _AGENT
        or record.replay_run_id != action_id
        or record.context_hash != target_sha256
        or payload.get("schema") != _SCHEMA
        or payload.get("workspace_instance_id") != workspace_instance_id
        or payload.get("target_sha256") != target_sha256
        or payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY) != action_id
        or payload.get("proposal_target_counterfactual_execution_proven") is not False
        or payload.get("risk_upper_bound_for_target") is not False
        or payload.get("grants_ticket_authority") is not False
        or payload.get("grants_real_money_authority") is not False
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target chain record identity is invalid"
        )
    return record


def _recover_target_chain(
    authority: MonotonicWorkspaceAuthority,
    ledger: JsonlDecisionLedger,
    workspace_instance_id: str,
    goal: EconomicGoalContract,
    policy: PaperRiskPolicy,
    book: PaperBook,
) -> str | None:
    """Recover one interrupted target transition and return the committed chain tip."""

    try:
        history = authority.read_history()
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target chain history is invalid"
        ) from exc

    if history and history[-1].phase is AuthorityPhase.PREPARE:
        pending = history[-1]
        pending_target = _sha(
            pending.intended_state_sha256, "pending target_sha256"
        )
        pending_record = _verified_chain_target_record(
            ledger, workspace_instance_id, pending_target
        )
        try:
            if pending_record is None:
                recovery = authority.recover(
                    observed_state_sha256=pending.previous_committed_state_sha256,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
                if recovery.disposition is not RecoveryDisposition.ABORTED_PREPARE:
                    raise ProductProposalRiskTargetError(
                        "missing target append did not abort its pending chain transition"
                    )
            else:
                action_id = _TARGET_AUTHORITY_PREFIX + pending_target
                current_record = _LEDGER_RESOLVE(
                    ledger,
                    action_id,
                    goal,
                    risk_policy=policy,
                )
                if current_record is None:
                    raise ProductProposalRiskTargetError(
                        "pending target append is not current economic authority"
                    )
                _build_target(
                    workspace_instance_id=workspace_instance_id,
                    record=current_record,
                    goal=goal,
                    policy=policy,
                    book=book,
                    expected_target_sha256=pending_target,
                )
                recovery = authority.recover(
                    observed_state_sha256=pending_target,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
                if recovery.disposition not in {
                    RecoveryDisposition.COMMITTED_PREPARE,
                    RecoveryDisposition.CURRENT,
                }:
                    raise ProductProposalRiskTargetError(
                        "durable target append did not commit its pending chain transition"
                    )
        except (DecisionLedgerIntegrityError, MonotonicWorkspaceAuthorityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target chain crash recovery failed"
            ) from exc
        try:
            history = authority.read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target chain history cannot be refreshed"
            ) from exc

    latest_commit = next(
        (
            record
            for record in reversed(history)
            if record.phase is AuthorityPhase.COMMIT
        ),
        None,
    )
    if latest_commit is None:
        return None
    latest_target = _sha(
        latest_commit.intended_state_sha256, "latest target_sha256"
    )
    if (
        _verified_chain_target_record(
            ledger, workspace_instance_id, latest_target
        )
        is None
    ):
        raise ProductProposalRiskTargetError(
            "current proposal-risk target chain tip is missing from the Decision Ledger"
        )
    return latest_target


def _require_target_authority_committed(
    authority: MonotonicWorkspaceAuthority,
    target_sha256: str,
) -> None:
    try:
        recovery = authority.recover(observed_state_sha256=target_sha256)
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskTargetError(
            "proposal-risk target is missing, rolled back, or superseded"
        ) from exc
    if (
        recovery.disposition is not RecoveryDisposition.CURRENT
        or recovery.committed_state_sha256 != target_sha256
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target is not the current committed target"
        )


def _derive(
    policy: PaperRiskPolicy,
    book: PaperBook,
    signal_strengths: tuple[Decimal, ...],
    contexts: tuple[ProposedTicketRiskContext, ...],
) -> StakeVectorDecision:
    goal = policy.economic_goal
    if type(goal) is not _GOAL_TYPE:
        raise ProductProposalRiskTargetError(
            "canonical risk policy lacks exact EconomicGoal authority"
        )
    relaxed_goal = _REPLACE(goal, max_risk_of_ruin=Decimal("1"))
    relaxed_policy = _REPLACE(policy, economic_goal=relaxed_goal)
    if type(relaxed_policy) is not _POLICY_TYPE:
        raise ProductProposalRiskTargetError(
            "internal relaxed risk policy is non-canonical"
        )
    try:
        decision = _DERIVE_VECTOR(
            relaxed_policy,
            book,
            signal_strengths,
            contexts=contexts,
            risk_of_ruin_vector_evidence=None,
        )
    except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "canonical pre-risk stake-vector derivation failed"
        ) from exc
    if type(decision) is not _VECTOR_TYPE:
        raise ProductProposalRiskTargetError(
            "canonical pre-risk stake-vector derivation returned a non-canonical value"
        )
    if decision.action != "STAKE_VECTOR" or not any(
        type(stake) is Decimal and stake.is_finite() and stake > 0
        for stake in decision.stakes
    ):
        raise ProductProposalRiskTargetError(
            "proposal has no positive canonical pre-risk stake vector to precommit"
        )
    if len(decision.stakes) != len(contexts):
        raise ProductProposalRiskTargetError(
            "canonical pre-risk stake-vector cardinality changed"
        )
    for stake in decision.stakes:
        _decimal_text(stake, "evaluated_stake")
        if stake < 0:
            raise ProductProposalRiskTargetError(
                "canonical pre-risk stake vector contains negative stake"
            )
    return decision


def _current_product_state(
    workspace: Path,
) -> tuple[EconomicGoalContract, PaperRiskPolicy, PaperBook, JsonlDecisionLedger]:
    try:
        goal = _GOAL_LOAD(_GOAL_STORE_TYPE(workspace))
    except (EconomicGoalContractError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "current product EconomicGoal cannot be resolved"
        ) from exc
    if type(goal) is not _GOAL_TYPE:
        raise ProductProposalRiskTargetError(
            "current product EconomicGoal has non-canonical type"
        )
    policy = _POLICY_TYPE(economic_goal=goal)
    if type(policy) is not _POLICY_TYPE:
        raise ProductProposalRiskTargetError(
            "current product PaperRiskPolicy has non-canonical type"
        )
    book_path = workspace / "paper_book.json"
    if not book_path.exists():
        raise ProductProposalRiskTargetError(
            "canonical paper_book.json is required before proposal-target issuance"
        )
    try:
        book = _BOOK_LOAD(book_path)
    except (ArithmeticError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "canonical PaperBook cannot be loaded"
        ) from exc
    if type(book) is not _BOOK_TYPE:
        raise ProductProposalRiskTargetError(
            "canonical PaperBook loader returned a non-canonical type"
        )
    ledger = _LEDGER_TYPE(workspace / "decisions.jsonl")
    try:
        _ENSURE_DURABLE_FILE(ledger.path)
        ledger.verify_integrity()
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTargetError(
            "canonical Decision Ledger cannot be verified"
        ) from exc
    return goal, policy, book, ledger


def _build_target(
    *,
    workspace_instance_id: str,
    record: DecisionRecord,
    goal: EconomicGoalContract,
    policy: PaperRiskPolicy,
    book: PaperBook,
    expected_target_sha256: str,
    _bind_identity=_BIND_TARGET_IDENTITY,
) -> ProductProposalRiskTarget:
    payload = record.payload
    allowed_payload_fields = {
        "schema",
        "workspace_instance_id",
        "allocation_algorithm",
        "decision_ts",
        "bankroll_id",
        "currency",
        "base_portfolio_sha256",
        "economic_goal_contract_sha256",
        "risk_policy_sha256",
        "candidate_sha256s",
        "candidate_vector_sha256",
        "signal_strengths",
        "evaluated_stakes",
        "candidate_contexts",
        "proposal_target_counterfactual_execution_proven",
        "risk_upper_bound_for_target",
        "grants_ticket_authority",
        "grants_real_money_authority",
        "target_sha256",
        MATERIAL_ACTION_ID_PAYLOAD_KEY,
        ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
        RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    }
    if set(payload) != allowed_payload_fields:
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target payload schema is invalid"
        )
    if (
        payload.get("schema") != _SCHEMA
        or payload.get("allocation_algorithm") != _ALLOCATION_ALGORITHM
        or payload.get("workspace_instance_id") != workspace_instance_id
    ):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target identity is invalid"
        )
    target_sha256 = _sha(payload.get("target_sha256"), "target_sha256")
    if target_sha256 != _sha(expected_target_sha256, "expected_target_sha256"):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target does not match requested identity"
        )
    material_action_id = _text(
        payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY), "material_action_id"
    )
    expected_action_id = _TARGET_AUTHORITY_PREFIX + target_sha256
    if material_action_id != expected_action_id:
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target material action identity is invalid"
        )
    if (
        record.action != _ACTION
        or record.agent != _AGENT
        or record.replay_run_id != expected_action_id
        or record.context_hash != target_sha256
        or record.decision_id != expected_action_id
    ):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target decision envelope is invalid"
        )

    if any(
        payload.get(name) is not False
        for name in (
            "proposal_target_counterfactual_execution_proven",
            "risk_upper_bound_for_target",
            "grants_ticket_authority",
            "grants_real_money_authority",
        )
    ):
        raise ProductProposalRiskTargetError(
            "proposal-risk target cannot persist downstream authority claims"
        )

    decision_ts = _text(payload.get("decision_ts"), "decision_ts")
    _instant(decision_ts, "decision_ts")
    if record.observed_ts != decision_ts:
        raise ProductProposalRiskTargetError(
            "proposal-risk target decision timestamp does not match its ledger envelope"
        )
    bankroll_id = _text(payload.get("bankroll_id"), "bankroll_id")
    currency = _text(payload.get("currency"), "currency")
    if bankroll_id != goal.bankroll_id or currency != goal.currency:
        raise ProductProposalRiskTargetError(
            "proposal-risk target is stale for the current EconomicGoal bankroll"
        )

    goal_sha = _PROVENANCE_FOR(goal).contract_sha256
    if payload.get("economic_goal_contract_sha256") != goal_sha:
        raise ProductProposalRiskTargetError(
            "proposal-risk target EconomicGoal provenance is stale"
        )
    if payload.get("risk_policy_sha256") != policy.provenance_sha256:
        raise ProductProposalRiskTargetError(
            "proposal-risk target PaperRiskPolicy provenance is stale"
        )

    base_portfolio_sha256 = _PORTFOLIO_SHA(_POLICY_TYPE, book)
    if base_portfolio_sha256 is None:
        raise ProductProposalRiskTargetError(
            "current canonical PaperBook identity cannot be resolved"
        )
    if payload.get("base_portfolio_sha256") != base_portfolio_sha256:
        raise ProductProposalRiskTargetError(
            "proposal-risk target base portfolio is stale"
        )

    contexts_raw = payload.get("candidate_contexts")
    if not isinstance(contexts_raw, (list, tuple)) or not contexts_raw:
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target contexts are invalid"
        )
    contexts = tuple(_context_from_payload(value) for value in contexts_raw)
    contexts = _validate_context_vector(contexts, goal)
    if any(context.proposal_ts != decision_ts for context in contexts):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target proposal time is inconsistent"
        )

    candidate_sha256s = tuple(
        _sha(_CANDIDATE_SHA(context), "candidate_sha256") for context in contexts
    )
    persisted_candidate_sha256s = payload.get("candidate_sha256s")
    if not isinstance(persisted_candidate_sha256s, (list, tuple)):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target candidate identities are invalid"
        )
    persisted_candidate_sha256s_tuple = tuple(
        _sha(value, "candidate_sha256") for value in persisted_candidate_sha256s
    )
    if persisted_candidate_sha256s_tuple != candidate_sha256s:
        raise ProductProposalRiskTargetError(
            "proposal-risk target candidate identities do not re-resolve"
        )
    candidate_vector_sha256 = _VECTOR_SHA(_POLICY_TYPE, contexts)
    if candidate_vector_sha256 is None:
        raise ProductProposalRiskTargetError(
            "proposal-risk target candidate vector cannot be re-resolved"
        )
    if payload.get("candidate_vector_sha256") != candidate_vector_sha256:
        raise ProductProposalRiskTargetError(
            "proposal-risk target candidate vector identity changed"
        )

    signal_raw = payload.get("signal_strengths")
    if not isinstance(signal_raw, (list, tuple)) or len(signal_raw) != len(contexts):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target signal vector is invalid"
        )
    signals = tuple(_signal(value, index) for index, value in enumerate(signal_raw))
    decision = _derive(policy, book, signals, contexts)
    stakes = tuple(decision.stakes)

    stake_raw = payload.get("evaluated_stakes")
    if not isinstance(stake_raw, (list, tuple)) or len(stake_raw) != len(stakes):
        raise ProductProposalRiskTargetError(
            "persisted proposal-risk target stake vector is invalid"
        )
    persisted_stakes = tuple(
        _signal(value, index) for index, value in enumerate(stake_raw)
    )
    if persisted_stakes != stakes:
        raise ProductProposalRiskTargetError(
            "proposal-risk target stake vector does not re-derive from current product state"
        )

    material = _target_material(
        workspace_instance_id=workspace_instance_id,
        decision_ts=decision_ts,
        bankroll_id=bankroll_id,
        currency=currency,
        base_portfolio_sha256=base_portfolio_sha256,
        economic_goal_contract_sha256=goal_sha,
        risk_policy_sha256=policy.provenance_sha256,
        candidate_sha256s=candidate_sha256s,
        candidate_vector_sha256=candidate_vector_sha256,
        signal_strengths=signals,
        evaluated_stakes=stakes,
        candidate_contexts=contexts,
    )
    if _digest(material) != target_sha256:
        raise ProductProposalRiskTargetError(
            "proposal-risk target digest does not re-derive"
        )

    instance = object.__new__(ProductProposalRiskTarget)
    values = {
        "workspace_instance_id": workspace_instance_id,
        "decision_id": record.decision_id,
        "decision_ts": decision_ts,
        "bankroll_id": bankroll_id,
        "currency": currency,
        "base_portfolio_sha256": base_portfolio_sha256,
        "economic_goal_contract_sha256": goal_sha,
        "risk_policy_sha256": policy.provenance_sha256,
        "candidate_sha256s": candidate_sha256s,
        "candidate_context_json": tuple(
            _canonical_json(_context_payload(context)).decode("utf-8")
            for context in contexts
        ),
        "candidate_vector_sha256": candidate_vector_sha256,
        "signal_strengths": signals,
        "evaluated_stakes": stakes,
        "target_sha256": target_sha256,
    }
    for name in _TARGET_FIELDS:
        object.__setattr__(instance, name, values[name])
    _bind_identity(instance)
    return instance


def issue_product_proposal_risk_target(
    workspace: Path,
    *,
    signal_strengths: tuple[Decimal | str, ...],
    contexts: tuple[ProposedTicketRiskContext, ...],
) -> ProductProposalRiskTarget:
    """Durably precommit the one current pre-risk target from canonical sizing.

    No caller-selected stake vector or risk-of-ruin bound is accepted. The product
    reloads its own EconomicGoal/PaperBook, derives the vector through the existing
    allocator with only the ruin gate internally relaxed, appends the causal target
    to the existing Decision Ledger, and advances one independent monotonic target
    chain. A newer target supersedes all older targets instead of leaving multiple
    simultaneously valid alternatives.
    """

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    if type(signal_strengths) is not tuple or not signal_strengths:
        raise ProductProposalRiskTargetError(
            "signal_strengths must be a non-empty exact tuple"
        )
    signals = tuple(
        _signal(value, index) for index, value in enumerate(signal_strengths)
    )

    with _LOCK_TYPE(workspace):
        _REQUIRE_DISPATCH_ORIGINAL()
        goal, policy, book, ledger = _current_product_state(workspace)
        validated_contexts = _validate_context_vector(contexts, goal)
        if len(signals) != len(validated_contexts):
            raise ProductProposalRiskTargetError(
                "signal/context vector cardinality does not match"
            )
        decision = _derive(policy, book, signals, validated_contexts)

        workspace_authority = _authority_for_workspace(workspace)
        workspace_instance_id = workspace_authority.workspace_instance_id
        base_portfolio_sha256 = _PORTFOLIO_SHA(_POLICY_TYPE, book)
        candidate_vector_sha256 = _VECTOR_SHA(_POLICY_TYPE, validated_contexts)
        if base_portfolio_sha256 is None or candidate_vector_sha256 is None:
            raise ProductProposalRiskTargetError(
                "canonical proposal-risk target identities cannot be derived"
            )
        candidate_sha256s = tuple(
            _sha(_CANDIDATE_SHA(context), "candidate_sha256")
            for context in validated_contexts
        )
        goal_sha = _PROVENANCE_FOR(goal).contract_sha256
        decision_ts = validated_contexts[0].proposal_ts
        assert decision_ts is not None

        material = _target_material(
            workspace_instance_id=workspace_instance_id,
            decision_ts=decision_ts,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            base_portfolio_sha256=base_portfolio_sha256,
            economic_goal_contract_sha256=goal_sha,
            risk_policy_sha256=policy.provenance_sha256,
            candidate_sha256s=candidate_sha256s,
            candidate_vector_sha256=candidate_vector_sha256,
            signal_strengths=signals,
            evaluated_stakes=tuple(decision.stakes),
            candidate_contexts=validated_contexts,
        )
        target_sha256 = _digest(material)
        action_id = _TARGET_AUTHORITY_PREFIX + target_sha256
        authority = _target_authority(workspace, workspace_instance_id)

        current_target = _recover_target_chain(
            authority,
            ledger,
            workspace_instance_id,
            goal,
            policy,
            book,
        )

        try:
            existing = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target Decision Ledger re-resolution failed"
            ) from exc

        if existing is not None:
            if current_target != target_sha256:
                raise ProductProposalRiskTargetError(
                    "proposal-risk target was already superseded and cannot be reissued"
                )
            target = _build_target(
                workspace_instance_id=workspace_instance_id,
                record=existing,
                goal=goal,
                policy=policy,
                book=book,
                expected_target_sha256=target_sha256,
            )
            _require_target_authority_committed(authority, target_sha256)
            return target

        try:
            history = authority.read_history()
            attempt_tx_id = f"{target_sha256}:{len(history) + 1}"
            authority.prepare(
                tx_id=attempt_tx_id,
                observed_state_sha256=current_target,
                intended_state_sha256=target_sha256,
                semantic_binding_sha256=target_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target chain PREPARE failed"
            ) from exc

        payload = {
            **material,
            "target_sha256": target_sha256,
            MATERIAL_ACTION_ID_PAYLOAD_KEY: action_id,
        }
        try:
            record = DecisionRecord(
                replay_run_id=action_id,
                agent=_AGENT,
                observed_ts=decision_ts,
                action=_ACTION,
                payload=payload,
                context_hash=target_sha256,
                decision_id=action_id,
            )
            _LEDGER_APPEND(
                ledger,
                record,
                goal,
                risk_policy=policy,
            )
            existing = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
            # If the append is absent, the PREPARE is safely abortable against the
            # previous target tip. If the exact target record is already durable,
            # preserve the PREPARE: only exact crash recovery may decide COMMIT.
            try:
                crossed = _verified_chain_target_record(
                    ledger, workspace_instance_id, target_sha256
                )
            except ProductProposalRiskTargetError:
                crossed = None
            if crossed is None:
                try:
                    authority.recover(
                        observed_state_sha256=current_target,
                        tx_id=attempt_tx_id,
                        semantic_binding_sha256=target_sha256,
                    )
                except MonotonicWorkspaceAuthorityError:
                    pass
                raise ProductProposalRiskTargetError(
                    "proposal-risk target Decision Ledger append failed"
                ) from exc
            raise ProductProposalRiskTargetError(
                "proposal-risk target append crossed durability boundary; "
                "exact chain recovery is required"
            ) from exc

        if existing is None:
            raise ProductProposalRiskTargetError(
                "proposal-risk target append did not re-resolve"
            )

        # Re-read every product-owned economic input after the durable ledger append
        # and before independent chain COMMIT. Cooperating writers are excluded by
        # WorkspaceEconomicLock; this second read also catches an uncooperative
        # filesystem mutation before positive target authority is published.
        fresh_goal, fresh_policy, fresh_book, fresh_ledger = _current_product_state(
            workspace
        )
        try:
            fresh_existing = _LEDGER_RESOLVE(
                fresh_ledger,
                action_id,
                fresh_goal,
                risk_policy=fresh_policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target product state changed before chain COMMIT"
            ) from exc
        if fresh_existing is None:
            raise ProductProposalRiskTargetError(
                "proposal-risk target disappeared before chain COMMIT"
            )
        target = _build_target(
            workspace_instance_id=workspace_instance_id,
            record=fresh_existing,
            goal=fresh_goal,
            policy=fresh_policy,
            book=fresh_book,
            expected_target_sha256=target_sha256,
        )
        try:
            authority.commit(
                tx_id=attempt_tx_id,
                observed_state_sha256=target_sha256,
                semantic_binding_sha256=target_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target chain COMMIT failed; recovery is required"
            ) from exc

        _require_target_authority_committed(authority, target_sha256)
        return target

def resolve_product_proposal_risk_target(
    workspace: Path,
    target_sha256: str,
) -> ProductProposalRiskTarget:
    """Re-resolve one target only while its base proposal state remains current.

    Any changed EconomicGoal, PaperRiskPolicy provenance, PaperBook state,
    candidate inputs, ledger bytes, or independent workspace authority fails closed.
    A downstream counterfactual evaluator must consume the target before unrelated
    economic mutation makes the proposal stale.
    """

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskTargetError(
            "proposal-risk target dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    target_sha256 = _sha(target_sha256, "target_sha256")
    with _LOCK_TYPE(workspace):
        _REQUIRE_DISPATCH_ORIGINAL()
        goal, policy, book, ledger = _current_product_state(workspace)
        workspace_authority = _authority_for_workspace(workspace)
        workspace_instance_id = workspace_authority.workspace_instance_id
        authority = _target_authority(workspace, workspace_instance_id)
        _require_target_authority_committed(authority, target_sha256)
        action_id = _TARGET_AUTHORITY_PREFIX + target_sha256
        try:
            record = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTargetError(
                "proposal-risk target Decision Ledger re-resolution failed"
            ) from exc
        if record is None:
            raise ProductProposalRiskTargetError(
                "proposal-risk target is missing from the canonical Decision Ledger"
            )
        return _build_target(
            workspace_instance_id=workspace_instance_id,
            record=record,
            goal=goal,
            policy=policy,
            book=book,
            expected_target_sha256=target_sha256,
        )


_PROPOSAL_TARGET_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
    )
    for name in (
        "_workspace_path",
        "_context_payload",
        "_plain_json",
        "_context_from_payload",
        "_validate_context_vector",
        "_target_material",
        "_authority_for_workspace",
        "_target_authority",
        "_verified_chain_target_record",
        "_recover_target_chain",
        "_require_target_authority_committed",
        "_derive",
        "_current_product_state",
        "_build_target",
        "_signal",
        "_canonical_json",
        "_digest",
        "_sha",
        "_text",
        "_instant",
        "_decimal_text",
    )
)

_PROPOSAL_TARGET_HELPER_WITNESSES_EXPECTED = _PROPOSAL_TARGET_HELPER_WITNESSES

# Keep issuance capability out of the mutable module namespace after import.
# The property and canonical builder retain only closure/default references.
del _TARGET_IDENTITY_PROVEN
del _BIND_TARGET_IDENTITY
