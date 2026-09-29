from __future__ import annotations

from pathlib import Path

import pytest

import autosport.monotonic_authority_root_binding as root_binding


_MAX_RECEIPT_BYTES = 64 * 1024


def test_oversized_single_link_receipt_fails_before_json_decode(tmp_path: Path) -> None:
    path = tmp_path / "oversized-root-selection.json"
    with path.open("wb") as handle:
        handle.truncate(_MAX_RECEIPT_BYTES + 1)

    with pytest.raises(
        root_binding.AuthorityRootSelectionIntegrityError,
        match="exceeds bounded root-selection receipt size",
    ):
        root_binding._read_strict_object(
            path,
            expected_keys=frozenset({"schema"}),
            label="focused oversized receipt",
        )


def test_bounded_reader_preserves_small_strict_object(tmp_path: Path) -> None:
    path = tmp_path / "small-root-selection.json"
    path.write_text('{"schema":"focused"}\n', encoding="utf-8")

    assert root_binding._read_strict_object(
        path,
        expected_keys=frozenset({"schema"}),
        label="focused small receipt",
    ) == {"schema": "focused"}
