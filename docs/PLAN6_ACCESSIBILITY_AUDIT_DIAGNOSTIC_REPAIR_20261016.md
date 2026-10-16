# Plan 6 accessibility audit diagnostic evidence repair

Status: NOT DONE. No machine qualification or NVDA acceptance is claimed.

Live main baseline: d439d442f234971504a5a77549248462d5ecdf4e.

Independent defect: src/autosport/accessibility_audit.py includes unfiltered exception details and provider diagnostic strings in JSON evidence. A failing provider may embed confidential connection material in these fields. The in-process audit must preserve FAIL status while ensuring these diagnostic fields are safe for publication.

Repair: use the existing src/autosport/secret_redaction.py presentation helpers for exception details, provider trouble and provider shutdown reason. Add negative tests covering labelled connection tokens, URL query credentials, and hostile exception type names. Verify the resulting JSON excludes synthetic confidential values while retaining status FAIL and human_tested=false and nvda_verified=false.

Integration requirements: exact-head cross-platform CI and Windows Candidate, accepted main readback, then update canonical Plan 6 status. Do not modify PR #1598 or its active owner files. Physical NVDA acceptance remains in Plan 8.

Current blocker: code update of the audit module was rejected by the available write tool. No production patch or passing tests are claimed.
