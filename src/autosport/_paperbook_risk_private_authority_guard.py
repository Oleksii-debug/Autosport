"""Bind PaperRiskPolicy derivations to private economic and generation authority.

Structural lifecycle replay can prove internal coherence but cannot prove that caller-
visible ticket economics are still product-issued opening/causal facts. This final
admission layer reuses the existing generation critical section plus the canonical
opening/causal registries. Exported risk wrappers capture their admission function and
executable spec in closure cells; mutable wrapper ``__globals__`` entries are therefore
non-authoritative. The outer admission independently rechecks the frozen PaperBook and
PaperRiskPolicy class graphs and loaded-state validator before and after the delegated
calculation, so even a reachable pre-admission delegate cannot mint positive authority.

No risk store, parser, serializer, registry, journal, generation authority or second
risk engine is introduced.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _authority
from . import paper as _paper
from . import risk as _risk


# Compatibility/evidence names intentionally remain module-visible. Exported wrappers
# do not load them from globals; adversarial tests may retarget these handles and must
# observe no change in positive authority.
_BOOK_STATE_SPEC: tuple[object, ...] | None = None
_PORTFOLIO_HASH_SPEC: tuple[object, ...] | None = None
_HISTORICAL_METRICS_SPEC: tuple[object, ...] | None = None
_SHADOW_BOOK_SPEC: tuple[object, ...] | None = None
_IDENTITY_CONCENTRATION_SPEC: tuple[object, ...] | None = None
_DERIVE_GOAL_STAKE_SPEC: tuple[object, ...] | None = None
_DERIVE_GOAL_STAKE_VECTOR_SPEC: tuple[object, ...] | None = None
_EVALUATE_SPEC: tuple[object, ...] | None = None


def _guarded_private_call(*_args, **_kwargs):
    """Non-authoritative compatibility handle; installed wrappers never call this."""

    raise RuntimeError("module-global private risk dispatch is not an authority")


def _function_for_descriptor(owner: type, name: str, descriptor_type: type) -> FunctionType:
    descriptor = vars(owner).get(name)
    if type(descriptor) is not descriptor_type:
        raise RuntimeError(f"canonical risk descriptor changed: {name}")
    function = descriptor.__func__
    if type(function) is not FunctionType:
        raise RuntimeError(f"canonical risk executable changed: {name}")
    return function


def _plain_function(owner: type, name: str) -> FunctionType:
    function = vars(owner).get(name)
    if type(function) is not FunctionType:
        raise RuntimeError(f"canonical risk method changed: {name}")
    return function


def _delegate_spec(function: FunctionType) -> tuple[object, ...]:
    """Capture executable state without retaining the callable itself."""

    return (
        function.__code__,
        function.__name__,
        function.__defaults__,
        None if function.__kwdefaults__ is None else dict(function.__kwdefaults__),
        function.__closure__,
        tuple(function.__globals__.items()),
    )


def _make_admission(
    *,
    canonical_paper_book: type,
    canonical_risk_policy: type,
    paperbook_class_witnesses: tuple[object, ...],
    risk_class_witnesses: tuple[object, ...],
    frozen_require_class_graph: FunctionType,
    frozen_validate_loaded_state: FunctionType,
    frozen_bound_snapshot_path: FunctionType,
    frozen_acquire_lock: FunctionType,
    frozen_release_lock: FunctionType,
    frozen_witness_path: FunctionType,
    risk_read_local: object,
    frozen_call_witnessed: FunctionType,
    require_opening: FunctionType,
    require_opening_witness: tuple[object, ...],
    require_causal: FunctionType,
    require_causal_witness: tuple[object, ...],
):
    function_type = FunctionType
    exact_type = type
    exact_getattr = getattr
    graph_code = frozen_require_class_graph.__code__
    validate_code = frozen_validate_loaded_state.__code__
    bound_code = frozen_bound_snapshot_path.__code__
    acquire_code = frozen_acquire_lock.__code__
    release_code = frozen_release_lock.__code__
    witness_path_code = frozen_witness_path.__code__
    call_witnessed_code = frozen_call_witnessed.__code__

    def require_executables() -> None:
        witnesses = (
            (frozen_require_class_graph, graph_code),
            (frozen_validate_loaded_state, validate_code),
            (frozen_bound_snapshot_path, bound_code),
            (frozen_acquire_lock, acquire_code),
            (frozen_release_lock, release_code),
            (frozen_witness_path, witness_path_code),
            (frozen_call_witnessed, call_witnessed_code),
        )
        for function, expected_code in witnesses:
            if exact_type(function) is not function_type or function.__code__ is not expected_code:
                raise ValueError("canonical risk admission executable authority changed")

    def require_private(book: object) -> None:
        frozen_call_witnessed(
            require_opening,
            require_opening_witness,
            "opening-authority risk read",
            book,
        )
        frozen_call_witnessed(
            require_causal,
            require_causal_witness,
            "causal-authority risk read",
            book,
        )

    def call_spec(
        spec: tuple[object, ...],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ):
        code, name, defaults, kwdefaults, closure, global_bindings = spec
        delegate = function_type(
            code,
            dict(global_bindings),
            name=name,
            argdefs=defaults,
            closure=closure,
        )
        if kwdefaults is not None:
            delegate.__kwdefaults__ = dict(kwdefaults)
        return delegate(*args, **kwargs)

    require_executables_code = require_executables.__code__
    require_private_code = require_private.__code__
    call_spec_code = call_spec.__code__

    def admitted_call(
        book: object,
        spec: tuple[object, ...],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        failure_result=None,
    ):
        snapshot_path = None
        held_reads = None
        try:
            if (
                exact_type(require_executables) is not function_type
                or require_executables.__code__ is not require_executables_code
                or exact_type(require_private) is not function_type
                or require_private.__code__ is not require_private_code
                or exact_type(call_spec) is not function_type
                or call_spec.__code__ is not call_spec_code
            ):
                raise ValueError("canonical risk admission closure authority changed")
            require_executables()
            frozen_require_class_graph(
                canonical_paper_book,
                paperbook_class_witnesses,
            )
            frozen_require_class_graph(
                canonical_risk_policy,
                risk_class_witnesses,
            )
            snapshot_path = frozen_bound_snapshot_path(book)
            if snapshot_path is not None:
                held_reads = exact_getattr(risk_read_local, "held", None)
                if held_reads is None:
                    held_reads = {}
                    risk_read_local.held = held_reads
                entry = held_reads.get(snapshot_path)
                if entry is None:
                    publication_lock = frozen_acquire_lock(
                        frozen_witness_path(snapshot_path)
                    )
                    held_reads[snapshot_path] = [1, publication_lock]
                else:
                    entry[0] += 1

            frozen_validate_loaded_state(canonical_paper_book, book)
            require_private(book)
            result = call_spec(spec, args, kwargs)
            require_private(book)
            frozen_validate_loaded_state(canonical_paper_book, book)
            frozen_require_class_graph(
                canonical_risk_policy,
                risk_class_witnesses,
            )
            frozen_require_class_graph(
                canonical_paper_book,
                paperbook_class_witnesses,
            )
            require_executables()
            if (
                require_executables.__code__ is not require_executables_code
                or require_private.__code__ is not require_private_code
                or call_spec.__code__ is not call_spec_code
            ):
                raise ValueError("canonical risk admission closure authority changed")
            return result
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return failure_result
        finally:
            if snapshot_path is not None and held_reads is not None:
                entry = held_reads.get(snapshot_path)
                if entry is not None:
                    entry[0] -= 1
                    if entry[0] == 0:
                        _count, publication_lock = held_reads.pop(snapshot_path)
                        frozen_release_lock(publication_lock)

    return admitted_call


def _make_book_state(admit, spec):
    expected_admit = admit
    expected_code = admit.__code__

    def _book_state(cls, book):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return None
        return admit(book, spec, (cls, book), {})

    return _book_state


def _make_portfolio_hash(admit, spec):
    expected_admit = admit
    expected_code = admit.__code__

    def risk_of_ruin_portfolio_sha256(cls, book):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return None
        return admit(book, spec, (cls, book), {})

    return risk_of_ruin_portfolio_sha256


def _make_historical_metrics(admit, spec):
    expected_admit = admit
    expected_code = admit.__code__

    def _historical_risk_metrics(
        cls,
        book,
        *,
        realized_loss_window=None,
        causal_cutoff=None,
    ):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return None
        return admit(
            book,
            spec,
            (cls, book),
            {
                "realized_loss_window": realized_loss_window,
                "causal_cutoff": causal_cutoff,
            },
        )

    return _historical_risk_metrics


def _make_shadow_book(admit, spec):
    expected_admit = admit
    expected_code = admit.__code__

    def _shadow_book_for_allocation(book):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return None
        return admit(book, spec, (book,), {})

    return _shadow_book_for_allocation


def _make_identity_concentration(admit, spec, risk_decision):
    expected_admit = admit
    expected_code = admit.__code__

    def _identity_concentration_decision(
        cls,
        book,
        amount,
        context,
        *,
        dimension,
        limit,
    ):
        failure = risk_decision(
            False,
            f"owner {dimension} concentration evidence is invalid",
        )
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return failure
        return admit(
            book,
            spec,
            (cls, book, amount, context),
            {"dimension": dimension, "limit": limit},
            failure,
        )

    return _identity_concentration_decision


def _make_derive_goal_stake(admit, spec):
    expected_admit = admit
    expected_code = admit.__code__

    def derive_goal_stake(self, book, signal_strength, *, context=None):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return None
        return admit(
            book,
            spec,
            (self, book, signal_strength),
            {"context": context},
        )

    return derive_goal_stake


def _make_evaluate(admit, spec, risk_decision):
    expected_admit = admit
    expected_code = admit.__code__
    failure = risk_decision(
        False,
        "virtual bankroll private economic authority is invalid",
    )

    def evaluate(self, book, stake, *, context=None):
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return failure
        return admit(
            book,
            spec,
            (self, book, stake),
            {"context": context},
            failure,
        )

    return evaluate


def _make_derive_goal_stake_vector(
    admit,
    spec,
    stake_vector_decision,
    zero_decimal,
):
    expected_admit = admit
    expected_code = admit.__code__

    def derive_goal_stake_vector(
        self,
        book,
        signal_strengths,
        *,
        contexts,
        risk_of_ruin_vector_evidence=None,
    ):
        count = len(contexts) if type(contexts) is tuple else 0
        failure = stake_vector_decision(
            "WAIT",
            (zero_decimal,) * count,
            "virtual bankroll private economic authority is invalid",
        )
        if admit is not expected_admit or admit.__code__ is not expected_code:
            return failure
        return admit(
            book,
            spec,
            (self, book, signal_strengths),
            {
                "contexts": contexts,
                "risk_of_ruin_vector_evidence": risk_of_ruin_vector_evidence,
            },
            failure,
        )

    return derive_goal_stake_vector


def _install() -> None:
    global _BOOK_STATE_SPEC
    global _PORTFOLIO_HASH_SPEC
    global _HISTORICAL_METRICS_SPEC
    global _SHADOW_BOOK_SPEC
    global _IDENTITY_CONCENTRATION_SPEC
    global _DERIVE_GOAL_STAKE_SPEC
    global _DERIVE_GOAL_STAKE_VECTOR_SPEC
    global _EVALUATE_SPEC

    policy = _risk.PaperRiskPolicy
    book_state = _function_for_descriptor(policy, "_book_state", classmethod)
    portfolio_hash = _function_for_descriptor(
        policy, "risk_of_ruin_portfolio_sha256", classmethod
    )
    historical_metrics = _function_for_descriptor(
        policy, "_historical_risk_metrics", classmethod
    )
    shadow_book = _function_for_descriptor(
        policy, "_shadow_book_for_allocation", staticmethod
    )
    identity_concentration = _function_for_descriptor(
        policy, "_identity_concentration_decision", classmethod
    )
    derive_goal_stake = _plain_function(policy, "derive_goal_stake")
    derive_goal_stake_vector = _plain_function(policy, "derive_goal_stake_vector")
    evaluate = _plain_function(policy, "evaluate")

    generation_globals = book_state.__globals__
    required_function_names = (
        "_FROZEN_BOUND_SNAPSHOT_PATH",
        "_FROZEN_ACQUIRE_LOCK",
        "_FROZEN_RELEASE_LOCK",
        "_FROZEN_WITNESS_PATH",
        "_FROZEN_CALL_WITNESSED",
        "_FROZEN_REQUIRE_CLASS_GRAPH",
        "_FROZEN_VALIDATE_LOADED_STATE",
    )
    functions = tuple(generation_globals.get(name) for name in required_function_names)
    if any(type(value) is not FunctionType for value in functions):
        raise RuntimeError("canonical generation-stable risk helper graph is unavailable")
    (
        bound_snapshot_path,
        acquire_lock,
        release_lock,
        witness_path,
        call_witnessed,
        require_class_graph,
        validate_loaded_state,
    ) = functions
    risk_read_local = generation_globals.get("_RISK_READ_LOCAL")
    canonical_paper_book = generation_globals.get("_CANONICAL_PAPER_BOOK")
    canonical_risk_policy = generation_globals.get("_CANONICAL_RISK_POLICY")
    paperbook_class_witnesses = generation_globals.get("_PAPERBOOK_CLASS_WITNESSES")
    risk_class_witnesses = generation_globals.get("_RISK_POLICY_CLASS_WITNESSES")
    if (
        risk_read_local is None
        or canonical_paper_book is not _paper.PaperBook
        or canonical_risk_policy is not policy
        or type(paperbook_class_witnesses) is not tuple
        or type(risk_class_witnesses) is not tuple
    ):
        raise RuntimeError("canonical generation-stable risk witness graph is unavailable")

    require_opening = _paper._require_ticket_opening_authority
    require_causal = _paper._require_paperbook_causal_history_authority
    if type(require_opening) is not FunctionType or type(require_causal) is not FunctionType:
        raise RuntimeError("canonical PaperBook private economic authority is unavailable")
    opening_witness = _authority._capture_delegate_witness(
        require_opening,
        "opening-authority risk read",
    )
    causal_witness = _authority._capture_delegate_witness(
        require_causal,
        "causal-authority risk read",
    )

    specs = {
        "book_state": _delegate_spec(book_state),
        "portfolio_hash": _delegate_spec(portfolio_hash),
        "historical_metrics": _delegate_spec(historical_metrics),
        "shadow_book": _delegate_spec(shadow_book),
        "identity_concentration": _delegate_spec(identity_concentration),
        "derive_goal_stake": _delegate_spec(derive_goal_stake),
        "derive_goal_stake_vector": _delegate_spec(derive_goal_stake_vector),
        "evaluate": _delegate_spec(evaluate),
    }

    # Preserve inert compatibility evidence for exact adversarial probes. Installed
    # wrappers capture the specs below directly and never resolve these globals.
    _BOOK_STATE_SPEC = specs["book_state"]
    _PORTFOLIO_HASH_SPEC = specs["portfolio_hash"]
    _HISTORICAL_METRICS_SPEC = specs["historical_metrics"]
    _SHADOW_BOOK_SPEC = specs["shadow_book"]
    _IDENTITY_CONCENTRATION_SPEC = specs["identity_concentration"]
    _DERIVE_GOAL_STAKE_SPEC = specs["derive_goal_stake"]
    _DERIVE_GOAL_STAKE_VECTOR_SPEC = specs["derive_goal_stake_vector"]
    _EVALUATE_SPEC = specs["evaluate"]

    admit = _make_admission(
        canonical_paper_book=canonical_paper_book,
        canonical_risk_policy=canonical_risk_policy,
        paperbook_class_witnesses=paperbook_class_witnesses,
        risk_class_witnesses=risk_class_witnesses,
        frozen_require_class_graph=require_class_graph,
        frozen_validate_loaded_state=validate_loaded_state,
        frozen_bound_snapshot_path=bound_snapshot_path,
        frozen_acquire_lock=acquire_lock,
        frozen_release_lock=release_lock,
        frozen_witness_path=witness_path,
        risk_read_local=risk_read_local,
        frozen_call_witnessed=call_witnessed,
        require_opening=require_opening,
        require_opening_witness=opening_witness,
        require_causal=require_causal,
        require_causal_witness=causal_witness,
    )

    guarded_book_state = _make_book_state(admit, specs["book_state"])
    guarded_portfolio_hash = _make_portfolio_hash(admit, specs["portfolio_hash"])
    guarded_historical_metrics = _make_historical_metrics(
        admit, specs["historical_metrics"]
    )
    guarded_shadow_book = _make_shadow_book(admit, specs["shadow_book"])
    guarded_identity_concentration = _make_identity_concentration(
        admit,
        specs["identity_concentration"],
        _risk.RiskDecision,
    )
    guarded_derive_goal_stake = _make_derive_goal_stake(
        admit,
        specs["derive_goal_stake"],
    )
    guarded_derive_goal_stake_vector = _make_derive_goal_stake_vector(
        admit,
        specs["derive_goal_stake_vector"],
        _risk.StakeVectorDecision,
        _risk.Decimal("0"),
    )
    guarded_evaluate = _make_evaluate(
        admit,
        specs["evaluate"],
        _risk.RiskDecision,
    )

    guarded_book_state.__qualname__ = "PaperRiskPolicy._book_state"
    guarded_portfolio_hash.__qualname__ = "PaperRiskPolicy.risk_of_ruin_portfolio_sha256"
    guarded_historical_metrics.__qualname__ = "PaperRiskPolicy._historical_risk_metrics"
    guarded_shadow_book.__qualname__ = "PaperRiskPolicy._shadow_book_for_allocation"
    guarded_identity_concentration.__qualname__ = (
        "PaperRiskPolicy._identity_concentration_decision"
    )
    guarded_derive_goal_stake.__qualname__ = "PaperRiskPolicy.derive_goal_stake"
    guarded_derive_goal_stake_vector.__qualname__ = "PaperRiskPolicy.derive_goal_stake_vector"
    guarded_evaluate.__qualname__ = "PaperRiskPolicy.evaluate"

    policy._book_state = classmethod(guarded_book_state)
    policy.risk_of_ruin_portfolio_sha256 = classmethod(guarded_portfolio_hash)
    policy._historical_risk_metrics = classmethod(guarded_historical_metrics)
    policy._shadow_book_for_allocation = staticmethod(guarded_shadow_book)
    policy._identity_concentration_decision = classmethod(guarded_identity_concentration)
    policy.derive_goal_stake = guarded_derive_goal_stake
    policy.derive_goal_stake_vector = guarded_derive_goal_stake_vector
    policy.evaluate = guarded_evaluate


_install()
del _install
