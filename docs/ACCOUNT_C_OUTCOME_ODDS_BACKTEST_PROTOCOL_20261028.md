# Historical outcome-and-odds economic diagnostic

Purpose: evaluate a research agent against published match outcomes and quoted
prices instead of treating price movement as a win. This is a **research-only**
diagnostic and does not change any running campaign, production memory, risk
authority or promotion decision.

Potential public source: Valuebetennis, Tennis results and odds since 2021,
https://www.valuebetennis.com/en/donnees.htm, CC BY 4.0 with attribution.
The publisher reports opening and closing prices, player identifiers and winners.
This source is **tennis**, not table tennis. Its reported price availability,
match results, publication times and survivorship are not independently verified.
No CSV has been admitted merely because this source exists; exact file SHA-256,
retrieval provenance and a successful real-data run must be recorded separately.

## Fixed design

- Earliest accepted data is 2021; use 2021 for warmup and evaluate calendar 2022.
- Reject duplicate match IDs, ambiguous winners and invalid dates. Exclude invalid
  quoted odds rather than silently convert them into wins. Record excluded events and
  the reason, including missing odds or incomplete/retired scores.
- Forecast each UTC day from Elo ratings updated only with results from dates
  strictly before that day; update the current day's results as a batch after all
  its predictions. Same-day results cannot inform other predictions on that day.
  Actual result-publication time remains unverified by this source.
- Compare a no-bet control, a fixed market-favorite control and a research Elo
  challenger on the same eligible 2022 events. Elo starts at 1500, K=16, and
  uses a 5-percentage-point estimated edge threshold over inverse opening odds.
  Parameters are fixed engineering choices, not tuned or proved profitable.
- Hypothetical one-unit stakes, exact Decimal cash accounting, and a fixed 2%
  deduction on positive winnings. Report no-bets, bets, wins, losses, gross and
  net return, turnover, ROI only with nonzero turnover, and maximum drawdown.
  These are hypothetical assumptions, not accepted fills or actual commission.
  Drawdown uses chronological match-start order with a match-ID tie-break;
  actual settlement order and simultaneous exposure are unknown, so that
  drawdown is not a reconstructed real portfolio path.
- Every result records exact input and evaluator hashes, date boundaries,
  exclusions, assumptions and all truth flags. It cannot enter forward learning
  or be promoted. A historical read is not an independently precommitted holdout.

## Scope limits

The input may contain only games already settled and published by the dataset
provider, producing selection bias. Actual available time of opening quotes,
delayed starts, match-result publication, bookmaker settlement rules, account
limits, rejected bets and slippage are not established by this flat CSV.
Treat the output as retrospective exploratory research and do not infer real
profits or table-tennis performance. Preserve negative findings and all exclusions.
