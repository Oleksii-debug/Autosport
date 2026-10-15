from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from autosport.smarkets_commission_economics import (
    SmarketsCommissionBasis,
    SmarketsCommissionEconomics,
    SmarketsCommissionEconomicsError,
    SmarketsCommissionTier,
    SmarketsMarketSettlementEconomics,
    SmarketsSettledMatchedBetEconomics,
    SmarketsTierEvidence,
)

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
A = "a" * 64
B = "b" * 64
C = "c" * 64


def tier(kind: SmarketsCommissionTier, **kw):
    return SmarketsTierEvidence(
        account_id=kw.get("account_id", "acct-1"),
        tier=kind,
        effective_from=kw.get("effective_from", NOW - timedelta(days=1)),
        effective_until=kw.get("effective_until"),
        evidence_sha256=kw.get("evidence_sha256", A),
    )


def market(pnl: str, **kw):
    return SmarketsMarketSettlementEconomics(
        account_id=kw.get("account_id", "acct-1"),
        market_id=kw.get("market_id", "mkt-1"),
        currency=kw.get("currency", "GBP"),
        settled_at=kw.get("settled_at", NOW),
        net_market_pnl=Decimal(pnl),
        settlement_evidence_sha256=kw.get("digest", B),
    )


def bet(bet_id: str, pnl: str, digest: str, **kw):
    return SmarketsSettledMatchedBetEconomics(
        account_id=kw.get("account_id", "acct-1"),
        market_id=kw.get("market_id", "mkt-1"),
        bet_id=bet_id,
        currency=kw.get("currency", "GBP"),
        settled_at=kw.get("settled_at", NOW),
        gross_pnl=Decimal(pnl),
        settlement_evidence_sha256=digest,
    )


def test_standard_charges_two_percent_of_positive_market_net_only():
    out = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=tier(SmarketsCommissionTier.STANDARD),
        settlement=market("18.02"),
    )
    assert out.basis is SmarketsCommissionBasis.MARKET_NET_WINNINGS
    assert out.rate == Decimal("0.02")
    assert out.modeled_commission == Decimal("0.3604")
    assert out.provider_incurred_exact is False
    assert out.execution_authorized is False


@pytest.mark.parametrize("pnl", ["0", "-1", "-999.99"])
def test_standard_never_charges_losing_or_zero_market(pnl):
    out = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=tier(SmarketsCommissionTier.STANDARD),
        settlement=market(pnl),
    )
    assert out.modeled_commission == Decimal("0")


def test_pro_charges_one_percent_on_wins_and_losses_per_bet():
    out = SmarketsCommissionEconomics.assess_per_bet(
        tier_evidence=tier(SmarketsCommissionTier.PRO),
        bets=(bet("b1", "30", B), bet("b2", "-20", C)),
    )
    assert out.basis is SmarketsCommissionBasis.PER_SETTLED_MATCHED_BET_ABSOLUTE_PNL
    assert out.rate == Decimal("0.01")
    assert out.modeled_commission == Decimal("0.50")


def test_select_charges_three_percent_on_wins_and_losses_per_bet():
    out = SmarketsCommissionEconomics.assess_per_bet(
        tier_evidence=tier(SmarketsCommissionTier.SELECT),
        bets=(bet("b1", "30", B), bet("b2", "-20", C)),
    )
    assert out.rate == Decimal("0.03")
    assert out.modeled_commission == Decimal("1.50")


def test_standard_cannot_use_per_bet_basis():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_per_bet(
            tier_evidence=tier(SmarketsCommissionTier.STANDARD),
            bets=(bet("b1", "10", B),),
        )


@pytest.mark.parametrize("kind", [SmarketsCommissionTier.PRO, SmarketsCommissionTier.SELECT])
def test_pro_select_cannot_use_market_net_basis(kind):
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_standard(
            tier_evidence=tier(kind),
            settlement=market("10"),
        )


def test_tier_must_cover_settlement_time():
    evidence = tier(
        SmarketsCommissionTier.STANDARD,
        effective_from=NOW + timedelta(seconds=1),
    )
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_standard(tier_evidence=evidence, settlement=market("10"))


