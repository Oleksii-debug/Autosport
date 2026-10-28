# Economic evaluation of forecast calibration

Owner goal: increase eventual net earnings. Compare the existing probability
calibrator with its unchanged market-probability baseline on the same recorded
match population. This is exploratory research following earlier inspection of
this dataset; it is not a new holdout and cannot authorize a model promotion.

Protocol fixed before executing this comparison: source TipsAudit historical
CSV `c7964f06b9aa02800381c50aca4637ef8c1ad7e940855d3f9c3b5f1bd40e53c5`;
one event per ID; no void/unknown outcome in calibration feedback; all three
probabilities train from a result assumed available 48 hours after kickoff.
This is an assumption, not a verified publication time. Source probabilities
and historical prices are also assumed available by the decision, and have
known timing caveats; the CSV does not prove this. Never use closing odds.

Existing 10-bin, 100-observation minimum, 100-prior calibration from
`prospective_forecast_learning.py`, applied separately to home/draw/away, with
renormalization. At each event, only labels available before that event can
affect the forecast. No event or simultaneous event sees its own outcome.

Use the existing fixed price rule: odds <= 3.5, probability >= .30, model value
5–12%, highest model value among eligible soft quotes, no exchanges, up to five
selections per kickoff day per variant. Only dates on or after 2026-09-21 have
the provider's newer soft-price definition. Unlike the future API, this
retrospective proxy selects from three recorded soft quotes. Decisions for void
or unknown results still consume the daily cap; they do not create settled P&L.
Selection ordering by kickoff and ID is a proxy: true quote capture times and
accepted prices are absent, and parallel capital exposure is not modeled.

Report raw and 1%-of-stake cost-stressed unit P&L, win rate, ROI, drawdown and
day-block intervals for both policies; count distinct and shared selections.
Compare Brier score for all valid settled validation matches (three outcomes
per match), not just bets. Keep full decision records. No parameter tuning or
winner promotion after inspecting this result. An apparent improvement must be
separately tested on new forward records before any product use.

Source attribution: TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0.
