from __future__ import annotations

from types import FunctionType

import pytest

import autosport.research_multiplicity_family_close as close_module


def test_family_close_rejects_evidence_class_rebind_before_forged_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    class ForgedCloseEvidence:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal forged_called
            forged_called = True

    monkeypatch.setattr(
        close_module,
        "MultiplicityFamilyCloseEvidence",
        ForgedCloseEvidence,
    )

    with pytest.raises(
        close_module.MultiplicityFamilyCloseError,
        match="public authority changed",
    ):
        close_module.derive_multiplicity_family_close(None)

    assert forged_called is False


def test_family_close_rejects_digest_rebind_before_forged_hasher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    def forged_digest(*args, **kwargs) -> str:
        nonlocal forged_called
        forged_called = True
        return "0" * 64

    monkeypatch.setattr(close_module, "_digest", forged_digest)

    with pytest.raises(
        close_module.MultiplicityFamilyCloseError,
        match="dependency '_digest' changed",
    ):
        close_module.derive_multiplicity_family_close(None)

    assert forged_called is False


def test_family_close_rejects_lock_rebind_before_forged_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    class ForgedLock:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal forged_called
            forged_called = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(close_module, "WorkspaceEconomicLock", ForgedLock)

    with pytest.raises(
        close_module.MultiplicityFamilyCloseError,
        match="dependency 'WorkspaceEconomicLock' changed",
    ):
        close_module.derive_multiplicity_family_close(None)

    assert forged_called is False


def test_family_close_rejects_store_constructor_rebind_before_forged_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False
    store_type = close_module._CANONICAL_STORE_TYPE

    def forged_init(self, path, *, workspace_root=None) -> None:
        nonlocal forged_called
        forged_called = True
        self.path = path
        self.workspace_root = workspace_root

    monkeypatch.setattr(store_type, "__init__", forged_init)

    with pytest.raises(
        close_module.MultiplicityFamilyCloseError,
        match="store constructor dispatch changed",
    ):
        close_module.derive_multiplicity_family_close(None)

    assert forged_called is False


def _reachable_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    seen: set[int] = set()
    found: list[FunctionType] = []
    pending: list[object] = [root]
    while pending:
        value = pending.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(value, FunctionType):
            found.append(value)
            for cell in value.__closure__ or ():
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
            pending.extend(value.__defaults__ or ())
            if value.__kwdefaults__:
                pending.extend(value.__kwdefaults__.values())
            wrapped = getattr(value, "__wrapped__", None)
            if wrapped is not None:
                pending.append(wrapped)
            continue
        if type(value) in (tuple, list, set, frozenset):
            pending.extend(value)
            continue
        if type(value) is dict:
            pending.extend(value.keys())
            pending.extend(value.values())
    return tuple(found)


@pytest.mark.parametrize(
    "entry_name",
    (
        "derive_multiplicity_family_close",
        "require_current_multiplicity_family_close",
    ),
)
def test_family_close_public_metadata_exposes_no_unguarded_predecessor(
    entry_name: str,
) -> None:
    public = getattr(close_module, entry_name)
    reachable = _reachable_functions(public)
    predecessors = [
        function
        for function in reachable
        if function is not public
        and getattr(function.__code__, "co_name", None) == entry_name
    ]

    assert predecessors == []


def test_family_close_rejects_derive_helper_rebind_before_forged_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    def forged_derive_locked(*args, **kwargs):
        nonlocal forged_called
        forged_called = True
        raise AssertionError("forged derive helper must not execute")

    monkeypatch.setattr(close_module, "_derive_locked", forged_derive_locked)

    with pytest.raises(
        close_module.MultiplicityFamilyCloseError,
        match="dependency '_derive_locked' changed",
    ):
        close_module.derive_multiplicity_family_close(None)

    assert forged_called is False

