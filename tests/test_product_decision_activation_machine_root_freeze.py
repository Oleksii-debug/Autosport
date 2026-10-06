from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.product_decision_activation as activation


def test_product_activation_machine_root_is_frozen_after_module_composition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Post-import OS resolver drift must not retarget supported START authority."""

    if os.name == "nt":
        pytest.skip("POSIX resolver falsifier; Windows has an equivalent shell resolver boundary")

    import pwd

    before = activation.ProductDecisionActivationStore(tmp_path / "before")
    canonical_root = before._authority.authority_root
    forged_home = tmp_path / "forged-home"

    monkeypatch.setattr(
        pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(forged_home)),
    )

    # The raw OS helper is not itself positive START authority after module sealing.
    # A newly constructed canonical store must continue to use the import-time root
    # captured by the sealed constructor closure, not the caller-retargeted resolver.
    after = activation.ProductDecisionActivationStore(tmp_path / "after")
    assert after._authority.authority_root == canonical_root
    assert forged_home not in canonical_root.parents
