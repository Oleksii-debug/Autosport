from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .decision_ledger import (
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .economic_goal import EconomicGoalContract
from .economic_goal_store import EconomicGoalContractError, EconomicGoalStore
from .integrity import ensure_durable_file
from .market_outcomes import MarketSettlementOutcomeAuthority
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
)
from .risk import PaperRiskPolicy
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.proposal-target-terminal-population-precommit.v1"
_ACTION = "PROPOSAL_TARGET_TERMINAL_POPULATION_PRECOMMIT"
_AGENT = "autosport.proposal-target-terminal-population-authority.v1"
_ACTION_PREFIX = "proposal-target-terminal-population-v1:"
_HEX = frozenset("0123456789abcdef")
_MAX_AUTHORITIES = 256
_PATH_TYPE = type(Path("."))
_TARGET_TYPE = ProductProposalRiskTarget
_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_AUTHORITY_TO_DICT = MarketSettlementOutcomeAuthority.to_dict
_AUTHORITY_TO_DICT_CODE = getattr(_AUTHORITY_TO_DICT, "__code__", None)
_AUTHORITY_AS_OF = MarketSettlementOutcomeAuthority.assert_available_as_of
_AUTHORITY_AS_OF_CODE = getattr(_AUTHORITY_AS_OF, "__code__", None)
_AUTHORITY_SHA_GETTER = MarketSettlementOutcomeAuthority.authority_sha256.fget
_AUTHORITY_SHA_GETTER_CODE = getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
_GOAL_STORE_TYPE = EconomicGoalStore
_GOAL_LOAD = EconomicGoalStore.load
_GOAL_LOAD_CODE = getattr(_GOAL_LOAD, "__code__", None)
_POLICY_TYPE = PaperRiskPolicy
_LEDGER_TYPE = JsonlDecisionLedger
_LEDGER_APPEND = JsonlDecisionLedger.append_economic
_LEDGER_APPEND_CODE = getattr(_LEDGER_APPEND, "__code__", None)
_LEDGER_RESOLVE = JsonlDecisionLedger.verified_economic_decision_for_material_action
_LEDGER_RESOLVE_CODE = getattr(_LEDGER_RESOLVE, "__code__", None)
_LEDGER_VERIFY = JsonlDecisionLedger.verify_integrity
_LEDGER_VERIFY_CODE = getattr(_LEDGER_VERIFY, "__code__", None)
_RECORD_TYPE = DecisionRecord
_RECORD_POST_INIT = DecisionRecord.__post_init__
_RECORD_POST_INIT_CODE = getattr(_RECORD_POST_INIT, "__code__", None)
_RECORD_TO_DICT = DecisionRecord.to_dict
_RECORD_TO_DICT_CODE = getattr(_RECORD_TO_DICT, "__code__", None)
_LOCK_TYPE = WorkspaceEconomicLock
_ENSURE_DURABLE_FILE = ensure_durable_file
_ENSURE_DURABLE_FILE_CODE = getattr(_ENSURE_DURABLE_FILE, "__code__", None)
_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_JSON_LOADS = json.loads
_JSON_LOADS_EXPECTED = _JSON_LOADS
_JSON_LOADS_CODE = getattr(_JSON_LOADS, "__code__", None)
_JSON_DECODE_ERROR_TYPE = json.JSONDecodeError
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256


