"""Pair provider lines with payer lines and measure agreement.

Reproduces the Trilliant Health comparison on any provider/payer pair: share of
paired lines that match exactly, fall within 10 percent, or differ by more than
50 percent. After signing, each paired line is also checked against the ledger
so a remaining difference is attributed to the file that departs from the
signed record.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .verify import Line, Verdict


def key(ln: Line) -> tuple:
    return (ln.payerId, ln.planId, ln.providerId, ln.npi, ln.code, ln.modifier_, ln.setting)


@dataclass
class Pair:
    key: tuple
    provider: Line
    payer: Line

    @property
    def ratio(self) -> float | None:
        a, b = self.provider.amount_cents, self.payer.amount_cents
        if a is None or b is None or b == 0:
            return None
        return a / b


@dataclass
class Stats:
    pairs: int = 0
    exact: int = 0
    within_10: int = 0
    over_50: int = 0
    no_amount: int = 0
    attribution: dict = field(default_factory=lambda: defaultdict(int))

    def row(self) -> dict:
        n = self.pairs or 1
        return {
            "pairs": self.pairs,
            "exact_pct": round(100 * self.exact / n, 1),
            "within_10_pct": round(100 * self.within_10 / n, 1),
            "over_50_pct": round(100 * self.over_50 / n, 1),
            "no_amount": self.no_amount,
            "attribution": dict(self.attribution),
        }


def pair_lines(provider: list[Line], payer: list[Line]) -> list[Pair]:
    by_key = defaultdict(list)
    for ln in payer:
        by_key[key(ln)].append(ln)
    out = []
    for h in provider:
        for p in by_key.get(key(h), []):
            out.append(Pair(key(h), h, p))
    return out


def measure(pairs: list[Pair], verdicts: dict[tuple, tuple[Verdict, Verdict]] | None = None) -> Stats:
    """verdicts: pair key -> (provider verdict, payer verdict) from coral.verify, when the ledger is in use."""
    s = Stats()
    for p in pairs:
        s.pairs += 1
        r = p.ratio
        if r is None:
            s.no_amount += 1
        else:
            if p.provider.amount_cents == p.payer.amount_cents:
                s.exact += 1
            if abs(r - 1) <= 0.10:
                s.within_10 += 1
            if abs(r - 1) > 0.50:
                s.over_50 += 1
        if verdicts and p.key in verdicts:
            hv, pv = verdicts[p.key]
            if hv.status == "match" and pv.status == "match":
                s.attribution["both_match_record"] += 1
            elif hv.status == "match":
                s.attribution["payer_file_departs"] += 1
            elif pv.status == "match":
                s.attribution["provider_file_departs"] += 1
            elif hv.status == pv.status == "no_record":
                s.attribution["unsigned"] += 1
            else:
                s.attribution[f"{hv.status}/{pv.status}"] += 1
    return s
