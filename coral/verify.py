"""Verify a published rate line against the ledger.

One check: does this line match a jointly signed record that covers its date?
For percent-of-charges rates the amount recomputes from the signed chargemaster
version named in the record; the caller supplies the gross charge from that
version.
"""
from __future__ import annotations

from dataclasses import dataclass

from .chain import Chain, RateRecord


@dataclass
class Line:
    """One line from a provider MRF or a payer TiC file, normalized."""
    source: str          # "provider" or "payer"
    payerId: str
    planId: str
    providerId: str
    npi: str
    code: str
    modifier_: str
    setting: int
    billingClass: int
    unit: str
    amount_cents: int | None      # dollar amount when the file gives one
    percent_bp: int | None        # basis points when the file gives a percentage
    gross_charge_cents: int | None = None  # provider file only

    def key(self) -> tuple:
        return (self.payerId, self.planId, self.providerId, self.npi, self.code, self.modifier_,
                self.setting, self.billingClass, self.unit)


@dataclass
class Verdict:
    status: str   # "match", "amount_differs", "no_record", "expired", "ambiguous", "needs_chargemaster"
    record: RateRecord | None
    expected_cents: int | None
    published_cents: int | None


def expected_amount(rec: RateRecord, gross_charge_cents: int | None, reference_cents: int | None) -> int | None:
    r = rec.rate
    if r.formula == 0:
        return r.value
    if r.formula == 1:
        if gross_charge_cents is None:
            return None
        return round(gross_charge_cents * r.value / 10000)
    if r.formula == 2:
        if reference_cents is None:
            return None
        return round(reference_cents * r.value / 10000)
    return None


def verify_line(chain, line: Line, at: int, reference_cents: int | None = None) -> Verdict:
    """`chain` is a Chain (one RPC lookup per line) or a RateIndex (loaded once). Both expose find_rate and any_for."""
    k = line.key()
    recs = chain.find_rate(k, at)
    if not recs:
        # distinguish "never signed" from "signed but window closed"
        return Verdict("expired" if chain.any_for(k) else "no_record", None, None, line.amount_cents)
    if len(recs) > 1:
        return Verdict("ambiguous", None, None, line.amount_cents)
    rec = recs[0]
    if rec.rate.formula == 1 and line.percent_bp is not None:
        status = "match" if line.percent_bp == rec.rate.value else "amount_differs"
        return Verdict(status, rec, None, None)
    exp = expected_amount(rec, line.gross_charge_cents, reference_cents)
    if exp is None:
        return Verdict("needs_chargemaster", rec, None, line.amount_cents)
    if line.amount_cents is None:
        return Verdict("amount_differs", rec, exp, None)
    return Verdict("match" if exp == line.amount_cents else "amount_differs", rec, exp, line.amount_cents)
