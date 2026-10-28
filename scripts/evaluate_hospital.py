"""Sign real hospital MRF lines as ledger records and measure cost and verification.

Input: one CMS v2/v3 tall CSV. Picks one payer/plan, takes the first N lines with
a dollar amount, commits each as a jointly signed rate (prototype keys for both
sides), then verifies the same lines and a tampered copy against the ledger.

The payer signature here is produced by a prototype key held in build/keys.json.
It shows the mechanism and its cost; it is not the payer's agreement.

Usage:
  .venv/bin/python scripts/evaluate_hospital.py data/raw/<file>.csv \
      --provider TIN:84-0398876 --npi 1790787307 --payer-name "United Healthcare" \
      --plan-name "Broad Networks" --payer PAYER:UHC --plan PLAN:UHC-BROAD -n 200
"""
import argparse
import json
import random
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, RateIndex  # noqa: E402
from coral.mrf import hospital_csv  # noqa: E402
from coral.typed import Rate, sign_rate  # noqa: E402
from coral.verify import verify_line  # noqa: E402

RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--npi", required=True)
    ap.add_argument("--payer-name", required=True)
    ap.add_argument("--plan-name", required=True)
    ap.add_argument("--payer", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--tamper", type=float, default=0.10, help="share of lines to alter in the tampered copy")
    ap.add_argument("--out", type=Path, default=Path("results/evaluate_hospital.json"))
    ap.add_argument("--skip-commit", action="store_true", help="records already on the ledger from an earlier run")
    args = ap.parse_args()

    keys = json.loads((Path(__file__).resolve().parent.parent / "build" / "keys.json").read_text())
    ch = Chain.rpc(relayer_key=RELAYER_KEY)
    registrar = keys["registrar"]["address"]
    now = int(time.time())

    # register the real hospital TIN and the payer if the bootstrap did not
    for eid, kind in ((args.provider, "provider"), (args.payer, "payer")):
        if ch.entity(eid) is None:
            from eth_account import Account
            root, signer = Account.create(), Account.create()
            ch.register_entity(eid, kind, root.address, registrar)
            ch.delegate(root.key, eid, signer.address, now - 60)
            keys["entities"][eid] = {"kind": kind, "name": eid, "root": {"address": root.address, "key": root.key.hex()},
                                     "signer": {"address": signer.address, "key": signer.key.hex()}}
            (Path(__file__).resolve().parent.parent / "build" / "keys.json").write_text(json.dumps(keys, indent=2))
    pkey = keys["entities"][args.provider]["signer"]["key"]
    qkey = keys["entities"][args.payer]["signer"]["key"]

    t0 = time.time()
    pm = {(args.payer_name, args.plan_name): (args.payer, args.plan)}
    lines, seen = [], set()
    for ln in hospital_csv(args.csv, args.provider, args.npi, pm):
        if ln.amount_cents is None:
            continue
        k = (ln.code, ln.modifier_, ln.setting)
        if k in seen:
            continue  # the file repeats a code across rows; one signed record per key
        seen.add(k)
        lines.append(ln)
        if len(lines) >= args.n:
            break
    parse_s = time.time() - t0
    print(f"parsed {len(lines)} distinct dollar lines for {args.payer_name}/{args.plan_name} in {parse_s:.1f}s")

    # commit
    gas, t1 = [], time.time()
    start, end = now - 30 * 86400, now + 335 * 86400
    for ln in ([] if args.skip_commit else lines):
        r = Rate(args.payer, args.plan, args.provider, args.npi, ln.code, ln.modifier_, ln.setting, 0, ln.amount_cents,
                 start, end, "FILE-2025-11-01", "")
        rcpt = ch._send(ch.contract.functions.commitRate(r.tuple(), sign_rate(pkey, ch.domain, r), sign_rate(qkey, ch.domain, r)))
        gas.append(rcpt.gasUsed)
    commit_s = time.time() - t1
    if gas:
        print(f"committed {len(lines)} records in {commit_s:.1f}s, {commit_s/len(lines):.2f}s each, gas mean {sum(gas)/len(gas):.0f}")
    else:
        gas = [r.gasUsed for r in ch.commit_receipts(args.provider)]
        print(f"skipped commit; gas from {len(gas)} earlier receipts, mean {sum(gas)/len(gas):.0f}")

    # verify untouched copy
    t2 = time.time()
    idx = RateIndex(ch, provider_id=args.provider, payer_id=args.payer)
    load_s = time.time() - t2
    print(f"loaded {len(idx.records)} records into the index in {load_s:.1f}s")
    clean = {}
    for ln in lines:
        v = verify_line(idx, ln, now)
        clean[v.status] = clean.get(v.status, 0) + 1
    verify_s = time.time() - t2
    print(f"verify clean: {clean} in {verify_s:.1f}s")

    # verify tampered copy
    random.seed(1)
    tampered, truth = {}, {"altered": 0}
    for ln in lines:
        t = ln
        if random.random() < args.tamper:
            t = replace(ln, amount_cents=round(ln.amount_cents * random.choice([0.9, 1.1, 1.5, 0.5])))
            truth["altered"] += 1
        v = verify_line(idx, t, now)
        tampered[v.status] = tampered.get(v.status, 0) + 1
    print(f"verify tampered ({truth['altered']} altered): {tampered}")

    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(json.dumps({
        "file": args.csv.name, "payer": args.payer_name, "plan": args.plan_name, "records": len(lines),
        "parse_s": parse_s, "commit_s": commit_s, "commit_s_per_record": commit_s / len(lines),
        "gas_mean": sum(gas) / len(gas), "gas_min": min(gas), "gas_max": max(gas),
        "index_load_s": load_s, "verify_s": verify_s, "verify_s_per_line": verify_s / len(lines),
        "clean": clean, "tampered": tampered, "altered": truth["altered"],
    }, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
