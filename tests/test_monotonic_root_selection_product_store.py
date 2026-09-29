from __future__ import annotations

from pathlib import Path
from types import FunctionType

import pytest

import autosport.monotonic_authority_root_binding as root_binding
import autosport.monotonic_workspace_authority as workspace_authority
from autosport.monotonic_authority_root_binding import stable_root_selection_store


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
        if value.__defaults__:
            pending.extend(value.__defaults__)
        if value.__kwdefaults__:
            pending.extend(value.__kwdefaults__.values())
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
    return tuple(found)


def _implementation(root: FunctionType, name: str) -> FunctionType:
    matches = [
        function
        for function in _reachable_functions(root)
        if function is not root and function.__name__ == name
    ]
    assert len(matches) == 1, [function.__name__ for function in matches]
    return matches[0]


def test_product_root_selection_store_ignores_caller_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    baseline = stable_root_selection_store()
    assert baseline.is_absolute()

    attacker = tmp_path / "attacker-controlled-state"
    monkeypatch.setenv("LOCALAPPDATA", str(attacker / "localappdata"))
    monkeypatch.setenv("XDG_STATE_HOME", str(attacker / "xdg-state"))
    monkeypatch.setenv("HOME", str(attacker / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: attacker / "path-home"))

    assert stable_root_selection_store() == baseline
    assert not baseline.is_relative_to(attacker)


def test_product_root_selection_store_ignores_module_global_rebinding(monkeypatch) -> None:
    """Function identity alone must not leave mutable resolver globals authoritative."""

    baseline = root_binding.stable_root_selection_store()
    assert baseline.is_absolute()

    monkeypatch.setattr(root_binding, "os", object())
    monkeypatch.setattr(root_binding, "Path", object())

    assert root_binding.stable_root_selection_store() == baseline


def test_root_selection_sha256_attribute_rebinding_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Keeping hashlib module identity must not make its mutable sha256 authoritative."""

    attacker_called = False

    def forged_sha256(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("forged SHA-256 dispatch must not execute")

    monkeypatch.setattr(root_binding.hashlib, "sha256", forged_sha256)
    with pytest.raises(
        root_binding.AuthorityRootSelectionConfigurationError,
        match="root-selection SHA-256 dispatch was rebound",
    ):
        root_binding.preflight_authority_root_selection(
            workspace=tmp_path / "workspace",
            authority_root=tmp_path / "authority",
            requested_workspace_instance_id=None,
        )

    assert attacker_called is False


def test_product_store_fails_closed_if_frozen_clone_globals_are_mutated() -> None:
    public = root_binding.stable_root_selection_store
    implementation = _implementation(public, "_sealed_stable_root_selection_store")
    original_os = implementation.__globals__["_os"]
    implementation.__globals__["_os"] = object()
    try:
        with pytest.raises(
            root_binding.AuthorityRootSelectionConfigurationError,
            match=r"frozen root-selection store global '_os' was rebound",
        ):
            public()
    finally:
        implementation.__globals__["_os"] = original_os


def test_product_store_fails_closed_if_frozen_clone_code_is_mutated() -> None:
    public = root_binding.stable_root_selection_store
    implementation = _implementation(public, "_sealed_stable_root_selection_store")
    original_code = implementation.__code__

    def forged_store():
        raise AssertionError("forged root-selection implementation must not execute")

    implementation.__code__ = forged_store.__code__
    try:
        with pytest.raises(
            root_binding.AuthorityRootSelectionConfigurationError,
            match=r"frozen root-selection store executable code was rebound",
        ):
            public()
    finally:
        implementation.__code__ = original_code


def test_preflight_fails_closed_if_frozen_context_dispatch_is_mutated(tmp_path: Path) -> None:
    public = root_binding.preflight_authority_root_selection
    implementation = _implementation(public, "preflight_authority_root_selection")
    original_context = implementation.__globals__["_selection_context"]
    implementation.__globals__["_selection_context"] = lambda **_kwargs: object()
    try:
        with pytest.raises(
            root_binding.AuthorityRootSelectionConfigurationError,
            match=r"frozen root-selection preflight global '_selection_context' was rebound",
        ):
            public(
                workspace=tmp_path / "workspace",
                authority_root=tmp_path / "authority",
                requested_workspace_instance_id=None,
            )
    finally:
        implementation.__globals__["_selection_context"] = original_context


def test_binding_resolve_fails_closed_if_frozen_context_dispatch_is_mutated(
    tmp_path: Path,
) -> None:
    public = root_binding.AuthorityRootSelectionBinding.resolve.__func__
    implementation = _implementation(public, "resolve")
    original_context = implementation.__globals__["_selection_context"]
    implementation.__globals__["_selection_context"] = lambda **_kwargs: object()
    try:
        with pytest.raises(
            root_binding.AuthorityRootSelectionConfigurationError,
            match=r"frozen root-selection binding resolve global '_selection_context' was rebound",
        ):
            root_binding.AuthorityRootSelectionBinding.resolve(
                workspace=tmp_path / "workspace",
                workspace_instance_id="workspace-instance",
                authority_root=tmp_path / "authority",
            )
    finally:
        implementation.__globals__["_selection_context"] = original_context


def test_workspace_authority_constructor_fails_closed_if_frozen_preflight_is_mutated(
    tmp_path: Path,
) -> None:
    public = workspace_authority.MonotonicWorkspaceAuthority.__init__
    implementation = _implementation(public, "__init__")
    original_preflight = implementation.__globals__["preflight_authority_root_selection"]
    implementation.__globals__["preflight_authority_root_selection"] = (
        lambda **_kwargs: None
    )
    try:
        with pytest.raises(
            root_binding.AuthorityRootSelectionConfigurationError,
            match=r"frozen monotonic workspace authority constructor global "
            r"'preflight_authority_root_selection' was rebound",
        ):
            workspace_authority.MonotonicWorkspaceAuthority(
                workspace=tmp_path / "workspace",
                domain="test-domain",
                key="test-key",
                authority_root=tmp_path / "authority",
            )
    finally:
        implementation.__globals__["preflight_authority_root_selection"] = original_preflight
