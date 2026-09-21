import pytest

from autosport.champion_challenger_gate import (
    PairedLoss,
    PromotionEvidenceError,
    PromotionPolicy,
    evaluate_promotion,
)


def rows(champion, challenger):
    return [
        PairedLoss(str(i), c, h)
        for i, (c, h) in enumerate(zip(champion, challenger, strict=True))
    ]


def policy(**overrides):
    values = {
        "min_pairs": 1,
        "min_effective_pairs": 1,
        "min_mean_improvement": 0.0,
        "min_win_rate": 0.5,
        "alpha": 0.05,
        "tie_tolerance": 0.0,
    }
    values.update(overrides)
    return PromotionPolicy(**values)


def test_promotes_only_when_all_gates_pass():
    evidence = rows([1.0] * 10, [0.0] * 9 + [2.0])
    decision = evaluate_promotion(
        evidence,
        policy(
            min_pairs=10,
            min_effective_pairs=10,
            min_mean_improvement=0.5,
            min_win_rate=0.8,
            alpha=0.05,
        ),
    )
    assert decision.promote is True
    assert decision.challenger_wins == 9
    assert decision.champion_wins == 1
    assert decision.ties == 0
    assert decision.mean_improvement == pytest.approx(0.8)
    assert decision.win_rate == pytest.approx(0.9)
    assert decision.one_sided_sign_test_p_value == pytest.approx(11 / 1024)
    assert decision.reasons == ()


def test_exact_sign_test_is_one_sided_upper_tail():
    decision = evaluate_promotion(
        rows([1.0] * 5, [0.0] * 4 + [2.0]),
        policy(min_pairs=5, min_effective_pairs=5, alpha=1.0 - 1e-12),
    )
    assert decision.one_sided_sign_test_p_value == pytest.approx((5 + 1) / 32)


def test_ties_excluded_from_sign_test_but_in_total_and_mean():
    evidence = [
        PairedLoss("a", 1.0, 0.0),
        PairedLoss("b", 1.0, 1.0),
        PairedLoss("c", 1.0, 1.0),
    ]
    decision = evaluate_promotion(
        evidence,
        policy(min_pairs=3, min_effective_pairs=1, alpha=0.9),
    )
    assert decision.effective_pair_count == 1
    assert decision.ties == 2
    assert decision.mean_improvement == pytest.approx(1 / 3)
    assert decision.one_sided_sign_test_p_value == pytest.approx(0.5)


def test_tie_tolerance_is_symmetric_and_inclusive():
    evidence = [
        PairedLoss("a", 1.125, 1.0),
        PairedLoss("b", 1.0, 1.125),
        PairedLoss("c", 1.1250001, 1.0),
    ]
    decision = evaluate_promotion(
        evidence,
        policy(min_pairs=3, min_effective_pairs=1, tie_tolerance=0.125, alpha=0.9),
    )
    assert decision.ties == 2
    assert decision.challenger_wins == 1
    assert decision.champion_wins == 0


def test_empty_evidence_fails_closed_with_explicit_reasons():
    decision = evaluate_promotion([], policy(min_pairs=2, min_effective_pairs=1))
    assert decision.promote is False
    assert decision.win_rate is None
    assert decision.one_sided_sign_test_p_value is None
    assert set(decision.reasons) == {
        "insufficient_total_pairs",
        "insufficient_effective_pairs",
        "win_rate_below_threshold",
        "sign_test_not_significant",
    }


def test_duplicate_evaluation_ids_are_rejected():
    with pytest.raises(PromotionEvidenceError, match="duplicate evaluation_id"):
        evaluate_promotion(
            [PairedLoss("same", 1.0, 0.9), PairedLoss("same", 1.1, 0.8)],
            policy(min_pairs=2, min_effective_pairs=1),
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, True, "1"])
def test_invalid_losses_rejected(bad):
    with pytest.raises(PromotionEvidenceError):
        PairedLoss("x", bad, 0.0)


@pytest.mark.parametrize("bad", ["", "   ", None, 3])
def test_invalid_evaluation_id_rejected(bad):
    with pytest.raises(PromotionEvidenceError):
        PairedLoss(bad, 1.0, 0.0)


def test_production_policy_has_no_implicit_thresholds():
    with pytest.raises(TypeError):
        PromotionPolicy()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_pairs": 0},
        {"min_pairs": True},
        {"min_mean_improvement": -0.1},
        {"min_win_rate": 1.1},
        {"alpha": 0.0},
        {"alpha": 1.0},
        {"tie_tolerance": -1e-6},
    ],
)
def test_invalid_policy_rejected(kwargs):
    with pytest.raises(PromotionEvidenceError):
        policy(**kwargs)


