from __future__ import annotations

from pathlib import Path


APP = Path(__file__).parents[1] / "src" / "autosport" / "windows_web" / "app.js"


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def test_owner_confirmation_is_disabled_until_latest_preview_completes() -> None:
    source = _source()

    assert "let ownerReviewFresh = false;" in source
    assert "let ownerReviewEpoch = 0;" in source
    assert "function isAnyWorkerBusy(state)" in source
    assert "function syncOwnerConfirmationAvailability(canInitialize)" in source
    assert "canInitialize !== true" in source
    assert "ownerReviewFresh !== true" in source
    assert "isAnyWorkerBusy(latestState)" in source
    assert 'const checkbox = byId("owner-confirm-checkbox");' in source
    assert "setDisabledWithFocusFallback(checkbox, reviewDisabled);" in source
    assert "reviewDisabled || checkbox.checked !== true" in source
    assert "syncOwnerConfirmationAvailability(state.owner.can_initialize);" in source

    preview = source.index('byId(328).addEventListener("click", async () => {')
    preview_invalidate = source.index("invalidateOwnerReview();", preview)
    preview_epoch = source.index("const reviewEpoch = ownerReviewEpoch;", preview)
    preview_dispatch = source.index('dispatch("owner.preview", {', preview)
    preview_fresh = source.index("markOwnerReviewFresh(reviewEpoch);", preview)
    assert preview < preview_invalidate < preview_epoch < preview_dispatch < preview_fresh
    assert 'result && result.status === "completed"' in source[preview:preview_fresh]


def test_create_actionability_tracks_explicit_confirmation_checkbox() -> None:
    source = _source()

    sync = source.index("function syncOwnerConfirmationAvailability(canInitialize)")
    invalidate = source.index("function invalidateOwnerReview()", sync)
    body = source[sync:invalidate]
    assert 'const checkbox = byId("owner-confirm-checkbox");' in body
    assert "setDisabledWithFocusFallback(checkbox, reviewDisabled);" in body
    assert "reviewDisabled || checkbox.checked !== true" in body

    listener = source.index(
        'byId("owner-confirm-checkbox").addEventListener("change", () => {'
    )
    preview = source.index('byId(328).addEventListener("click", async () => {', listener)
    listener_body = source[listener:preview]
    assert "syncOwnerConfirmationAvailability(" in listener_body
    assert "latestState.owner.can_initialize" in listener_body

    mark = source.index("function markOwnerReviewFresh(epoch)")
    request_id = source.index("function requestId()", mark)
    mark_body = source[mark:request_id]
    checked_false = mark_body.index('byId("owner-confirm-checkbox").checked = false;')
    resync = mark_body.index("syncOwnerConfirmationAvailability(", checked_false)
    assert checked_false < resync


def test_any_owner_contract_edit_invalidates_prior_confirmation() -> None:
    source = _source()

    invalidate = source.index("function invalidateOwnerReview()")
    request_id = source.index("function requestId()", invalidate)
    body = source[invalidate:request_id]
    assert "ownerReviewEpoch += 1;" in body
    assert "ownerReviewFresh = false;" in body
    assert 'byId("owner-confirm-checkbox").checked = false;' in body
    assert 'document.querySelectorAll("[data-owner-field]").forEach((node) => {' in source
    assert 'node.addEventListener("input", invalidateOwnerReview);' in source
    assert 'node.addEventListener("change", invalidateOwnerReview);' in source
    assert 'byId(327).addEventListener("change", invalidateOwnerReview);' in source

    # Changing strategy or the bound research plan invalidates the backend review
    # workspace/context too, so the presentation freshness fence must follow it.
    strategy = source.index('byId(106).addEventListener("change", () => {')
    assert source.index("invalidateOwnerReview();", strategy) < source.index(
        'dispatch("strategy.set"', strategy
    )
    plan = source.index('byId(107).addEventListener("click", () => {')
    assert source.index("invalidateOwnerReview();", plan) < source.index(
        'dispatch("research_plan.select"', plan
    )


def test_stale_preview_response_cannot_restore_confirmation_after_newer_edit() -> None:
    source = _source()

    mark = source.index("function markOwnerReviewFresh(epoch)")
    request_id = source.index("function requestId()", mark)
    body = source[mark:request_id]
    assert "if (epoch !== ownerReviewEpoch) return false;" in body
    assert "ownerReviewFresh = true;" in body
    assert "return true;" in body

    preview = source.index('byId(328).addEventListener("click", async () => {')
    assert "const reviewEpoch = ownerReviewEpoch;" in source[preview:]
    assert "markOwnerReviewFresh(reviewEpoch);" in source[preview:]


def test_initialize_requires_fresh_review_and_fresh_explicit_checkbox() -> None:
    source = _source()

    confirm = source.index('byId("owner-confirm").addEventListener("click", async () => {')
    guard = source.index(
        'ownerReviewFresh !== true || byId("owner-confirm-checkbox").checked !== true',
        confirm,
    )
    dispatch = source.index('dispatch("owner.initialize", {', confirm)
    assert confirm < guard < dispatch
    assert "confirmed: true," in source[dispatch:]

    completed = source.index('result && result.status === "completed"', dispatch)
    invalidated = source.index("invalidateOwnerReview();", completed)
    assert dispatch < completed < invalidated


def test_preview_always_clears_checkbox_even_if_previous_review_was_confirmed() -> None:
    source = _source()

    mark = source.index("function markOwnerReviewFresh(epoch)")
    checked_false = source.index(
        'byId("owner-confirm-checkbox").checked = false;',
        mark,
    )
    sync = source.index("syncOwnerConfirmationAvailability(", checked_false)
    assert mark < checked_false < sync

    # A fresh preview enables the controls but never auto-confirms the new contract.
    body = source[mark:source.index("function requestId()", mark)]
    assert ".checked = true" not in body


def test_owner_backend_mutations_are_disabled_while_any_worker_is_busy() -> None:
    source = _source()

    helper = source.index("function isAnyWorkerBusy(state)")
    sync = source.index("function syncOwnerConfirmationAvailability(canInitialize)", helper)
    helper_body = source[helper:sync]
    assert "state.busy" in helper_body
    assert "Object.values(state.busy).some(Boolean)" in helper_body

    render = source.index("function renderState(state)")
    preview_gate = source.index("setDisabledWithFocusFallback(", render)
    preview_control = source.index("byId(328)", preview_gate)
    preview_busy = source.index("isAnyWorkerBusy(state)", preview_control)
    confirmation_sync = source.index(
        "syncOwnerConfirmationAvailability(state.owner.can_initialize);",
        preview_busy,
    )
    assert render < preview_control < preview_busy < confirmation_sync

    sync_body = source[sync:source.index("function invalidateOwnerReview()", sync)]
    assert "isAnyWorkerBusy(latestState)" in sync_body
    assert "setDisabledWithFocusFallback(checkbox, reviewDisabled);" in sync_body
    assert "reviewDisabled || checkbox.checked !== true" in sync_body

    # Busy only gates backend-mutating owner actions. The form remains available
    # for inspection/editing while canonical owner.can_initialize permits it.
    owner_controls = source.index(
        'document.querySelectorAll("[data-owner-field], #327")',
        render,
    )
    assert owner_controls < preview_control
    owner_slice = source[owner_controls:preview_control]
    assert "isAnyWorkerBusy" not in owner_slice
