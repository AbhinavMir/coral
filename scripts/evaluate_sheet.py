"""Sign a whole provider rate sheet and measure cost, then verify the sheet against the ledger.

Input: one CMS tall CSV. Every payer in the file is registered (prototype keys for
both sides). Every line with a percentage becomes a percent-of-charges record that
names the signed chargemaster version (the row's dollar column is that percent of
the row's gross charge); every other line with a dollar amount becomes a fixed
record; lines with only an algorithm carry nothing a record can hold and are counted.
One record per key: a second line with the same key and a different value is a key
collision and is counted, not signed.

Both signatures come from prototype keys held in build/keys.json. The run measures
mechanism and cost. It does not show that the file is correct.

Usage:
  .venv/bin/python scripts/evaluate_sheet.py data/raw/<file>.csv --provider TIN:84-0398876 \
      --batch 250 --inflight 6 [--limit N] [--skip-commit]
"""
import argparse
import json
import multiprocessing as mp
import random
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, RateIndex, DEPLOY_FILE  # noqa: E402
from coral.mrf import provider_csv, provider_header, Skipped, _rows  # noqa: E402
from coral.typed import Rate, sign_rate, domain  # noqa: E402
from coral.verify import verify_line  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"
RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"

_DOM = None


def _sign_pair(args):
    rate, pkey, qkey = args
    return sign_rate(pkey, _DOM, rate), sign_rate(qkey, _DOM, rate)


def _init(dom):
    global _DOM
    _DOM = dom


def slug(name: str) -> str:
    return "PAYER:" + re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:26]


