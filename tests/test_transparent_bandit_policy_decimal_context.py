from decimal import Decimal, localcontext

from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


def test_policy_choice_is_independent_of_process_decimal_precision():
    policy = BanditPolicyState(
        environment_id="d" * 64,
        protocol_id="protocol-determinism-v1",
        config_sha256="c" * 64,
        seed=17,
        generation=0,
        estimates=(
            ActionEstimate("A", 3, Decimal("0.999999999999999999999999999999999999")),
            ActionEstimate("B", 3, Decimal("1.000000000000000000000000000000000000")),
        ),
    )

    with localcontext() as context:
        context.prec = 1
        low_precision_choice = policy.choose(admissible_actions=frozenset({"A", "B"}))

    with localcontext() as context:
        context.prec = 50
        high_precision_choice = policy.choose(admissible_actions=frozenset({"A", "B"}))

    assert low_precision_choice == "B"
    assert high_precision_choice == "B"
