"""Commit the ten example rates in data/examples.json to the running network.

Signs each rate with both prototype keys from build/keys.json and signs the
chargemaster version first, so the percent-of-charges rates name a signed
reference. Run after scripts/bootstrap.py.

Usage: .venv/bin/python scripts/seed_examples.py [--rpc http://127.0.0.1:8545]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, DEPLOY_FILE  # noqa: E402
from coral.typed import FORMULA, SETTING, ChargemasterVersion, Rate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "data" / "examples.json"
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"


def day(s: str) -> int:
    return int(time.mktime(time.strptime(s, "%Y-%m-%d")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:8545")
    args = ap.parse_args()
    ex = json.loads(EXAMPLES.read_text())
    keys = json.loads(KEYS_FILE.read_text())
    ch = Chain.rpc(args.rpc, keys["registrar"]["key"])
    pkey = keys["entities"][ex["providerId"]]["signer"]["key"]

    cm = ex["chargemaster"]
    cm_id = ch.commit_chargemaster(
        ChargemasterVersion(ex["providerId"], cm["version"], bytes.fromhex(cm["sha256"]), day(cm["effective"])), pkey)
    print(f"chargemaster {cm['version']} -> {cm_id.hex()[:16]}")

    for r in ex["rates"]:
        payer_id = ex["payers"][r["payer"]]
        qkey = keys["entities"][payer_id]["signer"]["key"]
        rate = Rate(
            payerId=payer_id, planId=r["plan"], providerId=ex["providerId"], npi=ex["npi"],
            code=r["code"], modifier_="", setting=SETTING[r["setting"]], formula=FORMULA[r["formula"]],
            value=round(r["value"] * 100), start=day(ex["start"]), end=0,
            contractVersion=ex["contractVersion"][r["payer"]],
            referenceVersion="" if r["formula"] == "fixed" else cm["version"],
        )
        rid = ch.commit_rate_with_keys(rate, pkey, qkey)
        print(f"{r['code']:12} {r['setting']:10} {r['payer']}/{r['plan']:18} {r['value']:>10.2f} -> {rid.hex()[:16]}")


if __name__ == "__main__":
    main()
