# Windows accessibility prerequisite — tk-uia provenance

Status: V1 machine prerequisite. This integration does not constitute physical NVDA acceptance.

## Dependency

- Upstream repository: `HuzPro/tk-uia`
- Pinned package version: `tk-uia==0.8.0`
- Upstream tag: `v0.8.0`
- Exact upstream commit behind that tag: `dd73aa99369862b4d485e3204e72ba1e190a9015`
- License declared by upstream package metadata: MIT
- Upstream runtime dependencies at the pinned commit: none
- Autosport integration type: external pinned dependency; upstream source is not copied into this repository.

## Why it is used

Autosport V1 currently uses Tk/Ttk. Bare Tk 8.6 controls do not provide a sufficient Windows accessibility surface for the V1 keyboard/screen-reader goal. `tk-uia` annotates and provides Windows accessibility/UIA semantics for Tk controls while allowing the application to keep its existing GUI architecture.

Autosport assigns stable accessible names, descriptions and Automation IDs to the critical V1 controls: dataset selection, replay launch, replay speed, paper-ticket/result list and execution log.

## Machine gate

The packaged Windows candidate supports `--accessibility-audit-output <path>`. The audit launches the real packaged GUI, reads `tk_uia.describe()` for the critical controls, requires stable names/roles and required patterns, rejects provider trouble, and writes a JSON result. The Windows build fails if the packaged audit is not PASS.

This is deliberately a prerequisite audit, not a claim that an external Windows UIA client or NVDA speech output has been proven. Upstream itself documents `describe()` as an in-process report and distinguishes its UIA-tree verification from future NVDA speech verification.

## Human acceptance boundary

`NVDA_VERIFIED` remains `false` until a human tester runs the exact packaged Autosport build on physical Windows 11 with NVDA and verifies focus order, announcements, control names/states, list navigation, keyboard commands, dialogs, error flows, replay updates and restart/resume behavior.

`HUMAN_TESTED` remains `false` until the corresponding physical acceptance evidence is recorded.

REAL_MONEY_EXECUTION=false.
