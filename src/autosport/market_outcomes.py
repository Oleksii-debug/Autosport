from __future__ import annotations

import contextvars
import hashlib
import itertools
import json
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .domain import MarketType, _canonical_sport_value, _quote_identity


_SHA256_HEX = frozenset("0123456789abcdef")
_VERIFIED_AUTHORITY_TOKEN = object()
_VERIFIED_AUTHORITY_ISSUANCE = contextvars.ContextVar(
    "autosport_market_outcome_authority_issuance",
    default=False,
)


def _build_issued_authority_registry():
    """Hide positive authority issuance membership from module-global mutation."""

    issued_objects: weakref.WeakValueDictionary[int, object] = (
        weakref.WeakValueDictionary()
    )
    issued_digests: dict[int, str] = {}

    def register(authority: object, digest: str) -> None:
        identity = id(authority)
        issued_objects[identity] = authority
        issued_digests[identity] = digest
        weakref.finalize(authority, issued_digests.pop, identity, None)

    def issued_digest(authority: object) -> str | None:
        identity = id(authority)
        if issued_objects.get(identity) is not authority:
            return None
        return issued_digests.get(identity)

    return register, issued_digest


_register_issued_authority, _issued_authority_digest = (
    _build_issued_authority_registry()
)
del _build_issued_authority_registry

_CANONICAL_REGISTER_ISSUED_AUTHORITY = _register_issued_authority
_CANONICAL_REGISTER_ISSUED_AUTHORITY_CODE = getattr(
    _CANONICAL_REGISTER_ISSUED_AUTHORITY,
    "__code__",
    None,
)
_CANONICAL_ISSUED_AUTHORITY_DIGEST = _issued_authority_digest
_CANONICAL_ISSUED_AUTHORITY_DIGEST_CODE = getattr(
    _CANONICAL_ISSUED_AUTHORITY_DIGEST,
    "__code__",
    None,
)


def _assert_canonical_issued_authority_registry_dispatch() -> None:
    checks = (
        (
            _register_issued_authority,
            _CANONICAL_REGISTER_ISSUED_AUTHORITY,
            _CANONICAL_REGISTER_ISSUED_AUTHORITY_CODE,
        ),
        (
            _issued_authority_digest,
            _CANONICAL_ISSUED_AUTHORITY_DIGEST,
            _CANONICAL_ISSUED_AUTHORITY_DIGEST_CODE,
        ),
    )
    for current, expected, expected_code in checks:
        if (
            current is not expected
            or getattr(expected, "__code__", None) is not expected_code
        ):
            raise ValueError(
                "market outcome authority issuance registry dispatch was replaced"
            )


_CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD = (
    _assert_canonical_issued_authority_registry_dispatch
)
_CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD_CODE = getattr(
    _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD,
    "__code__",
    None,
)
_BETFAIR_SOURCE_ID = "betfair_exchange_historical"
_BETFAIR_TABLE_TENNIS_EVENT_TYPE_ID = "2593174"
_BETFAIR_MATCH_ODDS_TYPE = "MATCH_ODDS"
_BETFAIR_SETTLEMENT_STATUS_MAP = {"WINNER": "win", "LOSER": "loss", "REMOVED": "void"}


class OutcomeRosterBasis(str, Enum):
    """Where an exhaustive selection roster was obtained.

    OBSERVED_ROWS_ONLY is deliberately non-authoritative: seeing quote rows does not
    prove that an unobserved real market outcome does not exist.
    """

    PROVIDER_MARKET_DEFINITION = "provider_market_definition"
    GOVERNED_DATASET_MARKET_DEFINITION = "governed_dataset_market_definition"
    OBSERVED_ROWS_ONLY = "observed_rows_only"


class SettlementSemantics(str, Enum):
    """Settlement state families that Autosport can currently prove mechanically."""

    EXCLUSIVE_SINGLE_WINNER = "exclusive_single_winner"
    EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID = "exclusive_single_winner_or_all_void"
    CANONICAL_WIN_LOSS_VOID_SUPERSET = "canonical_win_loss_void_superset"


class SettlementResult(str, Enum):
    WIN = "win"
    LOSS = "loss"
    VOID = "void"


