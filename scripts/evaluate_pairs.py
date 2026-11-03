"""Pair provider price files with one payer file on the 16 Trilliant codes, then run
the synthetic attribution experiment on the ledger.

Part 1, real data: for each provider file, take its Aetna commercial lines for the
16 codes; from the Aetna Transparency in Coverage file, take the prices for the
same NPIs and codes; pair on the record key; report exact, within 10 percent, and
more than 50 percent apart (the Trilliant measure). The plan crosswalk is manual
and listed in PROVIDERS and PLAN_ID below.

Part 2, synthetic: sign one record per paired key with the provider's amount
(prototype keys for both sides), then alter 10 percent of payer lines and 10
percent of provider lines and verify both copies against the ledger. Each
remaining difference is attributed to the side whose line departs from the
signed record. This shows the mechanism; the signatures are not the parties'.

Usage:
  .venv/bin/python scripts/evaluate_pairs.py data/raw/tic/<aetna>.json.gz
"""
import argparse
import json
import random
import sys
import time
from dataclasses import replace, asdict
from pathlib import Path

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, RateIndex, DEPLOY_FILE  # noqa: E402
from coral.match import pair_lines, measure, key as line_key  # noqa: E402
from coral.mrf import provider_csv, provider_header, payer_json, Skipped  # noqa: E402
from coral.typed import Rate  # noqa: E402
from coral.verify import Line, verify_line  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"
RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"
PAYER = "PAYER:AETNA"
PLAN_ID = "Aetna commercial"   # crosswalk label used on both sides

# Trilliant Health, 30 April 2026: 8 MS-DRGs, 5 GI CPTs, 3 orthopedic CPTs.
CODES = {
    "inpatient": [f"MS-DRG:{c}" for c in (189, 190, 193, 280, 291, 392, 552, 641)],
    "gi": [f"CPT:{c}" for c in (43239, 45380, 45385, 47562, 49650)],
    "ortho": [f"CPT:{c}" for c in (29827, 29881, 29916)],
}
ALL_CODES = {c for g in CODES.values() for c in g}

# provider file, ledger id, name, NPIs as listed in the file header, (payer_name, plan_name) rows that are Aetna commercial
PROVIDERS = [
    ("data/raw/840398876_yampa-valley-medical-center_standardcharges.csv", "TIN:84-0398876",
     "UCHealth Yampa Valley Medical Center", {("Aetna", "Commercial")}),
    ("data/raw/others/460796114-1144397134_uchmhs_standardcharges.csv", "TIN:46-0796114",
     "UCHealth Memorial Hospital Central", {("Aetna Core/Meritain", "Commercial")}),
    ("data/raw/others/362169147_advocate-christ-medical-center_standardcharges.csv", "TIN:36-2169147",
     "Advocate Christ Medical Center", {("Aetna", "Commercial")}),
    ("data/raw/others/931176109_Oregon-Health-and-Science-University_standardcharges.csv", "TIN:93-1176109",
     "Oregon Health & Science University", {("AETNA HEALTHCARE", "AETNA")}),
    ("data/raw/others/940562680-1295181477_alta-bates-summit-medical-center-summit-campus_standardcharges.csv", "TIN:94-0562680",
     "Alta Bates Summit Medical Center, Summit campus", {("Aetna", "HMO/PPO")}),
]


