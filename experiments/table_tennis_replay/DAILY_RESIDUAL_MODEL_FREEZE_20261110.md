# Next-day learned model commitment — 2026-11-10

[The update rule](DAILY_RESIDUAL_UPDATE_PROTOCOL_20261110.md) was published as
6e104d387363a68ffeea6fdc61aba9c880bfcb86 before the first test was scored.
The update has now run unchanged. Replication outcomes have NOT been requested.

- Training: 200 rows over 17 UTC prediction days:
  the original 130 plus 70 eligible November 9 results.
- One additional result was excluded because its source update was at or after
  the November 10 00:00 UTC cutoff. No row was removed for losing or winning.
- New coefficients: **a=0, b=0**. Mean forecasts equal the normalized market
  reference. This is absence of supported adjustment, not a profitable forecast.
- 104/200 whole-day bootstrap fits select zero weights.
- Model file SHA256: `01074f8279715996b3a65531afbc5a0997d699d4437c7e87bf50b1b7a0946c22`.
- Semantic model digest: `ecf07e2de0d8396f637a90c0308d7bb3435d7e68e3444e61389f62eee408d0b1`.
- Fitted at: `2026-11-10T08:10:56.482852+00:00`.
- November 9 forecast SHA256:
  `29300a3eb3ecff7687f4fe57af2f139113b05eaebf226e8f6228628bbc9f3958`.
- The fixed control remains **a=0, b=0.25**, its original hash unchanged.

Both new policies are evaluated alongside the six original rules on the exact
[replication cohort](MARKET_RESIDUAL_REPLICATION_PROTOCOL_20261110.md).
No coefficient or threshold will be changed after its outcomes arrive. The test
is retrospective, not a real bet or live prospective forecast. All results,
including zero wagers, are retained. Production promotion remains false.
