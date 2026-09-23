from __future__ import annotations

from pathlib import Path


APP = Path(__file__).parents[1] / "src" / "autosport" / "windows_web" / "app.js"


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def test_owner_confirmation_is_disabled_until_latest_preview_completes() -> None:
    source = _source()

    assert "let ownerReviewFresh = false;" in source
    assert "function syncOwnerConfirmationAvailability(canInitialize)" in source
    assert "canInitialize !== true || ownerReviewFresh !== true" in source
    assert 'setDisabledWithFocusFallback(byId("owner-confirm-checkbox"), disabled);' in source
    assert 'setDisabledWithFocusFallback(byId("owner-confirm"), disabled);' in source
    assert "syncOwnerConfirmationAvailability(state.owner.can_initialize);" in source

    preview = source.index('byId(328).addEventListener("click", async () => {')
    preview_invalidate = source.index("invalidateOwnerReview();", preview)
    preview_dispatch = source.index('dispatch("owner.preview", {', preview)
    preview_fresh = source.index("markOwnerReviewFresh();", preview)
    assert preview < preview_invalidate < preview_dispatch < preview_fresh
    assert 'result && result.status === "completed"' in source[preview:preview_fresh]


def test_any_owner_contract_edit_invalidates_prior_confirmation() -> None:
    source = _source()

    assert "function invalidateOwnerReview()" in source
    assert "ownerReviewFresh = false;" in source
    assert 'byId("owner-confirm-checkbox").checked = false;' in source
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

    mark = source.index("function markOwnerReviewFresh()")
    checked_false = source.index(
        'byId("owner-confirm-checkbox").checked = false;',
        mark,
    )
    sync = source.index("syncOwnerConfirmationAvailability(", checked_false)
    assert mark < checked_false < sync

    # A fresh preview enables the controls but never auto-confirms the new contract.
    body = source[mark:source.index("function requestId()", mark)]
    assert ".checked = true" not in body
