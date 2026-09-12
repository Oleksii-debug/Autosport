# Autosport accessibility and copyability contract

## User requirement

Autosport is keyboard-first and NVDA-first. A user must be able to navigate by semantic headings/landmarks and also select/copy the same useful text that is spoken. Accessibility speech must never depend on hidden/transient text that cannot be copied.

## DOM rules

1. Primary content lives in normal visible DOM text (`p`, headings, lists, table cells, labels, status summaries, code/pre where appropriate).
2. `aria-live`, `role=status` and `role=alert` contain only short duplicate notifications. They must not be the sole representation of market values, strategy explanations, ticket details, portfolio results, diagnostics or errors that the user may need to copy.
3. Do not globally apply `user-select:none`, `pointer-events:none` tricks to text, text rendered only via canvas, CSS pseudo-elements as the only content, or visually hidden primary data.
4. Standard browser/editing commands remain standard. Do not steal Ctrl+A/C/X/V/Z/Y from document selection, text controls or editable fields. App shortcuts must use non-conflicting combinations and respect focus context.
5. Re-rendering must preserve focus and selection where feasible. Prefer targeted text updates (`textContent`) over replacing entire containers containing the user's current focus/selection.
6. Every icon-only button needs an accessible name. Every input has a real label. Tables have captions/headers when tabular relationships matter.
7. Use one H1 per route/view and hierarchical H2/H3. Major application areas are landmarks or labelled regions.
8. Dynamic updates must be inspectable after the announcement: the latest value/status remains visibly present in the page.

## Chess defect lesson

Accessible Chess proves that a WebView/HTML interface can be excellent for NVDA, but Autosport must explicitly test a failure mode reported by the owner: some spoken content can be heard yet is difficult/impossible to copy from the page. The exact root cause in every Chess surface is not declared here as proven. Autosport therefore treats the architectural symptom as the regression target: important text may not exist only in transient live regions, accessibility-only nodes, native accessibility names or generated content.

## Automated regression checks

CI must at minimum assert that the canonical shell has semantic main/navigation/headings, primary output containers are not hidden, no global `user-select:none` is present, no primary data owner is `aria-live` only, and application JavaScript does not intercept Ctrl+C/Ctrl+A for ordinary content. Browser-level tests will later verify actual selection and Clipboard copy from representative dynamic market/ticket/portfolio text.

## Human evidence boundary

Automation can prove DOM semantics and keyboard contracts but cannot claim human usability. `NVDA_VERIFIED=true` and `HUMAN_TESTED=true` require an exact packaged Windows build physically tested by the owner or another human NVDA user.