def test_tier_expiry_is_exclusive():
    evidence = tier(
        SmarketsCommissionTier.STANDARD,
        effective_until=NOW,
    )
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_standard(tier_evidence=evidence, settlement=market("10"))


def test_account_binding_is_exact():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_standard(
            tier_evidence=tier(SmarketsCommissionTier.STANDARD, account_id="acct-X"),
            settlement=market("10"),
        )


def test_per_bet_population_requires_one_exact_market_currency_and_time():
    rows = (bet("b1", "10", B), bet("b2", "-3", C, market_id="mkt-X"))
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_per_bet(
            tier_evidence=tier(SmarketsCommissionTier.PRO), bets=rows
        )


def test_duplicate_bet_identity_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_per_bet(
            tier_evidence=tier(SmarketsCommissionTier.PRO),
            bets=(bet("b1", "10", B), bet("b1", "-3", C)),
        )


def test_duplicate_settlement_digest_rejected_as_ambiguous():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_per_bet(
            tier_evidence=tier(SmarketsCommissionTier.PRO),
            bets=(bet("b1", "10", B), bet("b2", "-3", B)),
        )


def test_assessment_id_is_order_invariant_for_per_bet_population():
    evidence = tier(SmarketsCommissionTier.PRO)
    left = SmarketsCommissionEconomics.assess_per_bet(
        tier_evidence=evidence,
        bets=(bet("b1", "10", B), bet("b2", "-3", C)),
    )
    right = SmarketsCommissionEconomics.assess_per_bet(
        tier_evidence=evidence,
        bets=(bet("b2", "-3", C), bet("b1", "10", B)),
    )
    assert left.modeled_commission == right.modeled_commission
    assert left.settlement_evidence_sha256s == right.settlement_evidence_sha256s
    assert left.assessment_id == right.assessment_id


def test_decimal_context_does_not_change_calculation_or_identity():
    evidence = tier(SmarketsCommissionTier.SELECT)
    rows = (bet("b1", "123456789.123456789", B), bet("b2", "-0.000000001", C))
    baseline = SmarketsCommissionEconomics.assess_per_bet(tier_evidence=evidence, bets=rows)
    with localcontext() as ctx:
        ctx.prec = 3
        stressed = SmarketsCommissionEconomics.assess_per_bet(tier_evidence=evidence, bets=rows)
    assert stressed.modeled_commission == baseline.modeled_commission
    assert stressed.assessment_id == baseline.assessment_id


def test_signed_zero_has_stable_identity():
    evidence = tier(SmarketsCommissionTier.STANDARD)
    left = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=evidence, settlement=market("0")
    )
    right = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=evidence, settlement=market("-0")
    )
    assert left.modeled_commission == right.modeled_commission == Decimal("0")
    assert left.assessment_id == right.assessment_id


def test_api_setup_fee_is_explicitly_excluded():
    out = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=tier(SmarketsCommissionTier.STANDARD), settlement=market("100")
    )
    assert out.api_setup_fee_included is False
    assert SmarketsCommissionEconomics.api_terms_url.endswith("Smarkets-API-Access-Integration-T-Cs")


def test_terms_revision_is_bound():
    out = SmarketsCommissionEconomics.assess_standard(
        tier_evidence=tier(SmarketsCommissionTier.STANDARD), settlement=market("1")
    )
    assert out.terms_version == "4.14"
    assert out.terms_effective_date == "2026-08-14"


def test_nonfinite_decimal_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        market("NaN")


def test_ambiguous_scalar_types_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsMarketSettlementEconomics(
            account_id="acct-1",
            market_id="mkt-1",
            currency="GBP",
            settled_at=NOW,
            net_market_pnl=1.0,  # type: ignore[arg-type]
            settlement_evidence_sha256=B,
        )


def test_bad_digest_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        market("1", digest="xyz")


def test_timezone_naive_settlement_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        market("1", settled_at=NOW.replace(tzinfo=None))


def test_empty_per_bet_population_rejected():
    with pytest.raises(SmarketsCommissionEconomicsError):
        SmarketsCommissionEconomics.assess_per_bet(
            tier_evidence=tier(SmarketsCommissionTier.PRO), bets=()
        )