class ProductProposalTargetTerminalPopulationError(RuntimeError):
    """Target terminal-space precommit cannot be issued or re-resolved safely."""


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_terminal_population_identity_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(
            instance,
            "_terminal_population_identity_capability",
            token,
        )

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalTargetTerminalPopulation:
    """Provider-verified exhaustive terminal-space precommit for one proposal target.

    This artifact deliberately carries no probability model and proves no IID-member
    mapping, counterfactual execution, risk upper bound, ticket authority, provider
    write authority, or real-money authority.
    """

    workspace_instance_id: str
    decision_id: str
    target_sha256: str
    target_decision_ts: str
    candidate_vector_sha256: str
    candidate_sha256s: tuple[str, ...]
    market_authority_sha256s: tuple[str, ...]
    market_authority_json: tuple[str, ...]
    market_group_sha256s: tuple[str, ...]
    market_group_json: tuple[str, ...]
    candidate_market_authority_sha256s: tuple[tuple[str, ...], ...]
    terminal_market_count: int
    terminal_state_count: int
    terminal_space_exact: bool
    population_sha256: str
    _terminal_population_identity_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalTargetTerminalPopulation":
        raise TypeError(
            "ProductProposalTargetTerminalPopulation is product-issued; use "
            "issue_product_proposal_target_terminal_population or "
            "resolve_product_proposal_target_terminal_population"
        )

    @property
    def population_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_terminal_authority_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def terminal_space_exhaustive(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def probability_model_bound(self) -> bool:
        return False

    @property
    def scientific_precommit_bound(self) -> bool:
        return False

    @property
    def iid_member_mapping_proven(self) -> bool:
        return False

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


_POPULATION_TYPE = ProductProposalTargetTerminalPopulation
_POPULATION_TYPE_EXPECTED = _POPULATION_TYPE
_POPULATION_AUTHORITY_PROPERTY_NAMES = (
    "population_identity_proven",
    "provider_terminal_authority_proven",
    "terminal_space_exhaustive",
    "probability_model_bound",
    "scientific_precommit_bound",
    "iid_member_mapping_proven",
    "proposal_target_counterfactual_execution_proven",
    "risk_upper_bound_for_target",
    "grants_ticket_authority",
    "grants_real_money_authority",
)
_POPULATION_AUTHORITY_PROPERTY_WITNESSES = tuple(
    (
        name,
        _POPULATION_TYPE.__dict__[name],
        _POPULATION_TYPE.__dict__[name].fget,
        getattr(_POPULATION_TYPE.__dict__[name].fget, "__code__", None),
    )
    for name in _POPULATION_AUTHORITY_PROPERTY_NAMES
)
_POPULATION_AUTHORITY_PROPERTY_WITNESSES_EXPECTED = (
    _POPULATION_AUTHORITY_PROPERTY_WITNESSES
)


def _require_dispatch() -> None:
    if (
        ProductProposalRiskTarget is not _TARGET_TYPE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None) is not _TARGET_RESOLVER_CODE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or MarketSettlementOutcomeAuthority.to_dict is not _AUTHORITY_TO_DICT
        or getattr(_AUTHORITY_TO_DICT, "__code__", None) is not _AUTHORITY_TO_DICT_CODE
        or MarketSettlementOutcomeAuthority.assert_available_as_of is not _AUTHORITY_AS_OF
        or getattr(_AUTHORITY_AS_OF, "__code__", None) is not _AUTHORITY_AS_OF_CODE
        or MarketSettlementOutcomeAuthority.authority_sha256.fget is not _AUTHORITY_SHA_GETTER
        or getattr(_AUTHORITY_SHA_GETTER, "__code__", None) is not _AUTHORITY_SHA_GETTER_CODE
        or EconomicGoalStore is not _GOAL_STORE_TYPE
        or EconomicGoalStore.load is not _GOAL_LOAD
        or getattr(_GOAL_LOAD, "__code__", None) is not _GOAL_LOAD_CODE
        or PaperRiskPolicy is not _POLICY_TYPE
        or JsonlDecisionLedger is not _LEDGER_TYPE
        or JsonlDecisionLedger.append_economic is not _LEDGER_APPEND
        or getattr(_LEDGER_APPEND, "__code__", None) is not _LEDGER_APPEND_CODE
        or JsonlDecisionLedger.verified_economic_decision_for_material_action is not _LEDGER_RESOLVE
        or getattr(_LEDGER_RESOLVE, "__code__", None) is not _LEDGER_RESOLVE_CODE
        or JsonlDecisionLedger.verify_integrity is not _LEDGER_VERIFY
        or getattr(_LEDGER_VERIFY, "__code__", None) is not _LEDGER_VERIFY_CODE
        or DecisionRecord is not _RECORD_TYPE
        or DecisionRecord.__post_init__ is not _RECORD_POST_INIT
        or getattr(_RECORD_POST_INIT, "__code__", None) is not _RECORD_POST_INIT_CODE
        or DecisionRecord.to_dict is not _RECORD_TO_DICT
        or getattr(_RECORD_TO_DICT, "__code__", None) is not _RECORD_TO_DICT_CODE
        or WorkspaceEconomicLock is not _LOCK_TYPE
        or ensure_durable_file is not _ENSURE_DURABLE_FILE
        or getattr(_ENSURE_DURABLE_FILE, "__code__", None)
        is not _ENSURE_DURABLE_FILE_CODE
        or ProductProposalTargetTerminalPopulation is not _POPULATION_TYPE_EXPECTED
        or _POPULATION_TYPE is not _POPULATION_TYPE_EXPECTED
        or _POPULATION_AUTHORITY_PROPERTY_WITNESSES
        is not _POPULATION_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        or any(
            ProductProposalTargetTerminalPopulation.__dict__.get(name)
            is not descriptor
            or getattr(
                ProductProposalTargetTerminalPopulation.__dict__.get(name),
                "fget",
                None,
            )
            is not getter
            or getattr(getter, "__code__", None) is not code
            for name, descriptor, getter, code
            in _POPULATION_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        )
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is not _JSON_DUMPS_CODE
        or _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or getattr(_JSON_LOADS_EXPECTED, "__code__", None) is not _JSON_LOADS_CODE
        or json.JSONDecodeError is not _JSON_DECODE_ERROR_TYPE
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
        or _HELPER_WITNESSES is not _HELPER_WITNESSES_EXPECTED
        or any(
            globals().get(name) is not function
            or getattr(function, "__code__", None) is not code
            for name, function, code in _HELPER_WITNESSES_EXPECTED
        )
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target terminal-population authority dispatch changed"
        )


