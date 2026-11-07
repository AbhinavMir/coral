"""Sensitivity of the pair run to three choices: the plan crosswalk for provider B (its second Aetna
commercial product line, "Aetna ASA", is a leased-network product and is left out of the main run),
repeat provider lines (same key and value), and the cent precision of the payer file.

Writes results/pairs_sensitivity.json. Needs no ledger.
Usage:
  .venv/bin/python scripts/pairs_sensitivity.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coral.match import pair_lines, pair_keys, measure, key as line_key  # noqa: E402
from coral.verify import Line  # noqa: E402
from evaluate_pairs import PROVIDERS, provider_lines  # noqa: E402
from evaluate_pairs_coarse import coarse_pairs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
B = "TIN:46-0796114"


def payer_unique():
    pay = [Line(**d) for d in json.loads((ROOT / "results" / "pairs_payer_lines.json").read_text())["lines"]]
    by = {}
    for ln in pay:
        by.setdefault(line_key(ln), {})[(ln.amount_cents, ln.percent_bp)] = ln
    return [ln for d in by.values() for ln in d.values()]


def lines_for(extra_b: set):
    out = []
    for rel, tin, _, pairs in PROVIDERS:
        ps = pairs | extra_b if tin == B else pairs
        out += provider_lines(ROOT / rel, tin, ps)[0]
    return out


def dedupe(lines):
    seen, out = set(), []
    for ln in lines:
        k = (line_key(ln), ln.amount_cents, ln.percent_bp)
        if k not in seen:
            seen.add(k)
            out.append(ln)
    return out


def within_cents(pairs, cents=5):
    return sum(1 for p in pairs if p.values is not None and abs(p.values[0] - p.values[1]) <= cents
               and p.provider.amount_cents is not None)


def main():
    pay = payer_unique()
    base = lines_for(set())
    with_asa = lines_for({("Aetna ASA", "Commercial")})
    base_dedup = dedupe(base)
    out = {
        "base": {"lines": len(base), "all_values": measure(pair_lines(base, pay)).row(),
                 "nearest": measure(pair_keys(base, pay)).row(), "coarse": measure(coarse_pairs(base, pay)).row()},
        "b_with_asa": {"lines": len(with_asa), "all_values": measure(pair_lines(with_asa, pay)).row(),
                       "nearest": measure(pair_keys(with_asa, pay)).row(), "coarse": measure(coarse_pairs(with_asa, pay)).row()},
        "base_dedup": {"lines": len(base_dedup), "all_values": measure(pair_lines(base_dedup, pay)).row(),
                       "nearest": measure(pair_keys(base_dedup, pay)).row()},
        "within_5_cents": {"all_values": within_cents(pair_lines(base, pay)), "nearest": within_cents(pair_keys(base, pay))},
    }
    for k, v in out.items():
        print(k, json.dumps(v))
    (ROOT / "results" / "pairs_sensitivity.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
