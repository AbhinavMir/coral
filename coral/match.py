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
    return ln.key()


def values(h: Line, p: Line) -> tuple[int, int] | None:
    """The two values when both lines carry the same kind of value (dollars or percent), else None."""
    if h.amount_cents is not None and p.amount_cents is not None:
        return h.amount_cents, p.amount_cents
    if h.percent_bp is not None and p.percent_bp is not None:
        return h.percent_bp, p.percent_bp
    return None


@dataclass
class Pair:
    key: tuple
    provider: Line
    payer: Line

    @property
    def values(self) -> tuple[int, int] | None:
        return values(self.provider, self.payer)

    @property
    def ratio(self) -> float | None:
        v = self.values
        if v is None or v[1] == 0:
            return None
        return v[0] / v[1]


@dataclass
class Stats:
    pairs: int = 0
    comparable: int = 0
    exact: int = 0
    within_10: int = 0
    over_50: int = 0
    not_comparable: int = 0
    attribution: dict = field(default_factory=lambda: defaultdict(int))

    def row(self) -> dict:
        n = self.comparable or 1
        return {
            "pairs": self.pairs,
            "comparable": self.comparable,
            "exact_pct": round(100 * self.exact / n, 1),
            "within_10_pct": round(100 * self.within_10 / n, 1),
            "over_50_pct": round(100 * self.over_50 / n, 1),
            "not_comparable": self.not_comparable,
            "attribution": dict(self.attribution),
        }


def pair_lines(provider: list[Line], payer: list[Line]) -> list[Pair]:
    """Every provider line with every payer line of the same key."""
    by_key = defaultdict(list)
    for ln in payer:
        by_key[key(ln)].append(ln)
    out = []
    for h in provider:
        for p in by_key.get(key(h), []):
            out.append(Pair(key(h), h, p))
    return out


def pair_keys(provider: list[Line], payer: list[Line]) -> list[Pair]:
    """One pair per provider line: the comparable payer line nearest to it, else the first payer line of the key.
    A payer file that lists several plans under one crosswalk label gives several values per key."""
    by_key = defaultdict(list)
    for ln in payer:
        by_key[key(ln)].append(ln)
    out = []
    for h in provider:
        cands = by_key.get(key(h), [])
        if not cands:
            continue
        best = None
        for p in cands:
            r = Pair(key(h), h, p).ratio
            if r is not None and (best is None or abs(r - 1) < best[0]):
                best = (abs(r - 1), p)
        out.append(Pair(key(h), h, best[1] if best else cands[0]))
    return out


def measure(pairs: list[Pair], verdicts: dict[tuple, tuple[Verdict, Verdict]] | None = None) -> Stats:
    """verdicts: pair key -> (provider verdict, payer verdict) from coral.verify, when the ledger is in use."""
    s = Stats()
    for p in pairs:
        s.pairs += 1
        r = p.ratio
        if r is None:
            s.not_comparable += 1
        else:
            s.comparable += 1
            if p.values[0] == p.values[1]:
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
