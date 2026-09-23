from __future__ import annotations

from decimal import Decimal
import json

import pytest

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


_PLACED_AT = "2026-09-23T01:00:00+00:00"
_SETTLED_AT = "2026-09-23T01:01:00+00:00"


def _leg(
    side: str | None,
    *,
    selection_id: str = "selection-1",
    odds: str = "2.5",
) -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        selection_id,
        Decimal(odds),
        sport="soccer",
        exchange_side=side,
    )


def _assert_open_state_unchanged(
    book: PaperBook,
    ticket_id: str,
    *,
    balance: Decimal,
    lifecycle: tuple[tuple[object, ...], ...],
) -> None:
    ticket = book.tickets[ticket_id]
    assert book.balance == balance
    assert ticket.status is TicketStatus.OPEN
    assert ticket.payout == Decimal("0")
    assert ticket.settled_at is None
    assert tuple(book._lifecycle) == lifecycle
    assert ticket_id not in book._settlement_times


def test_settle_rejects_post_open_stake_rewrite_before_payout() -> None:
    book = PaperBook("100")
    original = _leg("back", odds="2")
    ticket = book.open_ticket([original], "10", placed_at=_PLACED_AT)
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    ticket.stake = Decimal("20")

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.settle(
            ticket.ticket_id,
            {original.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_settle_rejects_post_open_back_odds_replacement_before_payout() -> None:
    book = PaperBook("100")
    original = _leg("back", odds="2")
    ticket = book.open_ticket([original], "10", placed_at=_PLACED_AT)
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    inflated = _leg("back", odds="100")
    assert inflated.quote_key == original.quote_key
    ticket.legs = (inflated,)

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.settle(
            ticket.ticket_id,
            {inflated.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_save_rejects_post_open_valid_back_odds_replacement(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back", odds="2")], "10", placed_at=_PLACED_AT)
    ticket.legs = (_leg("back", odds="100"),)

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.save(path)

    assert not path.exists()


def test_restart_reconstructs_opening_economic_witness(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    original = _leg("back", odds="2")
    book.open_ticket([original], "10", placed_at=_PLACED_AT)
    book.save(path)

    restored = PaperBook.load(path)
    ticket = next(iter(restored.tickets.values()))
    balance_before = restored.balance
    lifecycle_before = tuple(restored._lifecycle)
    ticket.stake = Decimal("20")

    with pytest.raises(ValueError, match="opening economic identity changed"):
        restored.settle(
            ticket.ticket_id,
            {original.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        restored,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_settle_revalidates_mutated_ticket_side_before_economic_mutation() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("back")],
        "10",
        placed_at=_PLACED_AT,
    )
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    lay = _leg("lay")
    ticket.legs = (lay,)

    with pytest.raises(ValueError, match="LAY materialization is unsupported"):
        book.settle(
            ticket.ticket_id,
            {lay.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_settle_rejects_lay_in_mutated_multi_leg_ticket_before_payout() -> None:
    book = PaperBook("100")
    first = _leg("back", selection_id="selection-1", odds="2")
    second = _leg("back", selection_id="selection-2", odds="3")
    ticket = book.open_ticket(
        [first, second],
        "10",
        placed_at=_PLACED_AT,
    )
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    lay_second = _leg("lay", selection_id="selection-2", odds="3")
    ticket.legs = (first, lay_second)

    with pytest.raises(ValueError, match="LAY materialization is unsupported"):
        book.settle(
            ticket.ticket_id,
            {first.quote_key, lay_second.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_load_rejects_preload_locked_odds_rebaseline(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("back", odds="2")], "10", placed_at=_PLACED_AT)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["legs"][0]["locked_odds"] = "100"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="opening economic|commitment|witness",
    ):
        PaperBook.load(path)


def test_load_rejects_preload_stake_and_balance_rebaseline(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("back", odds="2")], "10", placed_at=_PLACED_AT)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["stake"] = "20"
    payload["balance"] = "80"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="opening economic|commitment|witness",
    ):
        PaperBook.load(path)


def test_load_bytes_ticket_snapshot_is_read_only_without_path_authority(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority"),
    )
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    original = _leg("back", odds="2")
    book.open_ticket([original], "10", placed_at=_PLACED_AT)
    book.save(path)

    decoded = PaperBook.load_bytes(path.read_bytes())
    ticket = next(iter(decoded.tickets.values()))

    with pytest.raises(ValueError, match="lacks independent durable witness authority"):
        decoded.settle(
            ticket.ticket_id,
            {original.quote_key},
            settled_at=_SETTLED_AT,
        )
    with pytest.raises(ValueError, match="lacks independent durable witness authority"):
        decoded.open_ticket([_leg("back", selection_id="selection-2")], "1")
    with pytest.raises(ValueError, match="lacks independent durable witness authority"):
        decoded.save(tmp_path / "copy.json")


def test_path_load_rejects_whole_snapshot_rollback_behind_external_authority(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority"),
    )
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket(
        [_leg("back", selection_id="selection-1", odds="2")],
        "10",
        placed_at=_PLACED_AT,
    )
    book.save(path)
    first_generation = path.read_bytes()

    book.open_ticket(
        [_leg("back", selection_id="selection-2", odds="3")],
        "5",
        placed_at="2026-09-23T01:00:30+00:00",
    )
    book.save(path)

    path.write_bytes(first_generation)

    with pytest.raises(ValueError, match="independent durable opening witness"):
        PaperBook.load(path)


def test_failed_replace_keeps_last_good_snapshot_loadable_and_aborts_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority"),
    )
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    first = _leg("back", selection_id="selection-1", odds="2")
    first_ticket = book.open_ticket([first], "10", placed_at=_PLACED_AT)
    book.save(path)
    last_good = path.read_bytes()

    book.open_ticket(
        [_leg("back", selection_id="selection-2", odds="3")],
        "5",
        placed_at="2026-09-23T01:00:30+00:00",
    )

    import autosport.paper as paper_module

    original_replace = paper_module.os.replace

    def _fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(paper_module.os, "replace", _fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        book.save(path)
    monkeypatch.setattr(paper_module.os, "replace", original_replace)

    assert path.read_bytes() == last_good
    restored = PaperBook.load(path)
    assert tuple(restored.tickets) == (first_ticket.ticket_id,)
    assert restored.balance == Decimal("90")


def test_empty_current_schema_snapshot_cannot_bypass_external_witness(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority"),
    )
    path = tmp_path / "paper-book-empty.json"
    book = PaperBook("100")
    book.save(path)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("100")
    assert restored.tickets == {}

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 8
    assert payload["tickets"] == []
    assert payload["lifecycle"] == []
    payload["initial_bankroll"] = "1000"
    payload["balance"] = "1000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="snapshot bytes do not match independent durable opening witness",
    ):
        PaperBook.load(path)




def test_fresh_book_cannot_overwrite_existing_witnessed_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    authority_root = tmp_path.parent / f"{tmp_path.name}-authority-fresh-overwrite"
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(authority_root),
    )
    path = tmp_path / "paper-book.json"

    current = PaperBook("100")
    current.open_ticket(
        [_leg("back", odds="2")],
        "10",
        placed_at=_PLACED_AT,
    )
    current.save(path)
    expected_bytes = path.read_bytes()

    attacker_fresh = PaperBook("1000")
    with pytest.raises(
        ValueError,
        match="fresh PaperBook cannot overwrite an existing snapshot authority",
    ):
        attacker_fresh.save(path)

    assert path.read_bytes() == expected_bytes
    restored = PaperBook.load(path)
    assert restored.initial_bankroll == Decimal("100")
    assert restored.balance == Decimal("90")


def test_bound_book_rejects_snapshot_witness_root_drift_before_valid_old_save(
    tmp_path,
    monkeypatch,
) -> None:
    authority_root_1 = tmp_path.parent / f"{tmp_path.name}-authority-root-1"
    authority_root_2 = tmp_path.parent / f"{tmp_path.name}-authority-root-2"
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(authority_root_1),
    )
    path = tmp_path / "paper-book.json"

    current = PaperBook("100")
    current.open_ticket(
        [_leg("back", selection_id="selection-1", odds="2")],
        "10",
        placed_at=_PLACED_AT,
    )
    current.save(path)

    stale = PaperBook.load(path)

    current.open_ticket(
        [_leg("back", selection_id="selection-2", odds="3")],
        "5",
        placed_at="2026-09-23T01:00:30+00:00",
    )
    current.save(path)
    latest_bytes = path.read_bytes()

    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(authority_root_2),
    )
    with pytest.raises(
        ValueError,
        match="independent snapshot authority root changed after binding",
    ):
        stale.save(path)

    assert path.read_bytes() == latest_bytes

    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(authority_root_1),
    )
    restored = PaperBook.load(path)
    assert restored.balance == Decimal("85")
    assert len(restored.tickets) == 2


def test_load_bytes_cannot_mint_mutation_authority_by_flipping_public_field(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority-byte-flip"),
    )
    path = tmp_path / "paper-book.json"
    canonical = PaperBook("100")
    canonical.save(path)

    untrusted = PaperBook.load_bytes(path.read_bytes())
    # A caller-visible Python attribute must never be the authority boundary.
    untrusted._snapshot_authority_verified = True

    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks independent durable witness authority",
    ):
        untrusted.open_ticket(
            [_leg("back", odds="2")],
            "10",
            placed_at=_PLACED_AT,
        )


