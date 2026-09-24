from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.product_decision_activation as activation


def test_product_activation_machine_root_is_frozen_after_module_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-import OS resolver drift must not retarget supported START authority."""

    if os.name == "nt":
        pytest.skip("POSIX resolver falsifier; Windows has an equivalent shell resolver boundary")

    import pwd

    canonical_root = activation._product_activation_authority_root()
    forged_home = Path("/tmp/autosport-forged-product-root")

    monkeypatch.setattr(
        pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(forged_home)),
    )

    assert activation._product_activation_authority_root() == canonical_root
