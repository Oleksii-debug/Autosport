from __future__ import annotations

import builtins
from types import FunctionType

import autosport._run_transaction_current_binding_resolver_closure_guard as resolver_guard
from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    assert closure is not None
    assert name in freevars
    return closure[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def _resolver_entrypoint(method: FunctionType, name: str) -> FunctionType:
    inner_consumer = _closure_value(method, "function")
    assert isinstance(inner_consumer, FunctionType)
    entrypoint = _closure_value(inner_consumer, name)
    assert isinstance(entrypoint, FunctionType)
    return entrypoint


def _original_callable(entrypoint: FunctionType) -> FunctionType:
    if "original_callable" not in entrypoint.__code__.co_freevars:
        return entrypoint
    original = _closure_value(entrypoint, "original_callable")
    assert isinstance(original, FunctionType)
    return original


def test_current_binding_resolver_rejects_coordinated_closure_graph_retarget() -> None:
    """Resolver identity/code alone must not bless a replacement binding authority graph."""

    guarded = RunTransaction._stage_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    inner_consumer = _closure_value(guarded, "function")
    assert isinstance(inner_consumer, FunctionType)
    resolver_entrypoint = _closure_value(inner_consumer, "resolver")
    assert isinstance(resolver_entrypoint, FunctionType)

    resolver = resolver_entrypoint
    if "original_callable" in resolver_entrypoint.__code__.co_freevars:
        resolver = _closure_value(resolver_entrypoint, "original_callable")
        assert isinstance(resolver, FunctionType)

    names = (
        "trusted_globals",
        "frozen_load",
        "frozen_load_code",
        "frozen_load_globals",
        "frozen_globals",
        "verifier",
        "graph",
    )
    cells = {name: _closure_cell(resolver, name) for name in names}
    originals = {name: cell.cell_contents for name, cell in cells.items()}

    def hostile_verifier(*_args, **_kwargs) -> None:
        return None

    def load_template() -> None:
        return None

    fake_globals: dict[str, object] = {
        "__builtins__": __builtins__,
        "_require_bound_book": hostile_verifier,
    }
    fake_load = FunctionType(load_template.__code__, fake_globals, name="fake_load")

    cells["trusted_globals"].cell_contents = {"_FROZEN_LOAD": fake_load}
    cells["frozen_load"].cell_contents = fake_load
    cells["frozen_load_code"].cell_contents = fake_load.__code__
    cells["frozen_load_globals"].cell_contents = fake_globals
    cells["frozen_globals"].cell_contents = fake_globals
    cells["verifier"].cell_contents = hostile_verifier
    cells["graph"].cell_contents = ()
    try:
        try:
            resolved = resolver_entrypoint()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "current-binding resolver accepted a coordinated replacement closure graph "
                f"and returned {resolved!r}"
            )
    finally:
        for name, value in originals.items():
            cells[name].cell_contents = value

    for name, value in originals.items():
        assert cells[name].cell_contents is value


def test_current_binding_resolvers_reject_late_builtin_global_shadow_before_dispatch() -> None:
    """Late globals must not replace builtins used by the transitive resolver graph."""

    for method in (
        RunTransaction._stage_paper_book_snapshot,
        RunTransaction._promote_paper_book_snapshot,
    ):
        assert isinstance(method, FunctionType)
        entrypoint = _resolver_entrypoint(method, "resolver")
        original = _original_callable(entrypoint)
        globals_mapping = original.__globals__
        assert "zip" not in globals_mapping
        hostile_calls = 0

        def hostile_zip(*args):
            nonlocal hostile_calls
            hostile_calls += 1
            return builtins.zip(*args)

        globals_mapping["zip"] = hostile_zip
        try:
            try:
                entrypoint()
            except Exception:  # noqa: BLE001 - fail closed is the required behavior.
                pass
            else:
                raise AssertionError(
                    "current-binding resolver accepted a late global shadow of builtin zip"
                )
        finally:
            del globals_mapping["zip"]
        assert hostile_calls == 0


def test_persistence_resolvers_reject_late_builtin_global_shadow_before_dispatch() -> None:
    """Ephemeral persistence reconstruction must not execute a shadowed builtin dict."""

    for method in (
        RunTransaction._stage_paper_book_snapshot,
        RunTransaction._promote_paper_book_snapshot,
    ):
        assert isinstance(method, FunctionType)
        entrypoint = _resolver_entrypoint(method, "persistence_resolver")
        original = _original_callable(entrypoint)
        globals_mapping = original.__globals__
        assert "dict" not in globals_mapping
        hostile_calls = 0

        def hostile_dict(*args, **kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return builtins.dict(*args, **kwargs)

        globals_mapping["dict"] = hostile_dict
        try:
            try:
                entrypoint()
            except Exception:  # noqa: BLE001 - fail closed is the required behavior.
                pass
            else:
                raise AssertionError(
                    "persistence resolver accepted a late global shadow of builtin dict"
                )
        finally:
            del globals_mapping["dict"]
        assert hostile_calls == 0


def test_resolvers_reject_late_len_global_shadow_before_dispatch() -> None:
    """Both resolver families must reject a late global shadow of builtin len."""

    for method in (
        RunTransaction._stage_paper_book_snapshot,
        RunTransaction._promote_paper_book_snapshot,
    ):
        assert isinstance(method, FunctionType)
        for resolver_name in ("resolver", "persistence_resolver"):
            entrypoint = _resolver_entrypoint(method, resolver_name)
            original = _original_callable(entrypoint)
            globals_mapping = original.__globals__
            assert "len" not in globals_mapping
            hostile_calls = 0

            def hostile_len(value):
                nonlocal hostile_calls
                hostile_calls += 1
                return builtins.len(value)

            globals_mapping["len"] = hostile_len
            try:
                try:
                    entrypoint()
                except Exception:  # noqa: BLE001 - fail closed is required.
                    pass
                else:
                    raise AssertionError(
                        f"{resolver_name} accepted a late global shadow of builtin len"
                    )
            finally:
                del globals_mapping["len"]
            assert hostile_calls == 0


def test_resolver_wrapper_rejects_shadowed_failure_constructor_before_dispatch() -> None:
    """Fail-closed verification must not execute a late module-global ValueError hook."""

    entrypoint = _resolver_entrypoint(
        RunTransaction._stage_paper_book_snapshot,
        "resolver",
    )
    original = _original_callable(entrypoint)
    globals_mapping = original.__globals__
    assert "zip" not in globals_mapping
    assert "ValueError" not in resolver_guard.__dict__
    hostile_calls = 0

    def hostile_value_error(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.ValueError(*args, **kwargs)

    globals_mapping["zip"] = object()
    resolver_guard.ValueError = hostile_value_error
    try:
        try:
            entrypoint()
        except Exception:  # noqa: BLE001 - fail closed is required.
            pass
        else:
            raise AssertionError("resolver accepted mutated Python name-resolution authority")
    finally:
        del globals_mapping["zip"]
        del resolver_guard.ValueError

    assert hostile_calls == 0, "resolver wrapper dispatched a shadowed ValueError hook"


def test_resolver_guard_install_mutators_are_not_runtime_capabilities() -> None:
    """One-shot resolver composition helpers must disappear after package import."""

    assert not hasattr(resolver_guard, "_install")
    assert not hasattr(resolver_guard, "_seal_method")
    assert not hasattr(resolver_guard, "_sealed_callable")
    assert not hasattr(resolver_guard, "_capture_closure_graph")
    assert not hasattr(resolver_guard, "_closure_cell")