from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def test_owner_facing_evaluate_root_cannot_be_replaced() -> None:
    """Post-import class replacement must not become positive risk authority."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    original = vars(PaperRiskPolicy)["evaluate"]
    hostile_executed = False

    def hostile_evaluate(self, candidate_book, stake, *, context=None):
        nonlocal hostile_executed
        del self, candidate_book, stake, context
        hostile_executed = True
        raise AssertionError("replaced risk root must never execute")

    replacement_rejected = False
    try:
        try:
            setattr(PaperRiskPolicy, "evaluate", hostile_evaluate)
        except TypeError:
            replacement_rejected = True

        if not replacement_rejected:
            try:
                decision = policy.evaluate(book, Decimal("10"))
            except (TypeError, ValueError):
                decision = None
            assert hostile_executed is False
            if decision is not None:
                assert decision.allowed is False
    finally:
        if vars(PaperRiskPolicy).get("evaluate") is not original:
            type.__setattr__(PaperRiskPolicy, "evaluate", original)


@pytest.mark.parametrize(
    "name",
    ("provenance_payload", "provenance_record"),
)
def test_policy_provenance_roots_cannot_be_replaced_or_deleted(name: str) -> None:
    original = vars(PaperRiskPolicy)[name]

    def hostile(self):
        del self
        return {"spoofed": True}

    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        setattr(PaperRiskPolicy, name, hostile)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__setattr__(PaperRiskPolicy, name, hostile)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        delattr(PaperRiskPolicy, name)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__delattr__(PaperRiskPolicy, name)

    assert vars(PaperRiskPolicy)[name] is original


def _hostile_provenance_code_with_matching_closure(function):
    closure_count = len(function.__closure__ or ())
    lines = ["def build():"]
    for index in range(closure_count):
        lines.append(f"    cell_{index} = object()")
    lines.append("    def hostile(self):")
    if closure_count:
        names = ", ".join(f"cell_{index}" for index in range(closure_count))
        lines.append(f"        _ = ({names},)")
    else:
        lines.append("        _ = None")
    lines.append("        del self, _")
    lines.append("        return {'spoofed': True}")
    lines.append("    return hostile")
    namespace: dict[str, object] = {}
    exec("\n".join(lines), {}, namespace)
    hostile = namespace["build"]()
    assert callable(hostile)
    assert len(hostile.__closure__ or ()) == closure_count
    return hostile.__code__


@pytest.mark.parametrize(
    "name",
    ("provenance_payload", "provenance_record"),
)
def test_retained_policy_provenance_root_rechecks_in_place_code(name: str) -> None:
    policy = PaperRiskPolicy()
    retained = getattr(policy, name)
    root = vars(PaperRiskPolicy)[name]
    original_code = root.__code__
    hostile_code = _hostile_provenance_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        with pytest.raises(
            TypeError,
            match=f"canonical PaperRiskPolicy executable root changed: {name}",
        ):
            retained()
    finally:
        root.__code__ = original_code


def test_owner_facing_derive_root_cannot_be_deleted() -> None:
    """The canonical sizing entry cannot be removed after authority composition."""

    original = vars(PaperRiskPolicy)["derive_goal_stake"]
    deletion_rejected = False
    try:
        try:
            delattr(PaperRiskPolicy, "derive_goal_stake")
        except TypeError:
            deletion_rejected = True

        if not deletion_rejected:
            assert "derive_goal_stake" in vars(PaperRiskPolicy)
    finally:
        if vars(PaperRiskPolicy).get("derive_goal_stake") is not original:
            type.__setattr__(PaperRiskPolicy, "derive_goal_stake", original)


def test_root_dispatch_seal_preserves_canonical_policy_class_identity() -> None:
    """Authority sealing must not publish a facade subclass as a second policy class."""

    assert PaperRiskPolicy.__bases__ == (object,)


def _hostile_root_code_with_matching_closure(function):
    closure_count = len(function.__closure__ or ())
    lines = ["def build():"]
    for index in range(closure_count):
        lines.append(f"    cell_{index} = object()")
    lines.append("    def hostile(self, book, stake, *, context=None):")
    if closure_count:
        names = ", ".join(f"cell_{index}" for index in range(closure_count))
        lines.append(f"        _ = ({names},)")
    else:
        lines.append("        _ = None")
    lines.append("        del self, book, stake, context, _")
    lines.append("        return 'HOSTILE_RISK_ROOT'")
    lines.append("    return hostile")
    namespace: dict[str, object] = {}
    exec("\n".join(lines), {}, namespace)
    hostile = namespace["build"]()
    assert callable(hostile)
    assert len(hostile.__closure__ or ()) == closure_count
    return hostile.__code__


def _hostile_instance_dispatch(self, name):
    del self, name
    return lambda *args, **kwargs: "HOSTILE_INSTANCE_DISPATCH"


def _hostile_guard_function(*args, **kwargs):
    del args, kwargs
    return "HOSTILE_GUARD"


def test_instance_getattribute_root_rejects_in_place_code_mutation() -> None:
    """Special-method dispatch must reject mutation before hostile code can run."""

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    book = PaperBook("100")
    root = vars(PaperRiskPolicy)["__getattribute__"]
    original_code = root.__code__

    assert root.__closure__ is None
    try:
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy instance dispatch executable is sealed",
        ):
            root.__code__ = _hostile_instance_dispatch.__code__
        assert root.__code__ is original_code
        decision = policy.evaluate(book, Decimal("1"))
        assert decision.allowed is True
    finally:
        if root.__code__ is not original_code:
            root.__code__ = original_code


def test_instance_getattribute_root_cannot_be_replaced_or_deleted_via_base_type_api() -> None:
    """The special-method class slot stays sealed without owning metaclass dispatch."""

    original = vars(PaperRiskPolicy)["__getattribute__"]
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    book = PaperBook("100")

    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed: __getattribute__"):
        type.__setattr__(PaperRiskPolicy, "__getattribute__", _hostile_instance_dispatch)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed: __getattribute__"):
        type.__delattr__(PaperRiskPolicy, "__getattribute__")

    assert vars(PaperRiskPolicy)["__getattribute__"] is original
    decision = policy.evaluate(book, Decimal("1"))
    assert decision.allowed is True


def test_instance_getattribute_defaults_are_sealed_before_retarget() -> None:
    """Immutable authority inputs must not be replaceable on the special-method root."""

    root = vars(PaperRiskPolicy)["__getattribute__"]
    original_defaults = root.__defaults__
    assert original_defaults is not None

    replacement = original_defaults + (object(),)
    try:
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy instance dispatch executable is sealed",
        ):
            root.__defaults__ = replacement
        assert root.__defaults__ is original_defaults
    finally:
        if root.__defaults__ is not original_defaults:
            root.__defaults__ = original_defaults


def test_reachable_instance_guard_helpers_reject_code_retarget() -> None:
    """Traversing root defaults must not expose a mutable verifier/executor bearer."""

    root = vars(PaperRiskPolicy)["__getattribute__"]
    defaults = root.__defaults__
    assert defaults is not None
    validator = defaults[1]
    guarded_call = defaults[2]

    for helper in (validator, guarded_call):
        assert callable(helper)
        assert helper.__closure__ is None
        original_code = helper.__code__
        try:
            with pytest.raises(
                TypeError,
                match="canonical PaperRiskPolicy instance dispatch executable is sealed",
            ):
                helper.__code__ = _hostile_guard_function.__code__
            assert helper.__code__ is original_code
        finally:
            if helper.__code__ is not original_code:
                helper.__code__ = original_code


def test_instance_evaluate_rejects_in_place_root_code_mutation() -> None:
    """Normal instance lookup must not bypass the metaclass root seal."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    root = vars(PaperRiskPolicy)["evaluate"]
    original_code = root.__code__
    hostile_code = _hostile_root_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable root changed: evaluate",
        ):
            policy.evaluate(book, Decimal("1"))
    finally:
        root.__code__ = original_code


