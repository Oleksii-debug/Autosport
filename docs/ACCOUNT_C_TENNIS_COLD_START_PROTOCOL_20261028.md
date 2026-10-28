# Post-hoc cold-start guard on the same tennis preview

After inspecting the first preview's outcomes and its player-history breakdown,
test one research-only guard: apply the same Elo/edge policy only when **both**
players have at least one previously recorded completed match on strictly earlier
dates. Otherwise emit NO_BET. Same-day observations remain unavailable. Count
all 65 eligible matches and preserve baseline, exclusions, fees and drawdown rules.

This candidate and threshold were selected after viewing the sample. The sample
is consumed development data and is not a holdout; do not infer profitability,
significance, real fill or promotion. The source is tennis, not table tennis.
The original outcome is preserved at commit `6feb92396284e7db955d80796002ea718a2297ec`.
