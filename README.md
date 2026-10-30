# CORAL: Committed Ratesheet on Ledger

A permissioned ledger on which a provider and a payer jointly sign each
negotiated rate. Published price files cite the record id, so a
line that departs from the signed rate is detected by anyone with the file.

Paper and all working notes: `abstract.tex` (AMIA format).

## State (2026-10-30)

| Part | State |
|---|---|
| Contract `contracts/RateRegistry.sol` | done; 11 tests pass |
| Besu IBFT 2.0 network, 4 validators | done; `besu/docker-compose.yml` |
| Python client, EIP-712 signing | done; `coral/` |
| Provider MRF parser | done; tested on one real 141 MB file |
| Payer TiC parser | written; not yet run on a real file |
| Web app: provider/payer and CMS views | done; `app/` |
| Evaluation on real provider lines | 200 records; `results/evaluate_provider.json` |
| Payer-side real data | not started |
| Paper body | not started |

## Run

```
python3 -m venv .venv && .venv/bin/pip install web3 eth-account eth-tester py-evm py-solc-x ijson fastapi uvicorn python-multipart pytest
.venv/bin/python -m pytest tests            # in-memory EVM
cd besu && docker compose up -d && cd ..    # 4 Besu validators, RPC on :8545
.venv/bin/python scripts/bootstrap.py       # deploy, register UCHealth Yampa Valley, United Healthcare, Aetna; write build/keys.json
.venv/bin/python scripts/seed_examples.py   # sign the ten example rates in data/examples.json
.venv/bin/uvicorn app.server:app --port 8000
```

Open http://127.0.0.1:8000. `/party`: all negotiated rates on the ledger, with filters; pending, propose, chargemaster, keys. `/cms`: registry and verify a published file.

Evaluation on a real provider file:

```
.venv/bin/python scripts/evaluate_provider.py data/raw/<provider>_standardcharges.csv \
  --provider TIN:84-0398876 --npi 1790787307 --payer-name "United Healthcare" \
  --plan-name "Broad Networks" --payer PAYER:UHC --plan "Broad Networks" -n 200
```

Provider files are found through `https://<provider domain>/cms-hpt.txt`.

## Layout

```
contracts/RateRegistry.sol   registry: entities, delegations, revocations, rates, chargemaster versions
coral/typed.py               EIP-712 types and signing
coral/chain.py               ledger client (eth-tester or Besu RPC), RateIndex
coral/verify.py              one check: does a published line match a signed record
coral/mrf.py                 provider CSV and payer JSON parsers
coral/match.py               pair provider and payer lines, agreement statistics
app/                         FastAPI server and static pages
besu/                        genesis, validator keys, docker-compose
scripts/bootstrap.py         deploy and register demo entities
scripts/seed_examples.py     sign the ten example rates and the chargemaster hash
data/examples.json           ten rates copied from the UCHealth file; version labels are examples
scripts/evaluate_provider.py sign real lines, verify clean and tampered copies
tests/                       contract and verifier tests
results/                     evaluation output
```

Keys in `build/keys.json` are prototype keys written in clear. A deployment
keeps root keys in each entity's hardware module and signing keys in the system
that produces the rate sheet.
