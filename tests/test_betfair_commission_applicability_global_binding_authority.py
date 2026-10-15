from __future__ import annotations

import pytest

import autosport.betfair_commission_applicability as applicability


class _HostileJson:
    @staticmethod
    def dumps(*_args: object, **_kwargs: object) -> str:
        return '"caller-controlled-constant"'


def test_canonical_json_module_rebinding_fails_before_provider_read() -> None:
    original_json = applicability.json
    try:
        applicability.json = _HostileJson
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="assessment identity executable authority changed",
        ):
            applicability.assess_betfair_commission_applicability(
                object(),
                market_id="1.234",
            )
    finally:
        applicability.json = original_json


def test_canonical_json_dumps_rebinding_fails_before_provider_read() -> None:
    original_dumps = applicability.json.dumps
    try:
        applicability.json.dumps = _HostileJson.dumps
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="assessment identity executable authority changed",
        ):
            applicability.assess_betfair_commission_applicability(
                object(),
                market_id="1.234",
            )
    finally:
        applicability.json.dumps = original_dumps