_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE:
        raise TypeError("workspace must be an exact native Path")
    value = value.expanduser()
    if not value.is_absolute():
        raise ProductProposalTargetTerminalPopulationError(
            "workspace must be an absolute path"
        )
    return value


def _text(value: object, name: str, *, max_length: int = 2048) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalTargetTerminalPopulationError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalTargetTerminalPopulationError(
            f"{name} contains unsupported characters"
        )
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name, max_length=64)
    if (
        len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductProposalTargetTerminalPopulationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _instant(value: object, name: str) -> datetime:
    value = _text(value, name, max_length=128)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalTargetTerminalPopulationError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalTargetTerminalPopulationError(
            f"{name} must include timezone"
        )
    return parsed


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _canonical_json(value: object) -> str:
    if (
        _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is not _JSON_DUMPS_CODE
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target terminal-population authority dispatch changed"
        )
    try:
        return _JSON_DUMPS(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "terminal-population material is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    if (
        _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target terminal-population authority dispatch changed"
        )
    return _HASHLIB_SHA256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProductProposalTargetTerminalPopulationError(
                "candidate context contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ProductProposalTargetTerminalPopulationError(
        "candidate context contains non-finite JSON"
    )


def _candidate_context(value: object) -> dict[str, object]:
    if (
        _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or getattr(_JSON_LOADS_EXPECTED, "__code__", None) is not _JSON_LOADS_CODE
        or json.JSONDecodeError is not _JSON_DECODE_ERROR_TYPE
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target terminal-population authority dispatch changed"
        )
    if type(value) is not str or not value:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context must be canonical JSON text"
        )
    try:
        parsed = _JSON_LOADS(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except _JSON_DECODE_ERROR_TYPE as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context is invalid JSON"
        ) from exc
    if type(parsed) is not dict or _canonical_json(parsed) != value:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context is not canonical JSON"
        )
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
    if set(parsed) != expected:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context schema is unsupported"
        )
    if type(parsed["legs"]) is not list or not parsed["legs"]:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context legs are invalid"
        )
    if type(parsed["quotes"]) is not list or not parsed["quotes"]:
        raise ProductProposalTargetTerminalPopulationError(
            "candidate context quotes are invalid"
        )
    return parsed