def provider_lines(path: Path, tin: str, pairs: set) -> tuple[list[Line], str, list[str]]:
    hdr = provider_header(path)
    npis = [n.strip() for n in hdr["type_2_npi"].replace("|", ",").split(",") if n.strip()]
    payer_names = {p for p, _ in pairs}
    plans = {q for _, q in pairs}
    out = []
    for ln in provider_csv(path, tin, npis[0], {p: PAYER for p in payer_names}, plans):
        if ln.code in ALL_CODES:
            out.append(replace(ln, planId=PLAN_ID))
    return out, hdr["last_updated_on"], npis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("payer_file", type=Path)
    ap.add_argument("--out", type=Path, default=Path("results/evaluate_pairs.json"))
    ap.add_argument("--payer-cache", type=Path, default=Path("results/pairs_payer_lines.json"))
    args = ap.parse_args()

    # provider side
    prov: dict[str, list[Line]] = {}
    npi_to_tin, canonical = {}, {}
    meta = {}
    for rel, tin, name, pairs in PROVIDERS:
        lines, dated, npis = provider_lines(ROOT / rel, tin, pairs)
        prov[tin] = lines
        canonical[tin] = npis[0]
        for n in npis:
            npi_to_tin[n] = tin
        meta[tin] = {"name": name, "file": Path(rel).name, "dated": dated, "npis": npis, "lines": len(lines),
                     "codes_present": len({ln.code for ln in lines})}
        print(f"{name}: {len(lines)} Aetna commercial lines on {meta[tin]['codes_present']} of 16 codes")

    # payer side (one pass over the file; cached)
    skipped = Skipped()
    if args.payer_cache.exists():
        pay = [Line(**d) for d in json.loads(args.payer_cache.read_text())["lines"]]
        print(f"payer lines from cache: {len(pay)}")
    else:
        t0 = time.time()
        pay = []
        for ln in payer_json(args.payer_file, PAYER, PLAN_ID, npi_to_tin, skipped=skipped, codes_of_interest=ALL_CODES):
            pay.append(replace(ln, npi=canonical[ln.providerId]))
        print(f"payer file: {len(pay)} lines for our NPIs on the 16 codes in {time.time()-t0:.0f}s; skipped {dict(skipped.counts)}")
        args.payer_cache.parent.mkdir(exist_ok=True)
        args.payer_cache.write_text(json.dumps({"file": args.payer_file.name, "skipped": dict(skipped.counts),
                                                "lines": [asdict(l) for l in pay]}))
    # several payer prices per key (different negotiated_type or service codes) collapse to the set of distinct amounts
    pay_by_key: dict[tuple, dict] = {}
    for ln in pay:
        pay_by_key.setdefault(line_key(ln), {})[(ln.amount_cents, ln.percent_bp)] = ln
    pay_unique = [ln for d in pay_by_key.values() for ln in d.values()]

    # Part 1: real comparison
    real = {}
    for tin, lines in prov.items():
        pairs = pair_lines(lines, pay_unique)
        by_group = {}
        for g, codes in CODES.items():
            ps = [p for p in pairs if p.provider.code in codes]
            by_group[g] = measure(ps).row()
        real[tin] = {"pairs": len(pairs), "provider_keys": len({line_key(l) for l in lines}),
                     "payer_keys_for_provider": len({k for k in pay_by_key if k[2] == tin}),
                     "all": measure(pairs).row(), "groups": by_group}
        print(f"{meta[tin]['name']}: {len(pairs)} pairs, {real[tin]['all']}")
    all_pairs = [p for lines in prov.values() for p in pair_lines(lines, pay_unique)]
    real["all_providers"] = measure(all_pairs).row()
    print(f"all providers: {real['all_providers']}")

    # Part 2: synthetic attribution on the ledger
    keys = json.loads(KEYS_FILE.read_text())
    ch = Chain.rpc(relayer_key=RELAYER_KEY)
    registrar = keys["registrar"]["address"]
    now = int(time.time())
    for rel, tin, name, _ in PROVIDERS:
        if ch.entity(tin) is None:
            root, signer = Account.create(), Account.create()
            ch.register_entity(tin, "provider", root.address, registrar)
            ch.delegate(root.key, tin, signer.address, now - 60)
            keys["entities"][tin] = {"kind": "provider", "name": name, "npi": canonical[tin],
                                     "root": {"address": root.address, "key": root.key.hex()},
                                     "signer": {"address": signer.address, "key": signer.key.hex()}}
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    qkey = keys["entities"][PAYER]["signer"]["key"]

    # one record per paired key: the provider's published amount is taken as the agreed value
    agreed: dict[tuple, Line] = {}
    for p in all_pairs:
        if p.provider.amount_cents is not None:
            agreed.setdefault(p.key, p.provider)
    idx = RateIndex(ch, payer_id=PAYER)
    start = int(time.mktime(time.strptime("2025-11-01", "%Y-%m-%d")))
    committed = 0
    for k, ln in agreed.items():
        if idx.any_for(k):
            continue
        r = Rate(ln.payerId, ln.planId, ln.providerId, ln.npi, ln.code, ln.modifier_, ln.setting, ln.billingClass,
                 ln.unit, 0, ln.amount_cents, start, 0, f"example agreement, {ln.providerId}", "")
        ch.commit_rate_with_keys(r, keys["entities"][ln.providerId]["signer"]["key"], qkey)
        committed += 1
    print(f"signed {committed} records for {len(agreed)} paired keys")
    idx = RateIndex(ch, payer_id=PAYER)
    at = start + 86400

    random.seed(7)
    prov_copy, pay_copy, truth = {}, {}, {"provider_altered": 0, "payer_altered": 0, "both_altered": 0}
    for k, ln in agreed.items():
        h = ln
        q = replace(ln, source="payer")      # the agreed value on the payer side
        a, b = random.random() < 0.10, random.random() < 0.10
        if a:
            h = replace(h, amount_cents=round(h.amount_cents * random.choice([0.9, 1.1, 1.5, 0.5])))
        if b:
            q = replace(q, amount_cents=round(q.amount_cents * random.choice([0.9, 1.1, 1.5, 0.5])))
        truth["provider_altered"] += a and not b
        truth["payer_altered"] += b and not a
        truth["both_altered"] += a and b
        prov_copy[k], pay_copy[k] = h, q
    verdicts = {k: (verify_line(idx, prov_copy[k], at), verify_line(idx, pay_copy[k], at)) for k in agreed}
    pairs = pair_lines(list(prov_copy.values()), list(pay_copy.values()))
    synthetic = measure(pairs, verdicts).row()
    print(f"synthetic: {truth} -> {synthetic}")

    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(json.dumps({
        "payer_file": args.payer_file.name, "plan_crosswalk": PLAN_ID, "codes": CODES,
        "providers": meta, "payer_lines": len(pay), "payer_lines_unique": len(pay_unique),
        "payer_skipped": dict(skipped.counts), "real": real,
        "synthetic": {"keys": len(agreed), "records_signed": committed, "truth": truth, "result": synthetic},
    }, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