class OutcomeAuthorityStatus(str, Enum):
    PROVEN_EXHAUSTIVE = "proven_exhaustive"
    REFUSED = "refused"


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_betfair_runner_id(name: str, value: object) -> str:
    """Validate Betfair runner identity before canonical text normalization."""

    if type(value) is str:
        return _canonical_text(name, value)
    if type(value) is int:
        if value <= 0 or value > 0x7FFFFFFFFFFFFFFF:
            raise ValueError(f"{name} integer must be a positive signed-64-bit value")
        return str(value)
    raise ValueError(f"{name} must be a canonical string or signed-64-bit integer")


def _canonical_timestamp(name: str, value: object) -> tuple[str, datetime]:
    raw = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return raw, parsed


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _SHA256_HEX for character in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    return digest


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketOutcomeIdentity:
    """Exact provider-scoped market identity for terminal-outcome authority."""

    sport: str
    event_id: str
    market_id: str
    source_id: str
    market_type: MarketType

    def __post_init__(self) -> None:
        _canonical_sport_value(self.sport, "market outcome sport")
        _canonical_text("market outcome event_id", self.event_id)
        _canonical_text("market outcome market_id", self.market_id)
        _canonical_text("market outcome source_id", self.source_id)
        if not isinstance(self.market_type, MarketType):
            raise ValueError("market outcome market_type must be a MarketType")

    @property
    def identity_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.sport,
            self.event_id,
            self.market_id,
            self.source_id,
            self.market_type.value,
        )

    @property
    def market_key(self) -> tuple[str, str, str, str]:
        """Provider-independent key only when canonical market IDs already align."""
        return (
            self.sport,
            self.event_id,
            self.market_id,
            self.market_type.value,
        )

    def quote_key(self, selection_id: str) -> str:
        selection = _canonical_text("market outcome selection_id", selection_id)
        return _quote_identity(
            self.event_id,
            self.market_id,
            selection,
            self.sport,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "source_id": self.source_id,
            "market_type": self.market_type.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketOutcomeIdentity":
        expected = {"sport", "event_id", "market_id", "source_id", "market_type"}
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("serialized market outcome identity must contain canonical fields")
        try:
            return cls(
                sport=raw["sport"],
                event_id=raw["event_id"],
                market_id=raw["market_id"],
                source_id=raw["source_id"],
                market_type=MarketType(raw["market_type"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("serialized market outcome identity is invalid") from exc


@dataclass(frozen=True, slots=True)
class MarketTerminalState:
    """One fully specified terminal settlement state for the authoritative roster."""

    state_id: str
    settlements: tuple[tuple[str, SettlementResult], ...]

    def __post_init__(self) -> None:
        _canonical_text("terminal state_id", self.state_id)
        if type(self.settlements) is not tuple or not self.settlements:
            raise ValueError("terminal settlements must be a non-empty canonical tuple")
        seen: set[str] = set()
        ordered: list[str] = []
        for item in self.settlements:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError(
                    "terminal settlements must contain (selection_id, result) tuples"
                )
            selection_id, result = item
            selection = _canonical_text("terminal selection_id", selection_id)
            if selection in seen:
                raise ValueError("terminal settlements contain duplicate selection_id")
            if not isinstance(result, SettlementResult):
                raise ValueError("terminal settlement result must be a SettlementResult")
            seen.add(selection)
            ordered.append(selection)
        if tuple(ordered) != tuple(sorted(ordered)):
            raise ValueError("terminal settlements must use canonical selection order")

    def to_dict(self) -> dict[str, object]:
        return {
            "state_id": self.state_id,
            "settlements": [
                {"selection_id": selection_id, "result": result.value}
                for selection_id, result in self.settlements
            ],
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MarketSettlementOutcomeAuthority:
    """Canonical exhaustive terminal-outcome authority for supported market semantics.

    Completeness is derived from an authoritative selection roster plus a supported
    deterministic settlement family. Caller-supplied ScenarioGroup membership and
    opaque hashes cannot add or remove terminal states.
    """

    identity: MarketOutcomeIdentity
    selection_ids: tuple[str, ...]
    roster_basis: OutcomeRosterBasis
    settlement_semantics: SettlementSemantics
    source_revision: str
    causal_cutoff: str
    observed_at: str
    roster_provenance_sha256: str
    settlement_rules_sha256: str
    verification_protocol_sha256: str
    _verification_token: object = field(repr=False, compare=False)

    def __getattribute__(self, name: str) -> object:
        # Authority-bearing instance dispatch must remain on the exact class surface
        # captured after class construction. This closes class-method/property
        # rebinding that could otherwise bypass assert_issued_integrity().
        _assert_canonical_market_outcome_authority_dispatch()
        return object.__getattribute__(self, name)

    def __post_init__(self) -> None:
        if (
            self._verification_token is not _VERIFIED_AUTHORITY_TOKEN
            or not _VERIFIED_AUTHORITY_ISSUANCE.get()
        ):
            raise TypeError(
                "MarketSettlementOutcomeAuthority must come from verified evidence"
            )
        if not isinstance(self.identity, MarketOutcomeIdentity):
            raise TypeError("identity must be MarketOutcomeIdentity")
        if self.identity.market_type is not MarketType.WINNER:
            raise ValueError(
                "authoritative terminal outcome semantics currently support winner markets only"
            )
        if type(self.selection_ids) is not tuple or len(self.selection_ids) < 2:
            raise ValueError("authoritative outcome roster requires at least two selections")
        selections = tuple(
            _canonical_text("authoritative selection_id", selection_id)
            for selection_id in self.selection_ids
        )
        if selections != tuple(sorted(selections)):
            raise ValueError("authoritative selection_ids must be sorted")
        if len(selections) != len(set(selections)):
            raise ValueError("authoritative selection_ids must be unique")
        for selection_id in selections:
            self.identity.quote_key(selection_id)

        if not isinstance(self.roster_basis, OutcomeRosterBasis):
            raise ValueError("roster_basis must be an OutcomeRosterBasis")
        if self.roster_basis is OutcomeRosterBasis.OBSERVED_ROWS_ONLY:
            raise ValueError(
                "observed quote rows cannot establish exhaustive market outcome authority"
            )
        if not isinstance(self.settlement_semantics, SettlementSemantics):
            raise ValueError("settlement_semantics must be SettlementSemantics")

        _canonical_text("source_revision", self.source_revision)
        _, cutoff = _canonical_timestamp("causal_cutoff", self.causal_cutoff)
        _, observed = _canonical_timestamp("observed_at", self.observed_at)
        if cutoff > observed:
            raise ValueError("causal_cutoff must not be after observed_at")
        _canonical_sha256(
            "roster_provenance_sha256", self.roster_provenance_sha256
        )
        _canonical_sha256("settlement_rules_sha256", self.settlement_rules_sha256)
        _canonical_sha256(
            "verification_protocol_sha256", self.verification_protocol_sha256
        )
        self._register_issued_integrity()

    def _calculated_authority_sha256(self) -> str:
        return _sha256_payload(self._identity_payload())

    def _register_issued_integrity(self) -> None:
        if (
            _assert_canonical_issued_authority_registry_dispatch
            is not _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD
            or getattr(
                _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD,
                "__code__",
                None,
            )
            is not _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD_CODE
        ):
            raise ValueError(
                "market outcome authority issuance registry guard was replaced"
            )
        _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD()
        _CANONICAL_REGISTER_ISSUED_AUTHORITY(
            self,
            self._calculated_authority_sha256(),
        )

    def assert_issued_integrity(self) -> None:
        """Require the exact still-unchanged authority issued by a verified adapter."""

        if type(self) is not MarketSettlementOutcomeAuthority:
            raise ValueError(
                "market outcome authority is not the exact product-issued instance"
            )
        if (
            _assert_canonical_issued_authority_registry_dispatch
            is not _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD
            or getattr(
                _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD,
                "__code__",
                None,
            )
            is not _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD_CODE
        ):
            raise ValueError(
                "market outcome authority issuance registry guard was replaced"
            )
        _CANONICAL_ISSUED_AUTHORITY_REGISTRY_GUARD()
        try:
            current_digest = self._calculated_authority_sha256()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "market outcome authority mutated after verified issuance"
            ) from exc
        issued_digest = _CANONICAL_ISSUED_AUTHORITY_DIGEST(self)
        if issued_digest is None:
            raise ValueError(
                "market outcome authority is not the exact product-issued instance"
            )
        if current_digest != issued_digest:
            raise ValueError(
                "market outcome authority mutated after verified issuance"
            )

    @property
    def quote_keys(self) -> tuple[str, ...]:
        self.assert_issued_integrity()
        return tuple(
            self.identity.quote_key(selection_id)
            for selection_id in self.selection_ids
        )

    def _terminal_state_count_unchecked(self) -> int:
        if (
            self.settlement_semantics
            is SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
        ):
            return 3 ** len(self.selection_ids)
        if (
            self.settlement_semantics
            is SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID
        ):
            return len(self.selection_ids) + 1
        return len(self.selection_ids)

    def _terminal_space_exact_unchecked(self) -> bool:
        return (
            self.settlement_semantics
            is not SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
        )

    @property
    def terminal_state_count(self) -> int:
        self.assert_issued_integrity()
        return self._terminal_state_count_unchecked()

    @property
    def terminal_space_exact(self) -> bool:
        self.assert_issued_integrity()
        return self._terminal_space_exact_unchecked()

    @property
    def terminal_states(self) -> tuple[MarketTerminalState, ...]:
        self.assert_issued_integrity()
        if (
            self.settlement_semantics
            is SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
        ):
            states: list[MarketTerminalState] = []
            results = tuple(SettlementResult)
            for combination in itertools.product(
                results,
                repeat=len(self.selection_ids),
            ):
                state_id = "canonical:" + ",".join(
                    result.value for result in combination
                )
                states.append(
                    MarketTerminalState(
                        state_id=state_id,
                        settlements=tuple(
                            zip(self.selection_ids, combination)
                        ),
                    )
                )
            return tuple(states)

        states = [
            MarketTerminalState(
                state_id=f"winner:{winner}",
                settlements=tuple(
                    (
                        selection_id,
                        (
                            SettlementResult.WIN
                            if selection_id == winner
                            else SettlementResult.LOSS
                        ),
                    )
                    for selection_id in self.selection_ids
                ),
            )
            for winner in self.selection_ids
        ]
        if (
            self.settlement_semantics
            is SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID
        ):
            states.append(
                MarketTerminalState(
                    state_id="all_void",
                    settlements=tuple(
                        (selection_id, SettlementResult.VOID)
                        for selection_id in self.selection_ids
                    ),
                )
            )
        return tuple(states)

    def assert_available_as_of(self, decision_as_of: datetime) -> None:
        self.assert_issued_integrity()
        if type(decision_as_of) is not datetime:
            raise TypeError("decision_as_of must be an exact datetime")
        if (
            decision_as_of.tzinfo is None
            or decision_as_of.utcoffset() is None
        ):
            raise ValueError("decision_as_of must be timezone-aware")
        boundary = decision_as_of.astimezone(timezone.utc)
        _, cutoff = _canonical_timestamp("causal_cutoff", self.causal_cutoff)
        _, observed = _canonical_timestamp("observed_at", self.observed_at)
        if (
            cutoff.astimezone(timezone.utc) > boundary
            or observed.astimezone(timezone.utc) > boundary
        ):
            raise ValueError(
                "market outcome authority is not causally available at decision_as_of"
            )

    def _state_is_derived(self, state: MarketTerminalState) -> bool:
        if type(state) is not MarketTerminalState:
            return False
        actual_ids = tuple(
            selection_id for selection_id, _ in state.settlements
        )
        if actual_ids != self.selection_ids:
            return False
        actual = dict(state.settlements)

        if (
            self.settlement_semantics
            is SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
        ):
            expected_state_id = "canonical:" + ",".join(
                actual[selection_id].value
                for selection_id in self.selection_ids
            )
            return state.state_id == expected_state_id

        if (
            self.settlement_semantics
            is SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID
            and state.state_id == "all_void"
        ):
            return all(
                result is SettlementResult.VOID
                for result in actual.values()
            )

        prefix = "winner:"
        if not state.state_id.startswith(prefix):
            return False
        winner = state.state_id[len(prefix):]
        if winner not in self.selection_ids:
            return False
        return all(
            result
            is (
                SettlementResult.WIN
                if selection_id == winner
                else SettlementResult.LOSS
            )
            for selection_id, result in state.settlements
        )

    def settlement_by_quote(
        self, state: MarketTerminalState
    ) -> dict[str, str]:
        self.assert_issued_integrity()
        if type(state) is not MarketTerminalState:
            raise TypeError("state must be exact MarketTerminalState")
        if not self._state_is_derived(state):
            raise ValueError(
                "terminal state is not derived from this outcome authority"
            )
        return {
            self.identity.quote_key(selection_id): result.value
            for selection_id, result in state.settlements
        }

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.market_settlement_outcome_authority",
            "schema_version": 1,
            "identity": self.identity.to_dict(),
            "selection_ids": list(self.selection_ids),
            "roster_basis": self.roster_basis.value,
            "settlement_semantics": self.settlement_semantics.value,
            "source_revision": self.source_revision,
            "causal_cutoff": self.causal_cutoff,
            "observed_at": self.observed_at,
            "roster_provenance_sha256": self.roster_provenance_sha256,
            "settlement_rules_sha256": self.settlement_rules_sha256,
            "verification_protocol_sha256": self.verification_protocol_sha256,
            "terminal_space_exact": self._terminal_space_exact_unchecked(),
            "terminal_state_count": self._terminal_state_count_unchecked(),
        }

    @property
    def authority_sha256(self) -> str:
        self.assert_issued_integrity()
        return self._calculated_authority_sha256()

    def to_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "authority_sha256": self.authority_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        verified_authority: MarketSettlementOutcomeAuthority | None = None,
    ) -> "MarketSettlementOutcomeAuthority":
        """Read durable identity only against separately re-verified source evidence.

        A self-consistent JSON payload is not allowed to mint provider authority after
        restart. Callers must first re-run the canonical provider/dataset verifier and
        supply that independently derived authority here; durable data then proves
        identity/equality only.
        """
        if (
            cls is not _CANONICAL_MARKET_OUTCOME_AUTHORITY_TYPE
            or type(verified_authority) is not _CANONICAL_MARKET_OUTCOME_AUTHORITY_TYPE
        ):
            raise ValueError(
                "durable market outcome authority readback requires separately "
                "verified source authority of exact canonical type"
            )
        canonical = verified_authority.to_dict()
        expected = set(canonical)
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError(
                "serialized market outcome authority must contain canonical fields"
            )
        if (
            raw["schema"] != "autosport.market_settlement_outcome_authority"
            or raw["schema_version"] != 1
        ):
            raise ValueError("unsupported market outcome authority schema")
        if type(raw["selection_ids"]) is not list:
            raise ValueError(
                "serialized market outcome selection_ids must be a list"
            )
        if (
            type(raw["terminal_state_count"]) is not int
            or raw["terminal_state_count"] != canonical["terminal_state_count"]
        ):
            raise ValueError(
                "serialized terminal-state count does not match verified source authority"
            )
        if raw["terminal_space_exact"] is not canonical["terminal_space_exact"]:
            raise ValueError(
                "serialized terminal-space exactness does not match verified source authority"
            )
        if raw["authority_sha256"] != canonical["authority_sha256"]:
            raise ValueError("market outcome authority hash mismatch")
        if raw != canonical:
            raise ValueError(
                "serialized market outcome authority does not match separately "
                "verified source evidence"
            )
        return verified_authority


_MISSING_AUTHORITY_CLASS_SLOT = object()
_CANONICAL_MARKET_OUTCOME_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority


def _authority_descriptor_code_identity(member: object) -> tuple[object, ...]:
    """Capture executable descriptor code without invoking mutable descriptors."""

    if isinstance(member, property):
        return tuple(
            None if accessor is None else getattr(accessor, "__code__", None)
            for accessor in (member.fget, member.fset, member.fdel)
        )
    if isinstance(member, (classmethod, staticmethod)):
        function = member.__func__
        return (getattr(function, "__code__", None),)
    return (getattr(member, "__code__", None),)


_CANONICAL_MARKET_OUTCOME_AUTHORITY_CLASS_SURFACE = tuple(
    (
        name,
        member,
        _authority_descriptor_code_identity(member),
    )
    for name in (
        "identity",
        "selection_ids",
        "roster_basis",
        "settlement_semantics",
        "source_revision",
        "causal_cutoff",
        "observed_at",
        "roster_provenance_sha256",
        "settlement_rules_sha256",
        "verification_protocol_sha256",
        "_verification_token",
        "__post_init__",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "_calculated_authority_sha256",
        "_register_issued_integrity",
        "assert_issued_integrity",
        "quote_keys",
        "_terminal_state_count_unchecked",
        "_terminal_space_exact_unchecked",
        "terminal_state_count",
        "terminal_space_exact",
        "terminal_states",
        "assert_available_as_of",
        "_state_is_derived",
        "settlement_by_quote",
        "_identity_payload",
        "authority_sha256",
        "to_dict",
        "from_dict",
    )
    for member in (
        vars(MarketSettlementOutcomeAuthority).get(
            name,
            _MISSING_AUTHORITY_CLASS_SLOT,
        ),
    )
)


def _assert_canonical_market_outcome_authority_dispatch() -> None:
    """Fail closed if authority-bearing class dispatch changed after import."""

    class_dict = vars(_CANONICAL_MARKET_OUTCOME_AUTHORITY_TYPE)
    for name, expected, expected_code in (
        _CANONICAL_MARKET_OUTCOME_AUTHORITY_CLASS_SURFACE
    ):
        current = class_dict.get(name, _MISSING_AUTHORITY_CLASS_SLOT)
        if current is not expected:
            raise ValueError(
                "canonical market outcome authority class dispatch was replaced"
            )
        if isinstance(current, property):
            current_code = tuple(
                None if accessor is None else getattr(accessor, "__code__", None)
                for accessor in (current.fget, current.fset, current.fdel)
            )
        elif isinstance(current, (classmethod, staticmethod)):
            current_code = (getattr(current.__func__, "__code__", None),)
        else:
            current_code = (getattr(current, "__code__", None),)
        if current_code != expected_code:
            raise ValueError(
                "canonical market outcome authority class dispatch was replaced"
            )


@dataclass(frozen=True, slots=True)
class MarketOutcomeAuthorityAssessment:
    identity: MarketOutcomeIdentity
    status: OutcomeAuthorityStatus
    authority: MarketSettlementOutcomeAuthority | None
    refusal_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, MarketOutcomeIdentity):
            raise TypeError("assessment identity must be MarketOutcomeIdentity")
        if not isinstance(self.status, OutcomeAuthorityStatus):
            raise ValueError("assessment status must be OutcomeAuthorityStatus")
        if self.status is OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE:
            if (
                type(self.authority) is not _CANONICAL_MARKET_OUTCOME_AUTHORITY_TYPE
                or self.refusal_reason is not None
            ):
                raise ValueError(
                    "proven assessment requires exact canonical authority and no refusal"
                )
            self.authority.assert_issued_integrity()
            if self.authority.identity != self.identity:
                raise ValueError("assessment authority identity mismatch")
        else:
            if self.authority is not None:
                raise ValueError("refused assessment must not carry authority")
            _canonical_text("refusal_reason", self.refusal_reason)


def assess_market_outcome_authority(
    *,
    identity: MarketOutcomeIdentity,
    selection_ids: tuple[str, ...],
    roster_basis: OutcomeRosterBasis,
    settlement_semantics: SettlementSemantics | None,
    source_revision: str,
    causal_cutoff: str,
    observed_at: str,
    roster_provenance_sha256: str,
    settlement_rules_sha256: str,
    verification_protocol_sha256: str,
) -> MarketOutcomeAuthorityAssessment:
    """Refuse raw caller assertions; exhaustive authority needs verified source evidence."""

    if not isinstance(identity, MarketOutcomeIdentity):
        raise TypeError("identity must be MarketOutcomeIdentity")
    if not isinstance(roster_basis, OutcomeRosterBasis):
        raise ValueError("roster_basis must be OutcomeRosterBasis")
    if roster_basis is OutcomeRosterBasis.OBSERVED_ROWS_ONLY:
        reason = "observed_rows_do_not_prove_exhaustive_selection_roster"
    elif identity.market_type is not MarketType.WINNER:
        reason = "market_type_has_no_supported_terminal_settlement_semantics"
    elif settlement_semantics is None:
        reason = "settlement_semantics_not_proven"
    else:
        reason = "caller_supplied_roster_has_no_verified_revision_evidence"
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.REFUSED,
        authority=None,
        refusal_reason=reason,
    )


