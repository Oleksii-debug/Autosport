from __future__ import annotations

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