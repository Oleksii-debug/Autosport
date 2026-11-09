# Same-ID timestamp migration before prospective freezing

The first same-day refresh found two IDs already in the archive received on
November 8. In both cases the league, source-side names, and complete final score
were unchanged, but the provider's finished_at / updated_at moved from November 8
to November 9. The shifts in finished_at were about 18 hours and 16.5 hours.
This cannot be treated as two newly observed physical matches merely because
the date-filtered endpoint returned them again.

The initial script stopped on its overlapping-ID assertion. It had made exactly
one results request and had NOT requested the new market prices, selected any
future fixture, or produced forecasts. Preserve that failed script, receipt,
raw refresh and row-level overlap audit privately.

Before receiving the new prices, clarify the integrity rule: remove every
overlapping ID from BOTH the old-only and old-plus-today history states. Do not
choose a preferred timestamp, count the duplicate again, infer a new match from
the score, or use either version as evidence of recent form. Record the removed
IDs and counts. Keep their IDs in the forbidden historical-ID set for future
cohort selection. Use the already captured refresh, not a replacement query.

The model formulas, seven policies, costs, selection ordering, future horizon,
maximum cohort size and analysis rules remain unchanged. This is a source
integrity repair before forecast generation, not outcome-based strategy tuning.
