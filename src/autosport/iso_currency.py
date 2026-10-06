from __future__ import annotations

"""Canonical product authority for accepted current ISO 4217 alphabetic codes.

The set is intentionally centralized so economic authorities do not silently fall
back to three-letter shape checks.  It tracks ISO 4217 List One current currency
and fund/metal codes needed by Autosport as of 2026-01-01; withdrawn codes are not
accepted as new positive authority.
"""


class ISO4217CurrencyError(ValueError):
    """Raised when a value is not a current canonical ISO 4217 alphabetic code."""


# ISO 4217 Maintenance Agency (SIX) List One, current alphabetic codes.
# BGN, ANG, and SLL are deliberately absent after their withdrawals; XCG and
# XAD are included following amendments 176 and 179 respectively. Sierra Leone's
# current code is SLE, not the withdrawn SLL.
ISO_4217_CURRENT_CODES = frozenset(
    """
AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD
CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP
GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR
KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN
MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR
SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD
USN UYI UYU UYW UZS VED VES VND VUV WST XAD XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF
XPT XSU XTS XUA XXX YER ZAR ZMW ZWG
""".split()
)


def require_iso_4217_currency(value: object) -> str:
    """Return *value* iff it is an exact current ISO 4217 alphabetic code."""

    if type(value) is not str or value not in ISO_4217_CURRENT_CODES:
        raise ISO4217CurrencyError(
            "currency must be a current canonical ISO 4217 alphabetic code"
        )
    return value
