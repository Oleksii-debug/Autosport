# First scheduled result read — 2026-11-13

Board A forecast SHA256 `e3462b898a22687bcb28c6b280d466f6f47104311721789be2f366965a243ff2`. At the first allowed result read, 7/9
resolved and two IDs were absent; neither unresolved row had any frozen action.
H2H: two hypothetical tickets, one win, +0.1850 after costs and +0.1175 under
the stress haircut. All other rules abstained. Preserve this first evaluation
even if the later shared result snapshot changes coverage.

Separate set-count retrospective comparison: all 128 closing reads completed,
77 eligible. Set-count H2H returned +2.196666666666666536 on three tickets and
match H2H +1.284666666666666536 on four. Set-count Brier/log loss
0.24466328 /
0.68192346, worse than the unchanged
match-winner H2H (0.24390096 /
0.68084743).
The three winning tickets do not justify selecting the set variant. The
historical result is not a new prospective test. No model update yet.
