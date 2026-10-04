from autosport import _betfair_settlement_provider_row_semantics as settlement_semantics
from autosport.betfair_account_readonly import BetfairReadOnlyClient


def test_settlement_currency_snapshots_final_execution_readback_dispatch() -> None:
    assert (
        settlement_semantics._ORIGINAL_EXECUTION_READ
        is BetfairReadOnlyClient.read_execution_readback
    )


def test_settlement_correction_parser_composes_with_native_readback_surface() -> None:
    if settlement_semantics._NATIVE_CORRECTION_FIELDS:
        assert settlement_semantics._ORDER_WITH_FACTS_TYPE is settlement_semantics._BASE_ORDER
        assert (
            settlement_semantics._adapter._parse_cleared_order
            is settlement_semantics._ORIGINAL_PARSE
        )
        return

    assert (
        settlement_semantics._adapter._parse_cleared_order
        is settlement_semantics._parse_cleared_order_with_correction_facts
    )