def test_generator_input_is_consumed_once():
    evidence = (PairedLoss(str(i), 1.0, 0.0) for i in range(6))
    decision = evaluate_promotion(
        evidence,
        policy(min_pairs=6, min_effective_pairs=6, alpha=0.1),
    )
    assert decision.pair_count == 6
    assert decision.promote is True


def test_non_pair_row_rejected_without_coercion():
    with pytest.raises(PromotionEvidenceError, match="must be PairedLoss"):
        evaluate_promotion([("x", 1.0, 0.0)], policy(min_pairs=1, min_effective_pairs=1))


def test_mean_improvement_threshold_can_block_significant_win_rate():
    evidence = [PairedLoss(str(i), 1.0, 0.999999) for i in range(20)]
    decision = evaluate_promotion(
        evidence,
        policy(
            min_pairs=20,
            min_effective_pairs=20,
            min_mean_improvement=0.001,
            alpha=0.05,
        ),
    )
    assert decision.one_sided_sign_test_p_value < 0.05
    assert decision.promote is False
    assert "mean_improvement_below_threshold" in decision.reasons


def test_win_rate_threshold_can_block_mean_improvement_driven_by_outlier():
    evidence = [PairedLoss(str(i), 1.0, 2.0) for i in range(6)] + [PairedLoss("big", 100.0, 0.0)]
    decision = evaluate_promotion(
        evidence,
        policy(
            min_pairs=7,
            min_effective_pairs=7,
            min_mean_improvement=1.0,
            min_win_rate=0.5,
            alpha=1.0 - 1e-12,
        ),
    )
    assert decision.mean_improvement > 1.0
    assert decision.win_rate == pytest.approx(1 / 7)
    assert decision.promote is False
    assert "win_rate_below_threshold" in decision.reasons


def test_evaluation_ids_are_trimmed_before_duplicate_detection():
    with pytest.raises(PromotionEvidenceError, match="duplicate evaluation_id"):
        evaluate_promotion(
            [PairedLoss("sample", 1.0, 0.9), PairedLoss(" sample ", 1.1, 0.8)],
            policy(min_pairs=2, min_effective_pairs=1),
        )


def test_large_sign_test_remains_finite_positive_for_extreme_upper_tail():
    evidence = [PairedLoss(str(i), 1.0, 0.0) for i in range(2000)]
    decision = evaluate_promotion(
        evidence,
        policy(min_pairs=2000, min_effective_pairs=2000, alpha=0.05),
    )
    assert decision.promote is True
    assert decision.one_sided_sign_test_p_value is not None
    assert 0.0 < decision.one_sided_sign_test_p_value < 0.05


def test_large_sign_test_lower_half_tail_is_near_one_without_underflow_failure():
    evidence = [PairedLoss(str(i), 1.0, 0.0 if i < 400 else 2.0) for i in range(2000)]
    decision = evaluate_promotion(
        evidence,
        policy(min_pairs=2000, min_effective_pairs=2000, min_win_rate=0.0, alpha=0.999999),
    )
    assert decision.one_sided_sign_test_p_value is not None
    assert 0.999 < decision.one_sided_sign_test_p_value <= 1.0
    assert decision.promote is False
    assert "sign_test_not_significant" in decision.reasons


def test_policy_is_required_and_none_fails_closed():
    with pytest.raises(PromotionEvidenceError, match="policy must be PromotionPolicy"):
        evaluate_promotion([PairedLoss("x", 1.0, 0.0)], None)  # type: ignore[arg-type]


def test_wrong_policy_type_rejected_explicitly():
    with pytest.raises(PromotionEvidenceError, match="policy must be PromotionPolicy"):
        evaluate_promotion(
            [PairedLoss("x", 1.0, 0.0)],
            {"min_pairs": 1},  # type: ignore[arg-type]
        )


def test_huge_integer_loss_that_cannot_be_float_is_rejected_in_domain():
    with pytest.raises(PromotionEvidenceError, match="finite real number"):
        PairedLoss("x", 10**10000, 0.0)


def test_loss_difference_overflow_fails_closed():
    with pytest.raises(PromotionEvidenceError, match="mean improvement overflowed"):
        evaluate_promotion(
            [PairedLoss("x", 1.7e308, 0.0), PairedLoss("y", 1.7e308, 0.0)],
            policy(min_pairs=2, min_effective_pairs=1),
        )