def test_retained_instance_evaluate_rechecks_root_code_before_call() -> None:
    """A bound risk call obtained before mutation must still fail closed."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    retained = policy.evaluate
    root = vars(PaperRiskPolicy)["evaluate"]
    original_code = root.__code__
    hostile_code = _hostile_root_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable root changed: evaluate",
        ):
            retained(book, Decimal("1"))
    finally:
        root.__code__ = original_code


def test_instance_evaluate_rejects_referenced_global_binding_mutation() -> None:
    """A mutable function globals mapping must not retarget decision authority."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    root = vars(PaperRiskPolicy)["evaluate"]
    globals_mapping = root.__globals__
    assert "RiskDecision" not in globals_mapping
    decision = policy.evaluate(book, Decimal("-1"))
    assert decision.allowed is False


def test_retained_instance_evaluate_rejects_referenced_global_binding_mutation() -> None:
    """A retained callable must keep frozen global bindings after lookup."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    retained = policy.evaluate
    root = vars(PaperRiskPolicy)["evaluate"]
    globals_mapping = root.__globals__
    assert "RiskDecision" not in globals_mapping
    decision = retained(book, Decimal("-1"))
    assert decision.allowed is False


def test_owner_facing_root_cannot_be_replaced_via_base_type_api() -> None:
    """Metaclass data descriptors must close explicit type.__setattr__ bypass."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    original = vars(PaperRiskPolicy)["evaluate"]
    hostile_executed = False

    def hostile_evaluate(self, candidate_book, stake, *, context=None):
        nonlocal hostile_executed
        del self, candidate_book, stake, context
        hostile_executed = True
        raise AssertionError("base-metaclass root replacement must never execute")

    try:
        replacement_rejected = False
        try:
            type.__setattr__(PaperRiskPolicy, "evaluate", hostile_evaluate)
        except TypeError:
            replacement_rejected = True

        if not replacement_rejected:
            try:
                decision = policy.evaluate(book, Decimal("10"))
            except (TypeError, ValueError):
                decision = None
            assert hostile_executed is False
            if decision is not None:
                assert decision.allowed is False
    finally:
        if vars(PaperRiskPolicy).get("evaluate") is not original:
            type.__setattr__(PaperRiskPolicy, "evaluate", original)


