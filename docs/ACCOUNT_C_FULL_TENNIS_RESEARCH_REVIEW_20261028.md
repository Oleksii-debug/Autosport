# Retrospective outcome-and-odds research: 2025 tennis

Attribution: minmaxxxer, ATP/WTA Closing Odds 2025, CC BY 4.0,
https://huggingface.co/datasets/minmaxxxer/atp-wta-closing-odds-2025.
The 5,791 data rows were reconstructed from the source's public text view;
all line positions 0–5791 were present, with overlapping positions identical.
The resulting UTF-8 text snapshot SHA-256 is
`05ad12b646e55eba9b626b2860cfb98dd6fc869c173e3b9d7dec2016bfb58c20`.
It is the hash of the reconstructed text, not independently verified identity
of the publisher's downloadable binary. No full source file is committed here.

For the predeclared split, 3,297 completed earlier matches warmed the Elo model.
From July through December 2025, 2,256 matches were eligible and 99 were excluded
as retirement/incomplete or ambiguous score. All controls share those 2,256
eligible matches. Unit stakes and a hypothetical 2% fee on winning profit:

| Policy | Bets | Wins / losses | Net units | ROI | Drawdown units |
|---|---:|---|---:|---:|---:|
| No bet | 0 | 0 / 0 | 0 | unavailable | 0 |
| Market favorite | 2256 | 1565 / 691 | +13.3260 | +0.59% | 37.2392 |
| Fixed Elo + edge | 1770 | 481 / 1289 | -125.8576 | -7.11% | 170.8736 |
| Elo + prior-history guard | 1616 | 447 / 1169 | -113.0990 | -7.00% | 157.5060 |

Results use **reported best closing quotes**, potentially from different books;
price availability and simultaneous execution are unverified. ROI and drawdown
are hypothetical, not actual earnings; drawdown assumes match-start order, not
verified settlement chronology. The source contains completed matches only,
may omit events and may have selection or survivor bias. It is historical,
already published, and is not independently registered prospective evidence.
Neither apparent +0.59% for the favorite nor the Elo losses establish a future
profit rate; tennis performance cannot be transferred to table tennis.

The guard reduced hypothetical exposure and absolute loss, but both Elo challengers
remained materially negative under these assumptions. Do not promote either or
alter product learning from this result. Test a new hypothesis against unseen,
causally timestamped and executable price/outcome data rather than tuning to the
same sample. Negative results and all flags are retained in the JSON.

Eleven fixture tests passed across the input adapter and economic diagnostic,
including outcome chronology, cost accounting, duplicate IDs, unknown winners,
retirements and no fabricated wins. They validate implementation behavior; they
are not independent authentication of the publisher's data.
