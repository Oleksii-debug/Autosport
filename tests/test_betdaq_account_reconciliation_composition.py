from datetime import datetime, timezone
from decimal import Decimal

import autosport.betdaq_account_readonly as betdaq_account_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.bookmaker_account_reconciliation import (
    BookmakerAccountReconciliationStore,
)
from autosport.bookmaker_capability import BookmakerCapability


_NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


class _QueueUrlopen:
    def __init__(self, *results: bytes) -> None:
        self.results = list(results)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError("unexpected BETDAQ network call")
        return _FakeHttpResponse(self.results.pop(0))


def _soap(
    method: str,
    result_attributes: str = "",
    *,
    return_status_code: str = "0",
) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_SOAP}" xmlns="{_NS}">'
        f"<soap:Body><{method}Response><{method}Result {result_attributes}>"
        f'<ReturnStatus Code="{return_status_code}" Description="fixture-status" '
        'CallId="fixture-call" />'
        f"</{method}Result></{method}Response></soap:Body></soap:Envelope>"
    ).encode()


def _balance(*, available: str, total: str, exposure: str = "0") -> bytes:
    return _soap(
        "GetAccountBalances",
        (
            f'Currency="EUR" Balance="{total}" Exposure="{exposure}" '
            f'AvailableFunds="{available}" Credit="0"'
        ),
    )


def test_betdaq_snapshot_roundtrips_through_durable_reconciliation(
    monkeypatch,
    tmp_path,
) -> None:
    """Provider identity/money must survive the newly composed durable boundary."""

    opener = _QueueUrlopen(
        _balance(available="100.01", total="120.02", exposure="-20.01"),
        _balance(available="99.99", total="119.99", exposure="-20.00"),
    )
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)

    credentials = BetdaqCredentials("alice", "p@ss", "app-id")
    requested = frozenset({BookmakerCapability.BALANCE_READ})

    first = BetdaqAccountReadOnlyClient(
        credentials,
        clock=lambda: datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc),
        account_id="caller-label-a",
    ).read_account_snapshot(requested)
    second = BetdaqAccountReadOnlyClient(
        credentials,
        clock=lambda: datetime(2026, 9, 22, 14, 31, tzinfo=timezone.utc),
        account_id="caller-label-b",
    ).read_account_snapshot(requested)

    # Caller-friendly labels are not account authority. Equal authenticated
    # credential/application contexts must project the same product-issued scope.
    assert first.profile.account_id == second.profile.account_id
    assert first.profile.account_id not in {"caller-label-a", "caller-label-b"}
    assert first.balance is not None
    assert second.balance is not None
    assert first.balance.available_balance == Decimal("100.01")
    assert second.balance.available_balance == Decimal("99.99")

    path = tmp_path / "betdaq-account-reconciliation.json"
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(first) is True

    # Restart the durable authority between provider observations. The successor
    # must compose with the persisted exact account scope rather than rebootstrap.
    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted.append_snapshot(second) is True

    reopened = BookmakerAccountReconciliationStore(path)
    state = reopened.latest_state()
    assert state is not None
    assert state.venue_id == "betdaq"
    assert state.account_id == first.profile.account_id
    assert state.adapter_id == first.profile.adapter_id
    assert state.latest_balance_observation is not None
    assert state.latest_balance_observation.observation_id == second.balance.observation_id
    assert state.latest_balance_observation.available_balance == Decimal("99.99")

    delta = state.unexplained_balance_delta
    assert delta is not None
    assert delta.amount == Decimal("-0.02")
    assert delta.previous_observation_id == first.balance.observation_id
    assert delta.current_observation_id == second.balance.observation_id

    # Exact replay after another reopen stays idempotent and cannot double-count
    # the provider-native balance movement.
    assert BookmakerAccountReconciliationStore(path).append_snapshot(second) is False
    assert len(opener.calls) == 2
