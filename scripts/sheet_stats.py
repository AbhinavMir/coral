"""Count, for the full-sheet provider file, how rows with a value distribute over record keys.

Writes results/sheet_stats.json: percent-of-charges rows among rows with a value, keys that carry more
than one value, rows on those keys, and rows that repeat the key and value of an earlier row.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.mrf import provider_csv, provider_header, Skipped  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = ROOT / "build" / "keys.json"


def main(csv: Path, provider: str):
    keys = json.loads(KEYS_FILE.read_text())
    known = {e["name"]: eid for eid, e in keys["entities"].items() if e["kind"] == "payer"}
    npi = provider_header(csv)["type_2_npi"].strip()
    lines = list(provider_csv(csv, provider, npi, known, skipped=Skipped()))
    values, rows_on, n_pct, n_amt = defaultdict(set), defaultdict(int), 0, 0
    for ln in lines:
        if ln.percent_bp is not None:
            v, n_pct = (1, ln.percent_bp), n_pct + 1
        elif ln.amount_cents is not None:
            v, n_amt = (0, ln.amount_cents), n_amt + 1
        else:
            continue
        values[ln.key()].add(v)
        rows_on[ln.key()] += 1
    multi = [k for k, s in values.items() if len(s) > 1]
    out = {"file": csv.name, "lines": len(lines), "rows_with_value": n_pct + n_amt, "percent_rows": n_pct,
           "amount_rows": n_amt, "keys": len(values), "keys_with_several_values": len(multi),
           "rows_on_those_keys": sum(rows_on[k] for k in multi)}
    (ROOT / "results" / "sheet_stats.json").write_text(json.dumps(out, indent=2))
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
