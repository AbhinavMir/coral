"""Pair the same five provider files with the same payer file on provider and code only.

This is the pairing a comparison without the record key can make: every provider line for a code
meets every distinct payer value for the same provider and code, whatever the plan, setting,
billing class, or drug unit. Reported beside the full-key pairing of evaluate_pairs.py so the
difference between the two measures is measured, not inferred.

Usage:
  .venv/bin/python scripts/evaluate_pairs_coarse.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coral.match import Pair, measure  # noqa: E402
from coral.verify import Line  # noqa: E402
from evaluate_pairs import PROVIDERS, provider_lines  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def coarse_pairs(provider: list[Line], payer: list[Line]) -> list[Pair]:
    by_code = defaultdict(dict)
    for ln in payer:
        by_code[(ln.providerId, ln.code)][(ln.amount_cents, ln.percent_bp)] = ln
    out = []
    for h in provider:
        for p in by_code.get((h.providerId, h.code), {}).values():
            out.append(Pair((h.providerId, h.code), h, p))
    return out


def main():
    pay = [Line(**d) for d in json.loads((ROOT / "results" / "pairs_payer_lines.json").read_text())["lines"]]
    out, everything = {}, []
    for rel, tin, name, pairs in PROVIDERS:
        lines, dated, _ = provider_lines(ROOT / rel, tin, pairs)
        ps = coarse_pairs(lines, pay)
        everything += ps
        out[tin] = dict(measure(ps).row(), provider_lines=len(lines))
        print(f"{name}: {len(lines)} lines, {len(ps)} coarse pairs, {out[tin]}")
    s = measure(everything)
    out["all_providers"] = s.row()
    out["all_providers"]["exact_pct_of_all_pairs"] = round(100 * s.exact / (s.pairs or 1), 1)
    out["all_providers"]["within_10_pct_of_all_pairs"] = round(100 * s.within_10 / (s.pairs or 1), 1)
    print("all:", out["all_providers"])
    (ROOT / "results" / "evaluate_pairs_coarse.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
