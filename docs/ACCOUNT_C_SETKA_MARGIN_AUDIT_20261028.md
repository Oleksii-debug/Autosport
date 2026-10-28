# Set-margin rating: exploratory Setka comparison

This follows `ACCOUNT_C_SETKA_HISTORY_AUDIT_20261028.md` and inherits all its
source-rights UNKNOWN, date-description conflict, abbreviated-identity and
retrospective-sample limitations. The original CSV is not redistributed here.
No odds, execution or bookmaker settlement is available; this is probability
research, not a betting or profitability result.

The exploratory rating uses the same pre-day prediction and next-day update
rule. It scales the Elo update by the absolute final set difference divided by
three; set scores are used only after that day's predictions. On the 5,770
earlier records, a grid of six K values and three margin weights selected
K=20, full margin weighting. The 2,080 later records show Brier 0.241071 for
this candidate, 0.242834 for the match-only K=10 comparator, and 0.250000 for
constant 50% (lower is better). The margin candidate did better on six of
the eight interpreted dates and worse on two. These dates and this experiment
were already inspected, the period is short, and selection has been explored;
the difference is not independently validated or promotion-ready.

The script `scripts/audit_setka_margin_rating.py --csv PATH` takes a separately
obtained input; the companion JSON binds source and script hashes and lists
per-day differences. Tests verify that changing a same-day set margin cannot
affect a same-day prediction, and that later outcomes and margins cannot affect
training-only parameter selection. Participant names, actual source timestamps,
rights and settlement are not independently confirmed. No runtime agent,
financial execution or profitability claim is changed.
