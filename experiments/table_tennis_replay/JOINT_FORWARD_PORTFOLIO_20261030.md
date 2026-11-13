# Retrospective shared-book replay of two recorded PAPER sessions

This is a research reconstruction of two already inspected sessions. It is
not a forward holdout, a live order, a trained strategy, verified settlement,
or a test of profitability. The two policies are alternative simulations,
not separate bets to be added together. The [analysis plan](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6517026609)
was recorded before the joint calculation; outcomes were already known.

## What changed

Each earlier forward session had its own 100-unit PAPER book. The new
`joint_forward_portfolio.replay` verifies each frozen action journal and pinned
dated-result revision history using the existing research adapters, then
replays the frozen choices through **one** canonical `PaperBook` and
`PaperRiskPolicy` for each alternative threshold. The second session therefore
sees the balance actually produced by the first in this counterfactual.
Reevaluating risk on a shared book is retrospective: the original actions were
not originally made with that shared balance.

The initial capital per alternative is 100 units, each assumed stake is 1,
and 0.01 units per opening are charged only in the separate cost stress.
Recorded source prices are assumed fills; they are not bookmaker acceptance.
The no-bet baseline is zero. The original frozen price-threshold rejections
remain rejected; no replacement candidates are selected. The existing risk
policy and an additional fee-stressed reserve gate are checked before any
assumed opening. A quote over 30 seconds old at the recorded frozen action is
rejected. Actions are ordered before dated receipts at the same instant.

Each snapshot uses the existing receipt-bound result eligibility and
revision/quarantine logic. All legs must be eligible before the paper ticket
settles. If a later recorded revision quarantines a leg on a previously
settled ticket, that **entire alternative book is held** from that receipt:
current balance, current won/lost/pending and net are not reported, and later
actions are refused. Earlier as-of reports and the number previously settled
are retained for review; there is no automatic rollback or resettlement.
An unresolved or quarantined leg of a still-open ticket remains pending.

Sessions must be strictly non-overlapping from first captured request through
last dated receipt. Any exact selected source fixture ID reused across sessions
fails closed instead of being merged. A later opening is blocked if either
exact ID occurs in an earlier session's recorded result bodies, even if no
eligible earlier result was established. Unknown IDs and malformed prior
identities are not guessed; no alternate legs are substituted. This conservative
boundary does not support cross-session shared-ID positions and does not prove
that different IDs correspond to different physical matches. Shared-leg
exposure **within** a session is counted by exact source ID.

## Recorded, already observed material

The first session's three-link revision history is pinned at
`8842cef389259d2f52be1449d30201be3ffd31d3b2ced8547d3e112bca51c133`;
the later session's at
`130ac6e916b9703ea887231a4b60e9e037d670d242612ec555c69e1f564bf295`.
The second session's 20,001-ID prior scope was reconstructed and its SHA-256
`31c987453e93f05af529f9cf8b45bcdcf8391bcfce8866ba64caaba81e2710d7`
matched the stored frozen scope. The selected source ID sets did not overlap
exactly; this is not identity verification or an independence claim. Each
session opened a single hypothetical two-leg ticket in each alternative and
rejected its second proposed opening under the previously frozen threshold.

| Recorded local receipt/action time, UTC | Shared-book as-of state for either alternative |
|---|---|
| 00:13:12, first action | One ticket opened; one unit committed |
| 00:14:24, first dated response | Both legs unresolved; ticket pending |
| 00:20:16, second dated response | Both source-reported legs eligible; ticket settled as a win |
| 02:08:28, second session action | A second ticket opened using the carried-forward balance |
| 02:09:58 and 02:15:17, dated responses | Second ticket pending; first result remains in the book |
| 02:59:52, third dated response | One selected side reported lost; second ticket settled as a loss |

For **each alternative**, the resulting counterfactual has two assumed tickets,
one source-reported win and one loss. Cash excluding hypothetical costs is
`100.1388888888888891444444444`; two assumed costs total `0.02`; the final
cost-stressed net is approximately **+0.1189 units**. These are two tickets
in one book per policy, not four independent observations. Open stake peaked
at one ticket, and exact-ID shared exposure peaked at one: these two recorded
sessions did not overlap, so they do not empirically exercise concurrent
cross-session positions. Pending stake is carried at face value, not marked
to market. The maximum face-value cost-stressed drawdown was 1.01 units.
An independent `Fraction` calculation from the frozen price strings agrees
with the reported net to within `1e-24` units. The result is a tiny,
already-observed, selected sample and provides no evidence of a sustainable
edge or accepted prices.

## Evidence and limits

The code reuses the canonical PAPER financial authority rather than changing
or duplicating it. Thirteen new synthetic tests cover shared cash, exact-ID leg
exposure, risk on a shared balance, prior-result exclusion, corrections after
settlement, pending quarantine, stale quotes, non-overlap, ID reuse, missing
and modified evidence. The tightened-reserve stress test uses a deliberately
stricter test policy; the recorded replay uses the default policy. The
Linux/Python 3.12 targeted research/identity suite passed **284 tests** and
syntax compilation passed. GitHub CI must be reported separately for the
published exact head.

One extracted copy of the second session contained an extra 5,242,880-byte
file, which was the exact prefix of the larger expected result body. The
existing verifier correctly rejected that directory, and the extra file was
not deleted. A separately retained original session directory contained
exactly the 12 expected evidence files; each of those 12 byte hashes matched
its counterpart in the rejected copy. That clean original was revalidated
against its receipts, frozen journal, scope and pinned history and used for
the replay. This does not prove why the extra file appeared or that no other
source observations existed.

The history hashes bind recorded evidence relative to supplied external pins;
they do not establish that omitted responses or corrections never existed.
Clocks are local and cross-system comparisons assume synchronisation. Player
identity, source rights, actual completion times, accepted bookmaker prices,
settlement and match independence are unverified. Alias collisions between
different source IDs may conceal overlap. The replay cannot continue through
overlapping session histories or corrections that arrive after a later session
starts; those require a more general, separately validated event journal. No
model was trained, no source data entered a production store, and no real bets
were placed.
