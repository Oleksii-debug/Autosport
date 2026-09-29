from __future__ import annotations

import pytest

import autosport.data_tools_entry as entry


def test_expected_failure_type_rebinding_cannot_expand_main_catch_boundary(monkeypatch) -> None:
    class ProgrammingFailure(RuntimeError):
        pass

    def explode(_command: str, _forwarded: list[str]) -> int:
        raise ProgrammingFailure("must remain visible")

    monkeypatch.setattr(entry, "_dispatch", explode)
    monkeypatch.setattr(entry, "_EXPECTED_FAILURE_TYPES", (BaseException,))

    with pytest.raises(ProgrammingFailure, match="must remain visible"):
        entry.main(["verify-dataset", "dataset"])
