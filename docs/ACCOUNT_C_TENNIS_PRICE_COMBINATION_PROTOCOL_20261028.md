# Post-hoc price-combination check

After observing the trained research candidate's historical hypothetical gains,
audit whether its selected opportunities relied on the two publisher-reported
best closing odds satisfying `1/odds_a + 1/odds_b < 1`. Such a pair may reflect
genuine arbitrage, but source data do not prove the two quotes were simultaneously
available or executable. This is a structural warning, not proof a real arbitrage
did or did not exist.

Add a **research-only conservative control**: retain the trained candidate's
original decision, but emit NO_BET when its two best prices form an unverified
underround pair. Keep the exact 2,256-event population, actual hypothetical unit
stake and fee assumptions, historical wins/losses, and probability forecasts.
Count every affected candidate; do not silently remove them. The screen is
chosen after observing results on the same data; it does not prove an edge,
liquidity, real profit, point-in-time availability or a statistically valid
holdout. It must not be deployed to product learning or used for real money.