def test_owner_facing_root_cannot_be_deleted_via_base_type_api() -> None:
    """Metaclass data descriptors must close explicit type.__delattr__ bypass."""

    original = vars(PaperRiskPolicy)["derive_goal_stake_vector"]
    deletion_rejected = False
    try:
        try:
            type.__delattr__(PaperRiskPolicy, "derive_goal_stake_vector")
        except TypeError:
            deletion_rejected = True

        if not deletion_rejected:
            assert "derive_goal_stake_vector" in vars(PaperRiskPolicy)
    finally:
        if vars(PaperRiskPolicy).get("derive_goal_stake_vector") is not original:
            type.__setattr__(PaperRiskPolicy, "derive_goal_stake_vector", original)


def test_sealed_policy_cannot_be_subclassed_into_alternate_root_authority() -> None:
    """A subclass override must not become an alternate positive risk-policy authority."""

    subclass_rejected = False
    try:
        class HostilePaperRiskPolicy(PaperRiskPolicy):
            def evaluate(self, book, stake, *, context=None):
                del self, book, stake, context
                return None
    except TypeError:
        subclass_rejected = True

    assert subclass_rejected is True


def test_root_seal_preserves_dataclass_replace_and_instance_dispatch() -> None:
    """Root sealing must not break canonical dataclass cloning used by vector allocation."""

    original = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.50"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    cloned = replace(original, max_ticket_fraction=Decimal("0.20"))
    book = PaperBook("100")

    assert type(cloned) is PaperRiskPolicy
    assert cloned.max_ticket_fraction == Decimal("0.20")
    decision = cloned.evaluate(book, Decimal("1"))
    assert decision.allowed is True


def _hostile_book_state_code_with_matching_closure(function):
    closure_count = len(function.__closure__ or ())
    lines = ["def build():"]
    for index in range(closure_count):
        lines.append(f"    cell_{index} = object()")
    lines.append("    def hostile(cls, book):")
    if closure_count:
        names = ", ".join(f"cell_{index}" for index in range(closure_count))
        lines.append(f"        _ = ({names},)")
    else:
        lines.append("        _ = None")
    lines.append("        del cls, book, _")
    lines.append("        return ('HOSTILE', 'HOSTILE', 'HOSTILE', 0)")
    lines.append("    return hostile")
    namespace: dict[str, object] = {}
    exec("\n".join(lines), {}, namespace)
    hostile = namespace["build"]()
    assert callable(hostile)
    assert len(hostile.__closure__ or ()) == closure_count
    return hostile.__code__


