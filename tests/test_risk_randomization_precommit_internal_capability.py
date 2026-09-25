from __future__ import annotations

import inspect
from types import FunctionType

import autosport.risk_randomization_precommit as precommit


def _reachable_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    pending: list[object] = [root]
    seen: set[int] = set()
    found: list[FunctionType] = []
    while pending:
        value = pending.pop()
        if not isinstance(value, FunctionType) or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if value.__defaults__:
            pending.extend(value.__defaults__)
        if value.__kwdefaults__:
            pending.extend(value.__kwdefaults__.values())
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
    return tuple(found)


def test_randomization_entropy_injector_is_not_a_module_or_function_metadata_capability() -> None:
    """No issuer reachable through ordinary function metadata may accept caller entropy."""

    assert not hasattr(precommit, "_issue_risk_randomization_precommit")
    reachable = _reachable_functions(precommit.issue_risk_randomization_precommit)
    assert reachable
    for function in reachable:
        assert "_product_token_bytes" not in inspect.signature(function).parameters
        assert "_product_token_bytes" not in function.__code__.co_varnames
