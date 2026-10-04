from autosport import _betfair_settlement_provider_row_semantics as settlement_semantics
from autosport.betfair_account_readonly import BetfairReadOnlyClient


def test_settlement_currency_snapshots_final_execution_readback_dispatch() -> None:
    assert (
        settlement_semantics._ORIGINAL_EXECUTION_READ
        is BetfairReadOnlyClient.read_execution_readback
    )
