from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from threading import RLock
from typing import Iterable
from weakref import WeakKeyDictionary


class PromotionEvidenceError(ValueError):
    """Raised when champion/challenger evidence is invalid or ambiguous."""


@dataclass(frozen=True, slots=True)
class PairedLoss:
    evaluation_id: str
    champion_loss: float
    challenger_loss: float

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_id, str) or not self.evaluation_id.strip():
            raise PromotionEvidenceError("evaluation_id must be a non-empty string")
        object.__setattr__(self, "evaluation_id", self.evaluation_id.strip())
        object.__setattr__(
            self, "champion_loss", _finite_nonnegative(self.champion_loss, "champion_loss")
        )
        object.__setattr__(
            self,
            "challenger_loss",
            _finite_nonnegative(self.challenger_loss, "challenger_loss"),
        )


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    min_pairs: int
    min_effective_pairs: int
    min_mean_improvement: float
    min_win_rate: float
    alpha: float
    tie_tolerance: float

    def __post_init__(self) -> None:
        for name in ("min_pairs", "min_effective_pairs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PromotionEvidenceError(f"{name} must be a positive integer")
        object.__setattr__(
            self,
            "min_mean_improvement",
            _finite_nonnegative(self.min_mean_improvement, "min_mean_improvement"),
        )
        object.__setattr__(
            self,
            "tie_tolerance",
            _finite_nonnegative(self.tie_tolerance, "tie_tolerance"),
        )
        object.__setattr__(self, "min_win_rate", _unit_interval(self.min_win_rate, "min_win_rate"))
        alpha = _real(self.alpha, "alpha")
        if not 0.0 < alpha < 1.0:
            raise PromotionEvidenceError("alpha must be in (0, 1)")
        object.__setattr__(self, "alpha", alpha)


@dataclass(frozen=True, slots=True, init=False, eq=False, weakref_slot=True)
class PromotionDecision:
    _promote: bool
    pair_count: int
    effective_pair_count: int
    challenger_wins: int
    champion_wins: int
    ties: int
    mean_improvement: float
    win_rate: float | None
    one_sided_sign_test_p_value: float | None
    reasons: tuple[str, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise PromotionEvidenceError(
            "PromotionDecision is evaluator-issued; call evaluate_promotion"
        )

    @property
    def promote(self) -> bool:
        _require_issued_decision(self)
        return self._promote


_DecisionSnapshot = tuple[
    bool,
    int,
    int,
    int,
    int,
    int,
    float,
    float | None,
    float | None,
    tuple[str, ...],
]
_ISSUED_DECISIONS: WeakKeyDictionary[PromotionDecision, _DecisionSnapshot] = WeakKeyDictionary()
_ISSUED_DECISIONS_LOCK = RLock()


def evaluate_promotion(
    pairs: Iterable[PairedLoss],
    policy: PromotionPolicy,
) -> PromotionDecision:
    """Evaluate matched champion/challenger losses with a fail-closed promotion gate.

    Loss must be lower-is-better. The same ``evaluation_id`` may appear only once;
    duplicate paired evidence would otherwise inflate the denominator and is rejected.
    Ties within ``tie_tolerance`` are excluded from the exact sign test and win rate,
    but remain in the mean-improvement calculation and total-pair sufficiency check.

    Policy selection is deliberately outside this evaluator: callers must supply a
    ``PromotionPolicy``. Unique evaluation IDs prevent duplicate counting but do not
    prove that evaluation units are statistically independent. The sign test assumes
    independence across effective pairs; callers testing multiple challengers are
    responsible for any required multiplicity control.

    The returned decision is process-local evaluator-issued evidence. Copying,
    constructing, or mutating it cannot mint positive promotion authority. Durable
    restart/deployment authority remains a separate product integration boundary.
    """

    if not isinstance(policy, PromotionPolicy):
        raise PromotionEvidenceError("policy must be PromotionPolicy")

    try:
        iterator = iter(pairs)
    except TypeError as exc:
        raise PromotionEvidenceError("pairs must be iterable") from exc

    seen: set[str] = set()
    improvements: list[float] = []
    pair_count = 0
    challenger_wins = 0
    champion_wins = 0
    ties = 0

    for index, row in enumerate(iterator):
        if not isinstance(row, PairedLoss):
            raise PromotionEvidenceError(f"pairs[{index}] must be PairedLoss")
        if row.evaluation_id in seen:
            raise PromotionEvidenceError(f"duplicate evaluation_id: {row.evaluation_id}")
        seen.add(row.evaluation_id)
        pair_count += 1
        improvement = row.champion_loss - row.challenger_loss
        if not math.isfinite(improvement):
            raise PromotionEvidenceError("loss difference overflowed")
        improvements.append(improvement)
        if improvement > policy.tie_tolerance:
            challenger_wins += 1
        elif improvement < -policy.tie_tolerance:
            champion_wins += 1
        else:
            ties += 1

    effective = challenger_wins + champion_wins
    try:
        improvement_sum = math.fsum(improvements)
    except OverflowError as exc:
        raise PromotionEvidenceError("mean improvement overflowed") from exc
    if not math.isfinite(improvement_sum):
        raise PromotionEvidenceError("mean improvement overflowed")
    mean_improvement = improvement_sum / pair_count if pair_count else 0.0
    win_rate = challenger_wins / effective if effective else None
    p_value = _one_sided_sign_test_p_value(challenger_wins, effective) if effective else None

    reasons: list[str] = []
    if pair_count < policy.min_pairs:
        reasons.append("insufficient_total_pairs")
    if effective < policy.min_effective_pairs:
        reasons.append("insufficient_effective_pairs")
    if mean_improvement < policy.min_mean_improvement:
        reasons.append("mean_improvement_below_threshold")
    if win_rate is None or win_rate < policy.min_win_rate:
        reasons.append("win_rate_below_threshold")
    if p_value is None or p_value > policy.alpha:
        reasons.append("sign_test_not_significant")

    return _issue_decision(
        promote=not reasons,
        pair_count=pair_count,
        effective_pair_count=effective,
        challenger_wins=challenger_wins,
        champion_wins=champion_wins,
        ties=ties,
        mean_improvement=mean_improvement,
        win_rate=win_rate,
        one_sided_sign_test_p_value=p_value,
        reasons=tuple(reasons),
    )


def _issue_decision(
    *,
    promote: bool,
    pair_count: int,
    effective_pair_count: int,
    challenger_wins: int,
    champion_wins: int,
    ties: int,
    mean_improvement: float,
    win_rate: float | None,
    one_sided_sign_test_p_value: float | None,
    reasons: tuple[str, ...],
) -> PromotionDecision:
    decision = object.__new__(PromotionDecision)
    object.__setattr__(decision, "_promote", promote)
    object.__setattr__(decision, "pair_count", pair_count)
    object.__setattr__(decision, "effective_pair_count", effective_pair_count)
    object.__setattr__(decision, "challenger_wins", challenger_wins)
    object.__setattr__(decision, "champion_wins", champion_wins)
    object.__setattr__(decision, "ties", ties)
    object.__setattr__(decision, "mean_improvement", mean_improvement)
    object.__setattr__(decision, "win_rate", win_rate)
    object.__setattr__(decision, "one_sided_sign_test_p_value", one_sided_sign_test_p_value)
    object.__setattr__(decision, "reasons", reasons)
    snapshot = _decision_snapshot(decision)
    with _ISSUED_DECISIONS_LOCK:
        _ISSUED_DECISIONS[decision] = snapshot
    return decision


def _decision_snapshot(decision: PromotionDecision) -> _DecisionSnapshot:
    return (
        decision._promote,
        decision.pair_count,
        decision.effective_pair_count,
        decision.challenger_wins,
        decision.champion_wins,
        decision.ties,
        decision.mean_improvement,
        decision.win_rate,
        decision.one_sided_sign_test_p_value,
        decision.reasons,
    )


def _require_issued_decision(decision: PromotionDecision) -> None:
    try:
        current = _decision_snapshot(decision)
    except (AttributeError, TypeError) as exc:
        raise PromotionEvidenceError("promotion decision is incomplete") from exc
    with _ISSUED_DECISIONS_LOCK:
        issued = _ISSUED_DECISIONS.get(decision)
    if issued is None or issued != current:
        raise PromotionEvidenceError("promotion decision is not intact evaluator-issued evidence")


def _one_sided_sign_test_p_value(wins: int, effective: int) -> float:
    if wins < 0 or effective < 0 or wins > effective:
        raise PromotionEvidenceError("invalid sign-test counts")
    if wins == 0:
        return 1.0

    # Exact integer arithmetic is cheap and fully accurate while 2**n remains
    # representable as a normal finite float. Beyond that, sum the shorter
    # binomial tail from a log-PMF boundary to avoid giant integers and avoid
    # starting recurrences at 2**(-n), which would underflow for large cohorts.
    if effective <= 1023:
        numerator = sum(math.comb(effective, k) for k in range(wins, effective + 1))
        return math.ldexp(float(numerator), -effective)

    if wins > effective / 2:
        term = _binomial_half_pmf(effective, wins)
        if term == 0.0:
            return math.nextafter(0.0, 1.0)
        total = term
        for k in range(wins, effective):
            term *= (effective - k) / (k + 1)
            total += term
        return min(1.0, max(math.nextafter(0.0, 1.0), total))

    boundary = wins - 1
    term = _binomial_half_pmf(effective, boundary)
    lower_tail = term
    for k in range(boundary, 0, -1):
        term *= k / (effective - k + 1)
        lower_tail += term
    return min(1.0, max(0.0, 1.0 - lower_tail))


def _binomial_half_pmf(n: int, k: int) -> float:
    log_p = (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        - n * math.log(2.0)
    )
    return math.exp(log_p)


def _real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PromotionEvidenceError(f"{name} must be a real number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise PromotionEvidenceError(f"{name} must be a finite real number") from exc
    if not math.isfinite(result):
        raise PromotionEvidenceError(f"{name} must be finite")
    return result


def _finite_nonnegative(value: object, name: str) -> float:
    result = _real(value, name)
    if result < 0.0:
        raise PromotionEvidenceError(f"{name} must be >= 0")
    return result


def _unit_interval(value: object, name: str) -> float:
    result = _real(value, name)
    if not 0.0 <= result <= 1.0:
        raise PromotionEvidenceError(f"{name} must be in [0, 1]")
    return result