def test_instance_book_state_rejects_in_place_code_mutation() -> None:
    """Instance lookup must enforce the existing transitive root seal."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    descriptor = vars(PaperRiskPolicy)["_book_state"]
    assert type(descriptor) is classmethod
    root = descriptor.__func__
    original_code = root.__code__
    hostile_code = _hostile_book_state_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable root changed: _book_state",
        ):
            policy._book_state(book)
    finally:
        root.__code__ = original_code


def test_instance_evaluate_cannot_consume_mutated_book_state_root() -> None:
    """Positive risk authority must not dispatch through mutated _book_state code."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    descriptor = vars(PaperRiskPolicy)["_book_state"]
    root = descriptor.__func__
    original_code = root.__code__
    hostile_code = _hostile_book_state_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        decision = policy.evaluate(book, Decimal("1"))
        assert decision.allowed is False
        assert decision.reason == "virtual bankroll generation authority is invalid"
    finally:
        root.__code__ = original_code


def test_retained_instance_book_state_rechecks_code_before_call() -> None:
    """A retained transitive root must not survive executable mutation."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    retained = policy._book_state
    descriptor = vars(PaperRiskPolicy)["_book_state"]
    root = descriptor.__func__
    original_code = root.__code__
    hostile_code = _hostile_book_state_code_with_matching_closure(root)

    try:
        root.__code__ = hostile_code
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable root changed: _book_state",
        ):
            retained(book)
    finally:
        root.__code__ = original_code


def test_retained_book_state_rejects_closure_cell_retarget() -> None:
    """Retained transitive authority must revalidate exact closure cell contents."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    retained = policy._book_state
    descriptor = vars(PaperRiskPolicy)["_book_state"]
    root = descriptor.__func__
    closure = root.__closure__
    assert closure
    cell = closure[0]
    original = cell.cell_contents

    try:
        cell.cell_contents = object()
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable root changed: _book_state",
        ):
            retained(book)
    finally:
        cell.cell_contents = original


def test_retained_decimal_context_rejects_global_binding_retarget() -> None:
    """Protected static helpers must not execute through rebound module globals."""

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    retained = policy._decimal_context
    descriptor = vars(PaperRiskPolicy)["_decimal_context"]
    assert type(descriptor) is staticmethod
    root = descriptor.__func__
    globals_mapping = root.__globals__
    original_context = globals_mapping["Context"]
    hostile_executed = False

    def hostile_context(*args, **kwargs):
        nonlocal hostile_executed
        del args, kwargs
        hostile_executed = True
        return original_context()

    try:
        globals_mapping["Context"] = hostile_context
        with pytest.raises(
            TypeError,
            match="canonical PaperRiskPolicy executable global changed: _decimal_context:Context",
        ):
            retained()
        assert hostile_executed is False
    finally:
        globals_mapping["Context"] = original_context


def test_book_state_root_cannot_be_retargeted_or_deleted() -> None:
    """Reconstructed delegates must not bypass admission by replacing _book_state."""

    original = vars(PaperRiskPolicy)["_book_state"]
    assert type(original) is classmethod
    hostile_executed = False

    def hostile_book_state(cls, book):
        nonlocal hostile_executed
        del cls, book
        hostile_executed = True
        return (Decimal("100"), Decimal("100"), Decimal("0"), 0)

    hostile_descriptor = classmethod(hostile_book_state)

    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        setattr(PaperRiskPolicy, "_book_state", hostile_descriptor)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__setattr__(PaperRiskPolicy, "_book_state", hostile_descriptor)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        delattr(PaperRiskPolicy, "_book_state")
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__delattr__(PaperRiskPolicy, "_book_state")

    assert vars(PaperRiskPolicy)["_book_state"] is original
    resolved = PaperRiskPolicy._book_state
    assert resolved.__func__ is original.__func__
    assert resolved.__self__ is PaperRiskPolicy
    assert hostile_executed is False