def _authority_snapshot(
    authority: MarketSettlementOutcomeAuthority,
    *,
    decision_at: datetime,
) -> dict[str, object]:
    if type(authority) is not _AUTHORITY_TYPE:
        raise TypeError(
            "authorities must contain exact MarketSettlementOutcomeAuthority values"
        )
    _require_dispatch()
    try:
        _AUTHORITY_AS_OF(authority, decision_at)
        raw = _AUTHORITY_TO_DICT(authority)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "market terminal authority cannot be verified at proposal time"
        ) from exc
    if (
        type(raw) is not dict
        or raw.get("authority_sha256") != authority.authority_sha256
        or type(authority.terminal_state_count) is not int
        or authority.terminal_state_count <= 0
        or type(authority.terminal_space_exact) is not bool
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "market terminal authority serialization is inconsistent"
        )
    _require_dispatch()
    _sha(_AUTHORITY_SHA_GETTER(authority), "market_authority_sha256")
    return raw


def _leg_key(value: Mapping[str, object]) -> tuple[object, ...]:
    return (
        value.get("event_id"),
        value.get("market_id"),
        value.get("selection_id"),
        value.get("sport"),
        value.get("exchange_side"),
    )


def _material(
    target: ProductProposalRiskTarget,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> dict[str, object]:
    if type(target) is not _TARGET_TYPE:
        raise TypeError("target must be exact ProductProposalRiskTarget")
    if (
        target.proposal_target_identity_proven is not True
        or target.proposal_target_counterfactual_execution_proven is not False
        or target.risk_upper_bound_for_target is not False
        or target.grants_ticket_authority is not False
        or target.grants_real_money_authority is not False
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target truth flags are inconsistent"
        )
    if type(authorities) is not tuple or not authorities:
        raise TypeError("authorities must be a non-empty exact tuple")
    if len(authorities) > _MAX_AUTHORITIES:
        raise ProductProposalTargetTerminalPopulationError(
            "market authority count exceeds supported bound"
        )

    decision_at = _instant(target.decision_ts, "target_decision_ts")
    by_sha: dict[str, MarketSettlementOutcomeAuthority] = {}
    by_identity: dict[
        tuple[str, str, str, str, str],
        MarketSettlementOutcomeAuthority,
    ] = {}
    snapshots: list[dict[str, object]] = []
    for authority in authorities:
        snapshot = _authority_snapshot(authority, decision_at=decision_at)
        digest = _sha(authority.authority_sha256, "market_authority_sha256")
        identity = authority.identity.identity_key
        if digest in by_sha:
            raise ProductProposalTargetTerminalPopulationError(
                "market authority list contains duplicate authority digest"
            )
        if identity in by_identity:
            raise ProductProposalTargetTerminalPopulationError(
                "market authority list contains duplicate provider-market identity"
            )
        by_sha[digest] = authority
        by_identity[identity] = authority
        snapshots.append(snapshot)

    used: set[str] = set()
    candidate_mappings: list[list[str]] = []
    for candidate_index, raw in enumerate(target.candidate_context_json):
        context = _candidate_context(raw)
        legs = context["legs"]
        quotes = context["quotes"]
        assert isinstance(legs, list)
        assert isinstance(quotes, list)
        quotes_by_key: dict[tuple[object, ...], Mapping[str, object]] = {}
        for quote in quotes:
            if not isinstance(quote, Mapping):
                raise ProductProposalTargetTerminalPopulationError(
                    "candidate quote is invalid"
                )
            key = _leg_key(quote)
            if key in quotes_by_key:
                raise ProductProposalTargetTerminalPopulationError(
                    "candidate quote identity is duplicated"
                )
            quotes_by_key[key] = quote

        mapped: list[str] = []
        for leg_index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                raise ProductProposalTargetTerminalPopulationError(
                    "candidate leg is invalid"
                )
            if leg.get("exchange_side") is not None:
                raise ProductProposalTargetTerminalPopulationError(
                    "provider terminal population does not yet prove exchange-side semantics"
                )
            sport = leg.get("sport")
            if type(sport) is not str or not sport:
                raise ProductProposalTargetTerminalPopulationError(
                    "target terminal population requires canonical sport identity"
                )
            quote = quotes_by_key.get(_leg_key(leg))
            if quote is None:
                raise ProductProposalTargetTerminalPopulationError(
                    f"candidate {candidate_index} leg {leg_index} lacks exact quote evidence"
                )
            source_id = quote.get("source_id")
            market_type = quote.get("market_type")
            selection_id = leg.get("selection_id")
            matches = [
                authority
                for authority in authorities
                if authority.identity.sport == sport
                and authority.identity.event_id == leg.get("event_id")
                and authority.identity.market_id == leg.get("market_id")
                and authority.identity.source_id == source_id
                and authority.identity.market_type.value == market_type
                and selection_id in authority.selection_ids
            ]
            if len(matches) != 1:
                raise ProductProposalTargetTerminalPopulationError(
                    "target leg does not resolve to exactly one provider terminal authority"
                )
            digest = _sha(
                matches[0].authority_sha256,
                "candidate market authority sha256",
            )
            mapped.append(digest)
            used.add(digest)
        candidate_mappings.append(mapped)

    if len(candidate_mappings) != len(target.candidate_sha256s):
        raise ProductProposalTargetTerminalPopulationError(
            "candidate/terminal-population cardinality mismatch"
        )
    if used != set(by_sha):
        raise ProductProposalTargetTerminalPopulationError(
            "market authority list contains terminal spaces unrelated to the proposal target"
        )

    market_groups: dict[
        tuple[str, str, str, str],
        list[MarketSettlementOutcomeAuthority],
    ] = {}
    for authority in authorities:
        market_groups.setdefault(authority.identity.market_key, []).append(authority)

    groups: list[dict[str, object]] = []
    total_states = 1
    # Exact per-market terminal spaces do not prove that their Cartesian product is
    # an exact joint state space. With more than one canonical market, cross-market
    # dependence can make combinations impossible; absent a separate joint-state
    # authority this population is an exhaustive conservative superset.
    all_exact = len(market_groups) == 1
    for market_key in sorted(market_groups):
        providers = sorted(
            market_groups[market_key],
            key=lambda item: (item.identity.source_id, item.authority_sha256),
        )
        baseline = providers[0]
        for authority in providers[1:]:
            if (
                authority.selection_ids != baseline.selection_ids
                or authority.settlement_semantics is not baseline.settlement_semantics
                or authority.settlement_rules_sha256
                != baseline.settlement_rules_sha256
                or authority.terminal_state_count != baseline.terminal_state_count
                or authority.terminal_space_exact is not baseline.terminal_space_exact
            ):
                raise ProductProposalTargetTerminalPopulationError(
                    "provider authorities disagree on canonical terminal-space semantics"
                )
        group: dict[str, object] = {
            "market_key": list(market_key),
            "authority_sha256s": [
                item.authority_sha256 for item in providers
            ],
            "terminal_state_count": baseline.terminal_state_count,
            "terminal_space_exact": baseline.terminal_space_exact,
        }
        group["market_group_sha256"] = _digest(group)
        groups.append(group)
        total_states *= baseline.terminal_state_count
        if total_states.bit_length() > 4096:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal population cardinality exceeds supported bound"
            )
        all_exact = all_exact and baseline.terminal_space_exact

    material = {
        "schema": _SCHEMA,
        "workspace_instance_id": _text(
            target.workspace_instance_id,
            "workspace_instance_id",
            max_length=256,
        ),
        "target_sha256": _sha(target.target_sha256, "target_sha256"),
        "target_decision_ts": _text(
            target.decision_ts,
            "target_decision_ts",
            max_length=128,
        ),
        "candidate_vector_sha256": _sha(
            target.candidate_vector_sha256,
            "candidate_vector_sha256",
        ),
        "candidate_sha256s": list(target.candidate_sha256s),
        "market_authorities": sorted(
            snapshots,
            key=lambda item: str(item["authority_sha256"]),
        ),
        "market_authority_sha256s": sorted(by_sha),
        "market_groups": groups,
        "market_group_sha256s": [
            str(group["market_group_sha256"]) for group in groups
        ],
        "candidate_market_authority_sha256s": candidate_mappings,
        "terminal_market_count": len(groups),
        "terminal_state_count": total_states,
        "terminal_space_exhaustive": True,
        "terminal_space_exact": all_exact,
        "probability_model_bound": False,
        "scientific_precommit_bound": False,
        "iid_member_mapping_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }
    _canonical_json(material)
    return material


def _current_economic_state(
    workspace: Path,
) -> tuple[EconomicGoalContract, PaperRiskPolicy, JsonlDecisionLedger]:
    _require_dispatch()
    try:
        goal = _GOAL_LOAD(_GOAL_STORE_TYPE(workspace))
    except (EconomicGoalContractError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "current product EconomicGoal cannot be resolved"
        ) from exc
    if type(goal) is not EconomicGoalContract:
        raise ProductProposalTargetTerminalPopulationError(
            "current EconomicGoal has non-canonical type"
        )
    policy = _POLICY_TYPE(economic_goal=goal)
    ledger = _LEDGER_TYPE(workspace / "decisions.jsonl")
    try:
        _ENSURE_DURABLE_FILE(ledger.path)
        _LEDGER_VERIFY(ledger)
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "canonical Decision Ledger cannot be verified"
        ) from exc
    return goal, policy, ledger


