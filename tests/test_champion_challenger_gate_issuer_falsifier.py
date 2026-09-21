from __future__ import annotations

import autosport.champion_challenger_gate as promotion_gate


def test_private_issuer_cannot_mint_positive_promotion_authority() -> None:
    """A module caller must not bypass evaluate_promotion to mint PROMOTE."""

    issuer = getattr(promotion_gate, "_issue_decision", None)
    if issuer is None:
        return

    try:
        decision = issuer(
            promote=True,
            pair_count=100,
            effective_pair_count=100,
            challenger_wins=100,
            champion_wins=0,
            ties=0,
            mean_improvement=1.0,
            win_rate=1.0,
            one_sided_sign_test_p_value=0.0,
            reasons=(),
        )
    except (promotion_gate.PromotionEvidenceError, TypeError):
        return

    try:
        observed = decision.promote
    except promotion_gate.PromotionEvidenceError:
        return

    assert observed is not True, (
        "caller-accessible _issue_decision minted intact positive promotion authority "
        "without evaluate_promotion"
    )
