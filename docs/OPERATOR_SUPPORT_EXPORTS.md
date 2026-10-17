# Autosport — operator diagnostic and agent reports

The application records a **one-second process heartbeat** and instrumented operator/runtime events automatically while the Windows interface is running. A second is not evidence that an external provider, individual model operation or OS syscall was observed. If heartbeats were missed, the diagnostic report says how many seconds were not observed.

From the Windows interface, open the Data/Strategy area or Diagnostics and use **«Зберегти діагностику та звіт агентів»**. Enter an existing directory which is the parent of the Autosport workspace or an ancestor (the same conservative destination rule as the existing evidence exporter). You receive two independent UTF-8 JSON files:

- `Autosport-diagnostics.json` — UTC seconds, heartbeat/gap coverage, instrumented action/status events, counts, timestamps. It includes the last three UTC dates. The recorder persists daily hash-chained records in the workspace's `operator-support-trace` folder, using the existing `ForensicSessionJournal` verification and crash-recovery rules. It does **not** contain arbitrary environment variables, credentials, raw provider data, dataset records or user paths.
- `Autosport-agents.json` — current strategy, exact declared agents and composition digest, descriptions of goals, whether a research plan is selected, a typed recent activity chronology, and safe *presence-only* metadata for selected memory/learning records. File existence does not demonstrate that an agent learned or that a model was promoted. Unknown facts remain explicitly unverified.

Both exports are made in an operator-requested background worker. They do not grant risk/financial privileges or launch learning jobs. The files can be provided later to support/ChatGPT, though users should still review them before sharing. If the writer or history integrity verification fails, export must report a failure rather than claim complete coverage. The user is not expected to read this document visually: buttons, labels and result status have native HTML labels and live-region text accessible to keyboard/screen readers.

## Scope of automated sampling

A product launch creates the daily journal; while it runs, an application-owned background thread attempts one heartbeat per second. It also records the allowlisted UI actions and canonical PAPER runtime STARTED/TICK/STOPPED/ERROR transitions. It does not collect every internal instruction, private chain of thought, provider response, password or browser URL. It records real observed activity *only* and leaves gaps as gaps. Each daily file is bounded by the existing 64 MiB forensic-journal capacity. Export defaults to a three-day snapshot and fails closed when its safe row limit is exceeded rather than silently truncating. Reports are not a replacement for the canonical financial, research or source-evidence ledgers.

## AutoTrade comparison

The system deliberately adopts the **separation** described in AutoTrade's `docs/engineering/05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md`: declared strategy role and goal, observed agent activity, durable memory evidence, scientific validation and model/champion promotion are separate facts. It does not import AutoTrade's trading/financial code or treat describing a learning policy as proof that training was run. Autosport retains its canonical agent/strategy/scientific/memory authorities.


## Two-file integrity and exact strategy scope (source extension)

Both JSONs share report_pair_sha256, calculated over the complete contents
of both reports with the pair digest excluded. The verify_report_pair()
function reads both published files back and rejects a mixed pair, tampering,
different snapshot time, or an unexpected claim of release/real-money/NVDA
authority. SHA-256 is not a signing certificate. If one file is from a
previous export, download both again from one operation.

For a strategy other than the baseline, the science registry, AgentLoop,
ResearchSupervisor and memory inventory are read from that strategy's
canonical strategies/identity workspace. The technical one-second journal
remains rooted at the product workspace, allowing multiple strategies to
share process-level history. The agent_workspace_scope field is either ROOT
or CANONICAL_STRATEGY, without leaking a personal directory name. A symlink
or arbitrary external folder is rejected. Incomplete research configuration
requires selecting its valid research plan first.

The WebView2 research area displays Ukrainian explanations of each registered
strategy goal and agent role. Those descriptions do not prove successful
training. The Оновити стан навчання control uses the same selected strategy
workspace. Research and AgentLoop snapshots newer than the report's causal
cutoff fail closed, rather than presenting future state as historical truth.


## Independent operator diagnostics under damaged science or memory

The coverage summary also exposes `last_verified_heartbeat_age_seconds` and
`tail_seconds_without_verified_heartbeat` at the exact report cutoff. A report
can otherwise display zero *between-observation* gaps while its most recent
heartbeat is already several seconds old. An unknown tail (no heartbeat) is
`null`, never silently reported as zero. This is evidence of absent verified
process heartbeats only; it is not a measurement of OS operations or a claim
that the provider is down. A heartbeat newer than the report cutoff is rejected. A system-clock
rollback cannot turn later journal entries or queued original observation
seconds into earlier causal evidence. Pair verification independently rejects
future diagnostic/agent event times even after both SHA-256 digests are
recomputed. Such a mismatch fails the export instead of inventing history.

The recorder status distinguishes HEALTHY (verified heartbeat, no detected
gap/drop/pending event), NO_VERIFIED_HEARTBEAT, PARTIAL_HEARTBEAT_GAPS,
PARTIAL_PENDING_EVENTS, PARTIAL_DROPPED_EVENTS, and RECORDER_ERROR.
The outstanding-event count is sampled once for both reports. A healthy
sampler is still not proof of all OS, provider, or model operations.

The forensic trace and scientific/memory evidence are different verified sources.
Corrupt, inaccessible, symlinked or changing science never erases separately
verified technical chronology. Invalid causal cutoff or fabricated success flags
also fail closed into VERIFICATION_FAILED without erasing valid diagnostics. `coverage.scientific_evidence_verification_failed`
and `coverage.artifact_inventory_verification_failed` identify independent failures.
Damaged or malformed science uses `science.state=VERIFICATION_FAILED`,
`learning_verification=SOURCE_VERIFICATION_FAILED_NO_LEARNING_CLAIM`, and
`proven_live_model_learning=false` with no private source or exception text.
Damaged inventory is empty rather than claimed present and does not discredit
otherwise verified science. A corrupted diagnostic journal still fails closed;
none of these reports proves human NVDA or real execution.
The one-second heartbeat proves process liveness only, not the observation of
every internal instruction/OS call/provider event. Missing seconds, lost
events and uninstrumented operations remain explicit limitations.
