from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T13:50:00+00:00"


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def _saved_book(tmp_path: Path) -> tuple[Path, Path]:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket(
        [
            TicketLeg(
                "event-witness-scalars",
                "market-witness-scalars",
                "selection-witness-scalars",
                Decimal("2.5"),
                sport="soccer",
                exchange_side="back",
            )
        ],
        "10",
        placed_at=_TS,
    )
    book.save(path)
    return path, guard._witness_path(path)


def _rewrite_valid_chain(witness: Path, mutate) -> None:
    records = [json.loads(line) for line in witness.read_text(encoding="utf-8").splitlines()]
    previous: str | None = None
    rewritten: list[str] = []
    for index, record in enumerate(records, start=1):
        mutate(index, record)
        record["previous_witness_sha256"] = previous
        body = {key: value for key, value in record.items() if key != "witness_sha256"}
        record["witness_sha256"] = guard._digest_record(body)
        previous = record["witness_sha256"]
        rewritten.append(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    witness.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_witness_schema_version_rejects_json_boolean_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness = _saved_book(tmp_path)

    _rewrite_valid_chain(
        witness,
        lambda _index, record: record.__setitem__("witness_schema_version", True),
    )

    with pytest.raises(ValueError, match="schema"):
        PaperBook.load(path)


def test_witness_sequence_rejects_json_float_integer_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness = _saved_book(tmp_path)

    _rewrite_valid_chain(
        witness,
        lambda index, record: record.__setitem__("sequence", float(index)),
    )

    with pytest.raises(ValueError, match="sequence"):
        PaperBook.load(path)


def test_witness_schema_exact_type_ignores_self_restoring_builtin_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness = _saved_book(tmp_path)
    _rewrite_valid_chain(
        witness,
        lambda _index, record: record.__setitem__("witness_schema_version", True),
    )

    load_descriptor = vars(PaperBook)["load"]
    assert type(load_descriptor) is classmethod
    guarded_load = load_descriptor.__func__
    builtins_map = guarded_load.__builtins__
    original_type = builtins_map["type"]
    bypass_attempted = False

    def hostile_type(value):
        nonlocal bypass_attempted
        if value is True:
            bypass_attempted = True
            builtins_map["type"] = original_type
            return int
        return original_type(value)

    builtins_map["type"] = hostile_type
    try:
        with pytest.raises(ValueError, match="schema"):
            PaperBook.load(path)
    finally:
        builtins_map["type"] = original_type

    assert bypass_attempted is False
