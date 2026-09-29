from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


_VERIFIER_PATH = Path("scripts/verify_source_checkout.py")


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "autosport_verify_source_checkout_artifact_binding_test",
        _VERIFIER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bind_release_artifact_rejects_path_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    source = (tmp_path / "dist" / "Autosport.exe").absolute()
    replacement = (tmp_path / "replacement.exe").absolute()
    destination = (tmp_path / "bound" / "Autosport.exe").absolute()
    source.parent.mkdir(parents=True)
    source.write_bytes(b"trusted-pyinstaller-output")
    replacement.write_bytes(b"substituted-executable")

    real_open = Path.open
    replaced = False

    def replace_before_open(self: Path, *args, **kwargs):
        nonlocal replaced
        if self.absolute() == source and not replaced:
            os.replace(replacement, source)
            replaced = True
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)

    with pytest.raises(ValueError, match="changed before capture"):
        verifier.bind_release_artifact(source, destination)

    assert replaced is True
    assert not destination.exists()


def test_bind_release_artifact_copies_and_hashes_one_stable_file(tmp_path: Path) -> None:
    verifier = _load_verifier()
    source = tmp_path / "audit.json"
    destination = tmp_path / "bound" / "audit.json"
    payload = b'{"status":"PASS","real_money_execution":false}'
    source.write_bytes(payload)

    digest = verifier.bind_release_artifact(source, destination)

    assert destination.read_bytes() == payload
    assert len(digest) == 64
    verifier.require_artifact_sha256(destination, digest)
