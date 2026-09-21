from __future__ import annotations

from autosport.champion_challenger_gate import (
    PairedLoss,
    PromotionPolicy,
    evaluate_promotion,
)


def test_positive_promotion_requires_canonical_multiplicity_authority() -> None:
    """A caller-selected per-comparison alpha is not product multiplicity authority.

    PR #1109 currently has enough local statistics to return promote=True from only
    paired losses plus PromotionPolicy.  Autosport already has a durable canonical
    multiplicity/sequential plane in research_multiplicity.py.  A production-positive
    promotion must resolve that authority for the exact comparison family rather than
    treating the caller's alpha as proof that repeated/multiple challenger testing was
    controlled.

    This is intentionally an expected-red falsifier on the parent head.  The parent
    owner may compose the existing authority directly or introduce a narrow canonical
    wrapper; this test does not prescribe a second multiplicity registry.
    """

    evidence = tuple(
        PairedLoss(
            evaluation_id=f"eval-{index:02d}",
            champion_loss=1.0,
            challenger_loss=0.0,
        )
        for index in range(20)
    )
    policy = PromotionPolicy(
        min_pairs=20,
        min_effective_pairs=20,
        min_mean_improvement=0.5,
        min_win_rate=0.95,
        alpha=0.00001,
        tie_tolerance=0.0,
    )

    decision = evaluate_promotion(evidence, policy)

    # Prove the local comparison clears every statistical gate.  The only reason a
    # canonical promotion must remain false is the missing product-owned multiplicity
    # authority for this exact challenger/family.
    assert decision.pair_count == 20
    assert decision.effective_pair_count == 20
    assert decision.challenger_wins == 20
    assert decision.champion_wins == 0
    assert decision.ties == 0
    assert decision.mean_improvement == 1.0
    assert decision.win_rate == 1.0
    assert decision.one_sided_sign_test_p_value is not None
    assert decision.one_sided_sign_test_p_value < policy.alpha

    assert decision.promote is False
    assert any("multiplicity" in reason.lower() for reason in decision.reasons)
