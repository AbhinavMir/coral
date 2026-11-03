"""Exercise the key lifecycle on the running network and record what the registry did.

Steps, all on PAYER:AETNA's keys from build/keys.json:
  1. commit one rate with the current signing key
  2. try to revoke that key from before the current block (must revert)
  3. delegate a new signing key, revoke the old one from a time after the next block
  4. try to commit with the old key after that time (must revert)
  5. read authorizedAt for the old key at its record's commit time and after the revocation
  6. registrar proposes a new root; activation before the delay must revert; the current root vetoes

Usage: .venv/bin/python scripts/key_events.py
"""
import json
import sys
import time
from pathlib import Path

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coral.chain import Chain, DEPLOY_FILE  # noqa: E402
from coral.typed import Rate  # noqa: E402

KEYS_FILE = DEPLOY_FILE.parent / "keys.json"
RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"
ENTITY = "PAYER:AETNA"
PROVIDER = "TIN:84-0398876"


def attempt(fn):
    try:
        fn()
        return "succeeded"
    except Exception as e:  # revert reasons come back as custom error selectors
        return f"reverted ({type(e).__name__}: {str(e)[:60]})"


def main():
    keys = json.loads(KEYS_FILE.read_text())
    ch = Chain.rpc(relayer_key=RELAYER_KEY)
    e = keys["entities"][ENTITY]
    pkey = keys["entities"][PROVIDER]["signer"]["key"]
    root, old = e["root"], e["signer"]
    out = {}

    rate = Rate(ENTITY, "key-events", PROVIDER, keys["entities"][PROVIDER]["npi"], "CPT:99999", "", 2, 1, "",
                0, 1, int(time.time()) - 86400, 0, "key events", "")
    rid = ch.commit_rate_with_keys(rate, pkey, old["key"])
    committed_at = ch.get_rate(rid).committed_at
    out["record_committed_at"] = committed_at

    block_time = ch.w3.eth.get_block("latest").timestamp
    out["backdated_revocation"] = attempt(lambda: ch.revoke(root["key"], ENTITY, old["address"], block_time - 60))

    new = Account.create()
    effective = block_time + 10
    ch.delegate(root["key"], ENTITY, new.address, block_time - 1)
    ch.revoke(root["key"], ENTITY, old["address"], effective)
    out["revocation_effective"] = effective
    while ch.w3.eth.get_block("latest").timestamp < effective + 1:
        time.sleep(2)
    rate2 = Rate(ENTITY, "key-events", PROVIDER, keys["entities"][PROVIDER]["npi"], "CPT:99998", "", 2, 1, "",
                 0, 1, int(time.time()) - 86400, 0, "key events", "")
    out["commit_with_old_key_after_revocation"] = attempt(lambda: ch.commit_rate_with_keys(rate2, pkey, old["key"]))
    out["commit_with_new_key"] = attempt(lambda: ch.commit_rate_with_keys(rate2, pkey, new.key))
    out["old_key_authorized_at_commit_time"] = ch.authorized_at(ENTITY, old["address"], committed_at)
    out["old_key_authorized_now"] = ch.authorized_at(ENTITY, old["address"], ch.w3.eth.get_block("latest").timestamp)

    candidate = Account.create()
    ch.propose_root(ENTITY, candidate.address, keys["registrar"]["address"])
    out["activate_before_delay"] = attempt(lambda: ch.activate_root(ENTITY))
    ch.veto_root(root["key"], ENTITY, candidate.address)
    out["activate_after_veto"] = attempt(lambda: ch.activate_root(ENTITY))
    out["root_after"] = ch.entity(ENTITY)["root"]
    out["root_unchanged"] = out["root_after"] == root["address"]

    e["signer"] = {"address": new.address, "key": new.key.hex()}
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    Path("results").mkdir(exist_ok=True)
    Path("results/key_events.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
