"""Deploy RateRegistry to the dev network and set up prototype keys.

Prototype key handling only: keys are written to build/keys.json in clear.
In a deployment, root keys live in each entity's HSM and signing keys in the
contract-management system that produces the rate sheet. See NOTES.md, "Keys".

Usage: .venv/bin/python scripts/bootstrap.py [--rpc http://127.0.0.1:8545]
"""
import argparse
import json
import sys
import time
from pathlib import Path

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, DEPLOY_FILE  # noqa: E402

# Besu dev-network account funded in besu/ibftConfigFile.json. Gas is free (min-gas-price 0),
# the balance exists only because web3 checks it.
RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"

ENTITIES = [
    # id, kind, display name
    ("TIN:12-3456789", "provider", "Example Health System (hospital, NPI 1234567893)"),
    ("TIN:98-7654321", "provider", "Example Orthopedic Group (professional, NPI 1987654321)"),
    ("PAYER:UHC", "payer", "UnitedHealthcare"),
    ("PAYER:AETNA", "payer", "Aetna"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:8545")
    args = ap.parse_args()

    ch = Chain.rpc(args.rpc, RELAYER_KEY)
    registrar = Account.from_key(RELAYER_KEY).address  # CMS runs the relayer and is the registrar
    addr = ch.deploy(registrar)
    ch.save_deployment()
    print(f"RateRegistry at {addr} on chain {ch.w3.eth.chain_id}")

    keys = {"registrar": {"address": registrar, "key": RELAYER_KEY}, "entities": {}}
    now = int(time.time())
    for eid, kind, name in ENTITIES:
        root, signer = Account.create(), Account.create()
        ch.register_entity(eid, kind, root.address, registrar)
        ch.delegate(root.key, eid, signer.address, now - 60)
        keys["entities"][eid] = {
            "kind": kind, "name": name,
            "root": {"address": root.address, "key": root.key.hex()},
            "signer": {"address": signer.address, "key": signer.key.hex()},
        }
        print(f"registered {eid:18} root {root.address} signer {signer.address}")
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    print(f"keys written to {KEYS_FILE} (prototype only)")


if __name__ == "__main__":
    main()
