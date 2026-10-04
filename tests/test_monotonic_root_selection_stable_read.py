from __future__ import annotations

from pathlib import Path

import autosport.monotonic_authority_root_binding as root


def test_product_stable_reader_does_not_use_path_read_text(
    tmp_path, monkeypatch
) -> None:
    receipt = tmp_path / "selector.json"
    receipt.write_text('{"ok":true}\n', encoding="utf-8")
    attacker_called = False

    original_read_text = Path.read_text

    def hostile_read_text(self, *args, **kwargs):
        nonlocal attacker_called
        if self == receipt:
            attacker_called = True
            receipt.unlink()
            forged = tmp_path / "forged.json"
            forged.write_text('{"ok":false}\n', encoding="utf-8")
            receipt.symlink_to(forged)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", hostile_read_text)

    raw = root._read_strict_object(
        receipt,
        expected_keys=frozenset({"ok"}),
        label="authority-root binding",
    )

    assert raw == {"ok": True}
    assert attacker_called is False
    assert receipt.is_file()
    assert not receipt.is_symlink()