def test_bound_book_rejects_save_as_authority_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority-save-as"),
    )
    path = tmp_path / "paper-book.json"
    other_path = tmp_path / "paper-book-copy.json"

    canonical = PaperBook("100")
    canonical.save(path)
    loaded = PaperBook.load(path)

    with pytest.raises(
        ValueError,
        match="snapshot authority is bound to another path or witness root",
    ):
        loaded.save(other_path)

    assert not other_path.exists()



def test_empty_prewitness_legacy_snapshot_remains_read_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority-empty-legacy"),
    )
    path = tmp_path / "legacy-paper-book.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 7,
                "initial_bankroll": "100",
                "balance": "100",
                "tickets": [],
                "lifecycle": [],
            }
        ),
        encoding="utf-8",
    )

    legacy = PaperBook.load(path)
    assert legacy.balance == Decimal("100")
    assert legacy.tickets == {}

    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks independent durable witness authority",
    ):
        legacy.open_ticket(
            [_leg("back", odds="2")],
            "10",
            placed_at=_PLACED_AT,
        )

    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks independent durable witness authority",
    ):
        legacy.save(path)



def test_interrupted_first_save_cannot_rebootstrap_over_foreign_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-authority-first-save"),
    )
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")

    import autosport.paper as paper_module

    original_append = paper_module._append_snapshot_witness

    def _fail_before_prepare(*_args, **_kwargs):
        raise RuntimeError("injected witness publication failure")

    monkeypatch.setattr(
        paper_module,
        "_append_snapshot_witness",
        _fail_before_prepare,
    )
    with pytest.raises(RuntimeError, match="injected witness publication failure"):
        book.save(path)
    monkeypatch.setattr(
        paper_module,
        "_append_snapshot_witness",
        original_append,
    )

    assert not path.exists()
    foreign_bytes = b'{"foreign":"snapshot"}'
    path.write_bytes(foreign_bytes)

    with pytest.raises(
        ValueError,
        match="existing snapshot lacks independent durable witness",
    ):
        book.save(path)

    assert path.read_bytes() == foreign_bytes