def assess_betfair_historical_market_definition_authority(
    *,
    market_id: str,
    market_definition: dict[str, object],
    provider_publish_at: str,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Structurally assess raw Betfair marketDefinition evidence.

    Raw caller bytes and timestamps are assertions, not provider-origin evidence.
    Validate the supported market shape but refuse positive exhaustive authority
    until a product-owned Betfair acquisition witness is composed at this boundary.
    """

    market = _canonical_text("market_id", market_id)
    _, publish_dt = _canonical_timestamp(
        "provider_publish_at",
        provider_publish_at,
    )
    _, observed_dt = _canonical_timestamp("observed_at", observed_at)
    if publish_dt > observed_dt:
        raise ValueError("provider_publish_at must not be after observed_at")
    if type(market_definition) is not dict:
        raise ValueError("market_definition must be a JSON object")

    event_id = _canonical_text(
        "marketDefinition.eventId",
        market_definition.get("eventId"),
    )
    event_type_id = _canonical_text(
        "marketDefinition.eventTypeId",
        market_definition.get("eventTypeId"),
    )
    provider_market_type = _canonical_text(
        "marketDefinition.marketType",
        market_definition.get("marketType"),
    )
    status = _canonical_text(
        "marketDefinition.status",
        market_definition.get("status"),
    ).upper()

    identity = MarketOutcomeIdentity(
        sport="table_tennis",
        event_id=event_id,
        market_id=market,
        source_id=_BETFAIR_SOURCE_ID,
        market_type=(
            MarketType.WINNER
            if provider_market_type == _BETFAIR_MATCH_ODDS_TYPE
            else MarketType.OTHER
        ),
    )
    if event_type_id != _BETFAIR_TABLE_TENNIS_EVENT_TYPE_ID:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="betfair_event_type_has_no_verified_roster_protocol",
        )
    if provider_market_type != _BETFAIR_MATCH_ODDS_TYPE:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="market_type_has_no_supported_terminal_settlement_semantics",
        )
    if status != "OPEN":
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="betfair_market_definition_is_not_open_at_roster_revision",
        )

    complete = market_definition.get("complete")
    if type(complete) is not bool:
        raise ValueError("marketDefinition.complete must be a boolean")
    if not complete:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="betfair_market_definition_runner_roster_is_not_complete",
        )

    runners = market_definition.get("runners")
    if type(runners) is not list or len(runners) < 2:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="betfair_market_definition_lacks_authoritative_runner_roster",
        )
    selection_ids: list[str] = []
    for index, runner in enumerate(runners):
        if type(runner) is not dict or runner.get("id") is None:
            raise ValueError(
                f"marketDefinition.runners[{index}] requires id"
            )
        selection_ids.append(
            _canonical_betfair_runner_id(
                f"marketDefinition.runners[{index}].id",
                runner["id"],
            )
        )
    if len(selection_ids) != len(set(selection_ids)):
        raise ValueError("marketDefinition.runners contains duplicate selection id")
    # Structural validity is necessary but not sufficient for provider truth.
    # betfair_historical_read_once only freezes bytes from a user-supplied file,
    # and historical governance binds rights/retention records; neither authenticates
    # these exact marketDefinition bytes as Betfair-origin evidence.
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.REFUSED,
        authority=None,
        refusal_reason="betfair_market_definition_provider_origin_unverified",
    )