def _build(
    record: DecisionRecord,
    target: ProductProposalRiskTarget,
    material: dict[str, object],
    population_sha256: str,
) -> ProductProposalTargetTerminalPopulation:
    population_sha256 = _sha(population_sha256, "population_sha256")
    action_id = _ACTION_PREFIX + target.target_sha256
    if (
        type(record) is not _RECORD_TYPE
        or record.action != _ACTION
        or record.replay_run_id != action_id
        or record.decision_id != action_id
        or record.observed_ts != target.decision_ts
        or record.context_hash != population_sha256
        or record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY) != action_id
        or record.payload.get("population_sha256") != population_sha256
    ):
        raise ProductProposalTargetTerminalPopulationError(
            "persisted terminal-population envelope is inconsistent"
        )
    for key, expected in material.items():
        if _plain(record.payload.get(key)) != _plain(expected):
            raise ProductProposalTargetTerminalPopulationError(
                f"persisted terminal-population field {key!r} does not re-resolve"
            )
    if _digest(material) != population_sha256:
        raise ProductProposalTargetTerminalPopulationError(
            "terminal-population digest does not re-derive"
        )

    result = object.__new__(_POPULATION_TYPE)
    values = {
        "workspace_instance_id": material["workspace_instance_id"],
        "decision_id": action_id,
        "target_sha256": material["target_sha256"],
        "target_decision_ts": material["target_decision_ts"],
        "candidate_vector_sha256": material["candidate_vector_sha256"],
        "candidate_sha256s": tuple(material["candidate_sha256s"]),
        "market_authority_sha256s": tuple(
            material["market_authority_sha256s"]
        ),
        "market_authority_json": tuple(
            _canonical_json(item) for item in material["market_authorities"]
        ),
        "market_group_sha256s": tuple(material["market_group_sha256s"]),
        "market_group_json": tuple(
            _canonical_json(item) for item in material["market_groups"]
        ),
        "candidate_market_authority_sha256s": tuple(
            tuple(values)
            for values in material["candidate_market_authority_sha256s"]
        ),
        "terminal_market_count": material["terminal_market_count"],
        "terminal_state_count": material["terminal_state_count"],
        "terminal_space_exact": material["terminal_space_exact"],
        "population_sha256": population_sha256,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    _BIND_IDENTITY(result)
    return result


_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
    )
    for name in (
        "_workspace_path",
        "_text",
        "_sha",
        "_instant",
        "_plain",
        "_canonical_json",
        "_digest",
        "_reject_duplicate_keys",
        "_reject_nonfinite",
        "_candidate_context",
        "_authority_snapshot",
        "_leg_key",
        "_material",
        "_current_economic_state",
        "_build",
    )
)
_HELPER_WITNESSES_EXPECTED = _HELPER_WITNESSES


