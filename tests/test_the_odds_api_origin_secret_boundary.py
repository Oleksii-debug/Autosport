import pytest

from autosport.the_odds_api_provider import TheOddsApiProvider


@pytest.mark.parametrize(
    "base_url",
    (
        "https://attacker.example",
        "https://api.the-odds-api.com.attacker.example",
        "https://api.the-odds-api.com@attacker.example",
    ),
)
def test_credential_bearing_provider_rejects_noncanonical_https_origin(
    base_url: str,
) -> None:
    """An arbitrary HTTPS origin must never receive The Odds API credentials."""

    with pytest.raises(ValueError):
        TheOddsApiProvider(
            "SECRET_SENTINEL_DO_NOT_SEND",
            sport="soccer_epl",
            base_url=base_url,
        )
