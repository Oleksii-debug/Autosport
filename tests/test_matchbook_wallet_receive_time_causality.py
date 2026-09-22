from __future__ import annotations

from decimal import Decimal
import hashlib
import json

from autosport.matchbook_provider import MatchbookHttpJsonResponse
from autosport.matchbook_wallet_evidence import MatchbookWalletEvidenceClient


AFTER = "2026-09-21T00:00:00Z"
BEFORE = "2026-09-22T00:00:00Z"


class _MutableClock:
    def __init__(self) -> None:
        self.now = "2026-09-21T20:00:00Z"

    def __call__(self) -> str:
        return self.now


def _response(payload: object) -> MatchbookHttpJsonResponse:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return MatchbookHttpJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_wallet_evidence_observed_time_is_sampled_after_response_bytes() -> None:
    clock = _MutableClock()

    def transaction_transport(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> MatchbookHttpJsonResponse:
        clock.now = "2026-09-21T20:00:05Z"
        return _response({"transactions": []})

    client = MatchbookWalletEvidenceClient(
        "secret-session",
        transport=transaction_transport,
        clock=clock,
        sleeper=lambda _: None,
    )
    window = client.read_transaction_window(
        after=AFTER,
        before=BEFORE,
        per_page=20,
    )

    assert window.pages[0].observed_at == "2026-09-21T20:00:05Z"
    assert window.observed_at == "2026-09-21T20:00:05Z"

    clock.now = "2026-09-21T20:01:00Z"

    def balance_transport(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> MatchbookHttpJsonResponse:
        clock.now = "2026-09-21T20:01:07Z"
        return _response(
            {
                "id": 77,
                "balance": Decimal("100"),
                "exposure": Decimal("5"),
                "commission-reserve": Decimal("1"),
                "free-funds": Decimal("94"),
            }
        )

    balance_client = MatchbookWalletEvidenceClient(
        "secret-session",
        transport=balance_transport,
        clock=clock,
        sleeper=lambda _: None,
    )
    balance = balance_client.read_balance()

    assert balance.observed_at == "2026-09-21T20:01:07Z"