def besu_bytes() -> int:
    """Bytes on disk of the CMS validator's data directory."""
    out = subprocess.run(["du", "-sk", str(ROOT / "besu" / "data" / "cms")], capture_output=True, text=True).stdout
    return int(out.split()[0]) * 1024 if out else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--batch", type=int, default=250, help="records per transaction")
    ap.add_argument("--inflight", type=int, default=6, help="transactions sent before waiting for receipts")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many records (0 = all)")
    ap.add_argument("--tamper", type=float, default=0.01)
    ap.add_argument("--skip-commit", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("results/evaluate_sheet.json"))
    args = ap.parse_args()

    keys = json.loads(KEYS_FILE.read_text())
    ex = json.loads((ROOT / "data" / "examples.json").read_text())
    ch = Chain.rpc(relayer_key=RELAYER_KEY)
    registrar = keys["registrar"]["address"]
    now = int(time.time())
    hdr = provider_header(args.csv)
    npi = hdr["type_2_npi"].strip()
    print(f"{hdr.get('hospital_name') or hdr.get('location_name')} · {hdr['last_updated_on']} · v{hdr['version']} · NPI {npi}")

    # register every payer named in the file (prototype keys)
    names = set()
    for row in _rows(args.csv):
        names.add(row["payer_name"].strip())
    known = {e["name"]: eid for eid, e in keys["entities"].items() if e["kind"] == "payer"}
    for name in sorted(names):
        if name in known:
            continue
        eid = slug(name)
        root, signer = Account.create(), Account.create()
        ch.register_entity(eid, "payer", root.address, registrar)
        ch.delegate(root.key, eid, signer.address, now - 60)
        keys["entities"][eid] = {"kind": "payer", "name": name, "npi": "",
                                 "root": {"address": root.address, "key": root.key.hex()},
                                 "signer": {"address": signer.address, "key": signer.key.hex()}}
        known[name] = eid
        print(f"registered {eid} for '{name}'")
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    pkey = keys["entities"][args.provider]["signer"]["key"]

    # parse the whole sheet
    t0 = time.time()
    skipped = Skipped()
    lines = list(provider_csv(args.csv, args.provider, npi, known, skipped=skipped))
    parse_s = time.time() - t0
    cm_version = ex["chargemaster"]["version"]
    start, end = int(time.mktime(time.strptime(ex["start"], "%Y-%m-%d"))), 0
    on_ledger = {rec.rate.key() for rec in RateIndex(ch, provider_id=args.provider).records}
    rates, first_value, collisions, algorithm_only, already = [], {}, 0, 0, 0
    for ln in lines:
        # a percent-of-charges row also carries the dollar result; the contract term is the percent
        if ln.percent_bp is not None:
            formula, value, ref = 1, ln.percent_bp, cm_version
        elif ln.amount_cents is not None:
            formula, value, ref = 0, ln.amount_cents, ""
        else:
            algorithm_only += 1
            continue
        k = ln.key()
        if k in first_value:
            if first_value[k] != (formula, value):
                collisions += 1
            continue
        first_value[k] = (formula, value)
        if k in on_ledger:
            already += 1   # the example records seeded earlier
            continue
        rates.append(Rate(ln.payerId, ln.planId, ln.providerId, ln.npi, ln.code, ln.modifier_, ln.setting,
                          ln.billingClass, ln.unit, formula, value, start, end,
                          f"example agreement, {ln.payerId}", ref))
    if args.limit:
        rates = rates[:args.limit]
    n_fixed = sum(1 for r in rates if r.formula == 0)
    n_pct = len(rates) - n_fixed
    print(f"parsed {len(lines)} lines in {parse_s:.1f}s: {len(rates)} records ({n_fixed} fixed, {n_pct} percent), "
          f"{algorithm_only} algorithm-only lines, {collisions} key collisions, {already} already on the ledger, skipped {dict(skipped.counts)}")

    result = {
        "file": args.csv.name, "provider": args.provider, "npi": npi, "lines": len(lines), "parse_s": parse_s,
        "records": len(rates), "fixed": n_fixed, "percent": n_pct, "algorithm_only": algorithm_only,
        "key_collisions": collisions, "already_on_ledger": already, "skipped": dict(skipped.counts), "payers": len(names),
        "batch": args.batch, "inflight": args.inflight,
    }

    if not args.skip_commit:
        # sign
        t1 = time.time()
        dom = domain(ch.w3.eth.chain_id, ch.contract.address)
        jobs = [(r, pkey, keys["entities"][r.payerId]["signer"]["key"]) for r in rates]
        with mp.Pool(initializer=_init, initargs=(dom,)) as pool:
            sigs = pool.map(_sign_pair, jobs, chunksize=500)
        sign_s = time.time() - t1
        print(f"signed {len(rates)} records twice in {sign_s:.1f}s ({2*len(rates)/sign_s:.0f} signatures/s)")

        # commit in batches, several transactions in flight
        disk0 = besu_bytes()
        t2 = time.time()
        gas, calldata, pending = [], 0, []
        fn = ch.contract.functions.commitRates
        for i in range(0, len(rates), args.batch):
            chunk = rates[i:i + args.batch]
            ps = [s[0] for s in sigs[i:i + args.batch]]
            qs = [s[1] for s in sigs[i:i + args.batch]]
            tx = fn([r.tuple() for r in chunk], ps, qs).transact({"from": ch.relayer})
            pending.append(tx)
            if len(pending) >= args.inflight:
                for h in pending:
                    rcpt = ch.w3.eth.wait_for_transaction_receipt(h, timeout=600)
                    if rcpt.status != 1:
                        raise RuntimeError(f"batch reverted: {h.hex()}")
                    gas.append(rcpt.gasUsed)
                    calldata += len(ch.w3.eth.get_transaction(h)["input"])
                pending = []
                done = min(i + args.batch, len(rates))
                el = time.time() - t2
                print(f"  {done}/{len(rates)} committed, {done/el:.0f} records/s, block {ch.w3.eth.block_number}", flush=True)
        for h in pending:
            rcpt = ch.w3.eth.wait_for_transaction_receipt(h, timeout=600)
            if rcpt.status != 1:
                raise RuntimeError(f"batch reverted: {h.hex()}")
            gas.append(rcpt.gasUsed)
            calldata += len(ch.w3.eth.get_transaction(h)["input"])
        commit_s = time.time() - t2
        disk1 = besu_bytes()
        print(f"committed {len(rates)} records in {commit_s:.1f}s: {len(rates)/commit_s:.0f} records/s, "
              f"{sum(gas)/len(rates):.0f} gas/record, {calldata/len(rates):.0f} calldata bytes/record, "
              f"disk +{(disk1-disk0)/1e6:.0f} MB on one validator")
        result.update({
            "sign_s": sign_s, "signatures_per_s": 2 * len(rates) / sign_s,
            "commit_s": commit_s, "records_per_s": len(rates) / commit_s, "transactions": len(gas),
            "gas_per_record": sum(gas) / len(rates), "gas_per_tx_mean": sum(gas) / len(gas),
            "calldata_bytes_per_record": calldata / len(rates), "calldata_bytes_total": calldata,
            "validator_disk_bytes_before": disk0, "validator_disk_bytes_after": disk1,
        })

    # verify: load the ledger once, then check every line of the file (not deduplicated)
    t3 = time.time()
    idx = RateIndex(ch, provider_id=args.provider)
    load_s = time.time() - t3
    print(f"loaded {len(idx.records)} records in {load_s:.1f}s")
    at = start + 86400
    t4 = time.time()
    clean, checked = {}, 0
    for ln in lines:
        if ln.amount_cents is None and ln.percent_bp is None:
            continue
        v = verify_line(idx, ln, at)
        clean[v.status] = clean.get(v.status, 0) + 1
        checked += 1
    verify_s = time.time() - t4
    print(f"verify clean copy: {checked} lines, {clean} in {verify_s:.1f}s ({checked/verify_s:.0f} lines/s)")

    random.seed(1)
    tampered, altered, caught = {}, 0, 0
    for ln in lines:
        if ln.amount_cents is None and ln.percent_bp is None:
            continue
        t = ln
        hit = random.random() < args.tamper
        if hit:
            altered += 1
            f = random.choice([0.9, 1.1, 1.5, 0.5])
            t = replace(ln, amount_cents=round(ln.amount_cents * f)) if ln.amount_cents is not None \
                else replace(ln, percent_bp=round(ln.percent_bp * f))
        v = verify_line(idx, t, at)
        tampered[v.status] = tampered.get(v.status, 0) + 1
        if hit and v.status == "amount_differs":
            caught += 1
    print(f"verify tampered copy: {altered} altered, {caught} caught, {tampered}")

    result.update({
        "index_load_s": load_s, "verify_lines": checked, "verify_s": verify_s, "lines_per_s": checked / verify_s,
        "clean": clean, "tampered": tampered, "altered": altered, "caught": caught,
    })
    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