def issue_product_proposal_target_terminal_population(
    workspace: Path,
    *,
    target_sha256: str,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> ProductProposalTargetTerminalPopulation:
    """Persist one target-specific provider terminal-space precommit."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalTargetTerminalPopulationError(
            "terminal-population dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    target_sha256 = _sha(target_sha256, "target_sha256")
    try:
        target = _TARGET_RESOLVER(workspace, target_sha256)
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target cannot be re-resolved"
        ) from exc
    material = _material(target, authorities)
    population_sha256 = _digest(material)
    action_id = _ACTION_PREFIX + target_sha256

    # The target resolver owns its own workspace lock. Do not nest it here. As with
    # the target/science join, mandatory post-append re-resolution below catches a
    # concurrent product transition and leaves at most a non-authorizing audit row.
    with _LOCK_TYPE(workspace):
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            existing = _LEDGER_RESOLVE(ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal-population Decision Ledger re-resolution failed"
            ) from exc
        if existing is None:
            payload = {
                **material,
                "population_sha256": population_sha256,
                MATERIAL_ACTION_ID_PAYLOAD_KEY: action_id,
            }
            record = _RECORD_TYPE(
                replay_run_id=action_id,
                agent=_AGENT,
                observed_ts=target.decision_ts,
                action=_ACTION,
                payload=payload,
                context_hash=population_sha256,
                decision_id=action_id,
            )
            try:
                _LEDGER_APPEND(
                    ledger,
                    record,
                    goal,
                    risk_policy=policy,
                )
                existing = _LEDGER_RESOLVE(ledger,
                    action_id,
                    goal,
                    risk_policy=policy,
                )
            except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
                raise ProductProposalTargetTerminalPopulationError(
                    "terminal-population Decision Ledger append failed"
                ) from exc
        if existing is None:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal-population append did not re-resolve"
            )

    try:
        fresh_target = _TARGET_RESOLVER(
            workspace,
            target_sha256,
        )
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target changed during terminal-population precommit"
        ) from exc
    fresh_material = _material(fresh_target, authorities)
    if _digest(fresh_material) != population_sha256:
        raise ProductProposalTargetTerminalPopulationError(
            "target/source authority changed during terminal-population precommit"
        )

    with _LOCK_TYPE(workspace):
        goal, policy, ledger = _current_economic_state(workspace)
        final_record = _LEDGER_RESOLVE(ledger,
            action_id,
            goal,
            risk_policy=policy,
        )
        if final_record is None:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal-population disappeared after durable append"
            )
        return _build(
            final_record,
            fresh_target,
            fresh_material,
            population_sha256,
        )


def resolve_product_proposal_target_terminal_population(
    workspace: Path,
    *,
    target_sha256: str,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> ProductProposalTargetTerminalPopulation:
    """Re-resolve a terminal population against separately reverified source authority."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalTargetTerminalPopulationError(
            "terminal-population dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    target_sha256 = _sha(target_sha256, "target_sha256")
    try:
        target = _TARGET_RESOLVER(workspace, target_sha256)
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalTargetTerminalPopulationError(
            "proposal target cannot be re-resolved"
        ) from exc
    material = _material(target, authorities)
    population_sha256 = _digest(material)
    action_id = _ACTION_PREFIX + target_sha256
    with _LOCK_TYPE(workspace):
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            record = _LEDGER_RESOLVE(ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal-population Decision Ledger re-resolution failed"
            ) from exc
        if record is None:
            raise ProductProposalTargetTerminalPopulationError(
                "terminal population is missing from the canonical Decision Ledger"
            )
        return _build(record, target, material, population_sha256)
