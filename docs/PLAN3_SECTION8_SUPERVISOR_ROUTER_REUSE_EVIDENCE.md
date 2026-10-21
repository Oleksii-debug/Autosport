# Plan 3 / Section 8 — Research Supervisor and Model/Compute Router: source-identical qualification

Status: **DONE** for repository-controllable component engineering. No continuously-running hosted service, authenticated bookmaker access, real-money authority, or physical NVDA result is claimed.

## Acceptance and implementation reused
- **8.1:** `src/autosport/research_scheduler.py` owns bounded durable wakeups, interval cadence, occurrence identity, misfire/pause/STOP handling and crash/redelivery; `research_trigger_adapter.py` (not the scheduler) validates the scientific trigger, while `research_supervisor.py` owns the frozen, persistent research run and checkpoint state. A scheduler wakeup is a trigger, never a substitute for the Autosport-owned journal.
- **8.2:** `research_supervisor.py` fixes the scientific phase order QUESTION -> HYPOTHESIS -> SOURCE_SEARCH -> PROTOCOL_FREEZE -> DATASET_SNAPSHOT -> EXPERIMENT -> CAUSAL_EVALUATION -> ROBUSTNESS -> CHAMPION_CHALLENGER -> FORWARD_PAPER_SHADOW -> DECISION -> POSTMORTEM -> MEMORY -> NEXT_QUESTION -> COMPLETE. `research_supervisor_actions.py` bridges phase transitions to existing ScientificRegistry, frozen Factory, causal learning environment and durable negative-result/postmortem memory; replay of an already persisted downstream action after a checkpoint crash cannot silently grant a new decision.
- **8.3:** `model_compute_router.py` retains deterministic / local / cloud / WAIT tiers and applies data classification, supported model capability, deadline, measured evidence and cost; lack of eligible evidence or route fails closed. Model output never owns paper/real economic state, arithmetic, Risk limits, promotion or owner authority. Deterministic/no-LLM paths remain available where the contract permits.

## Exact source/test identities
Previously tested complete-suite PR [#793](https://github.com/Oleksii-debug/Autosport/pull/793) head: `6633da8f0022e1807d56d91daf5033c2997b962b`.
Main readback immediately before Section-8 closure: `a9ad71e85da345d5da2c2eb2603c84aac739cea7`.
GitHub compare: ahead=75, behind=0. The changed paths since #793 are other-plan performance/Windows-lab files and evidence/status documents; **none of the Section-8 implementation/test paths below changed**.

Production blobs:
- `research_scheduler.py` `02b00bc27daa8147527c673861f6fc339161452c`
- `research_supervisor.py` `ded56a5e4dd45a7b8194657b41af202419fda9f3`
- `research_supervisor_actions.py` `9a76d3143bc01c131cca8b60402a7b04472cefd7`
- `model_compute_router.py` `bea69b62f02a620737127ba74e4acf54396f4a33`

Focused unchanged test blobs:
- `test_research_scheduler.py` `9ddc1c67167c48a31c53318b5e990238caef26e2`
- `test_research_supervisor.py` `1ab590cdfe9ebf140f9f92a4795255461988aa4e`
- `test_research_supervisor_actions.py` `ee82ee4fdd344df66261c6077c3a661ca7b52c88`
- `test_research_supervisor_admission_stop.py` `1a3d12114ca4a739df306518889ba50f60dc56ca`
- `test_research_supervisor_next_question_replay.py` `cc31a8720195e10c64b8c4db36ac7ed01254d7f0`
- `test_model_compute_router.py` `20d90488796a222e02ac01349dc0d65b738f86cb`
- `test_model_compute_router_exact_type_admission.py` `6220ec1b33a3d9e84380d174d5b08be1f6d3b0d5`

## Executed acceptance evidence
- [Full CI #37884352611](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611) **SUCCESS** at the exact immutable head above: `python -m pytest -v tests` succeeded on Ubuntu Python 3.11/3.12 and Windows Python 3.11/3.12. The named focused tests above are included in that complete `tests` run. [Windows Candidate #37884352755](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755) **SUCCESS** (build + admission). Both runs have matching immutable head SHA; PR #793 merged, no unresolved review threads.
- Scientific/causal: immutable question/phase/protocol bindings, forward/PAPER evidence distinct from past inference, no self-promotion.
- Restart/memory: schedule idempotence after downstream ACK crash, pending replay, checkpoint restoration, memory/next-question replay.
- Adversarial/negative: conflicting trigger, altered scheduling authority, future time, tampered durable JSON, STOP/PAUSE/deadline/budget admission, empty learning evidence, malformed/mutated route payload and cloud/privacy/cost-fail-closed paths.
- No new production subsystem or untested source mutation was introduced. Existing tests and implementations were qualified by byte identity against a previously executed comprehensive CI gate, not by claiming a fresh local test run.

## Deliberate boundaries
External workflow trigger availability, a physically unattended 24/7 runtime observation, cloud provider account eligibility, paid-model usage, provider execution and physical NVDA are **not** part of this repository-controllable component engineering DONE. This does not expand Risk/owner permissions; Plan-7/8 campaigns own their separate execution and external evidence.
