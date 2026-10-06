from __future__ import annotations

from types import FunctionType

from autosport.betfair_commission_applicability import (
    assess_betfair_commission_applicability,
)


def _reachable_python_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    pending = [root]
    seen: set[int] = set()
    functions: list[FunctionType] = []

    while pending:
        function = pending.pop()
        function_id = id(function)
        if function_id in seen:
            continue
        seen.add(function_id)
        functions.append(function)

        for cell in function.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is FunctionType:
                pending.append(value)

    return tuple(functions)


def test_public_assessment_ingress_exposes_no_raw_observation_issuer_callable() -> None:
    reachable = _reachable_python_functions(assess_betfair_commission_applicability)

    assert all(function.__name__ != "issue" for function in reachable)
    assert all(function.__code__.co_name != "issue" for function in reachable)
