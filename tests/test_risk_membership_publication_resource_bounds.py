from __future__ import annotations

from pathlib import Path

import pytest

from autosport.risk_membership_publication import (
    RiskMembershipPublicationError,
    _MAX_STATE_BYTES,
    _read_stable_state_bytes,
)


def test_oversized_regular_state_is_rejected_before_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "oversized-membership-state.json"
    with state_path.open("wb") as handle:
        handle.truncate(_MAX_STATE_BYTES + 1)

    open_attempted = False

    def fail_open(self: Path, *args: object, **kwargs: object):
        nonlocal open_attempted
        open_attempted = True
        raise AssertionError("oversized state must fail before file open")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(
        RiskMembershipPublicationError,
        match="publication state exceeds bounded size",
    ):
        _read_stable_state_bytes(state_path)

    assert open_attempted is False


def test_stable_reader_uses_explicit_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "small-membership-state.json"
    expected = b'{"schema":"focused-regression"}\n'
    state_path.write_bytes(expected)

    real_handle = state_path.open("rb")
    read_sizes: list[int] = []

    class ReadProbe:
        def __enter__(self) -> "ReadProbe":
            return self

        def __exit__(self, *_args: object) -> None:
            real_handle.close()

        def fileno(self) -> int:
            return real_handle.fileno()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return real_handle.read(size)

    def probed_open(self: Path, *args: object, **kwargs: object) -> ReadProbe:
        assert self == state_path
        assert args == ("rb",)
        assert kwargs == {}
        return ReadProbe()

    monkeypatch.setattr(Path, "open", probed_open)

    assert _read_stable_state_bytes(state_path) == expected
    assert read_sizes == [_MAX_STATE_BYTES + 1]
