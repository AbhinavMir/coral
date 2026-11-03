# CORAL: Committed Ratesheet on Ledger

A permissioned ledger on which a provider and a payer jointly sign each
negotiated rate. Published price files cite the record id, so a line that
departs from the signed rate is detected by anyone with the file.

Paper and all working notes: `abstract.tex` (AMIA format; body in `abstract.tex`,
notes in `notes.tex`, run numbers in `numbers.tex` written by `scripts/make_numbers.py`).

## State (2026-11-03)

| Part | State |
|---|---|
| Contract `contracts/RateRegistry.sol` | done; 16 tests pass; revocation bound to block time, root delay and veto, supersedes, overlap rejection, batch commit |
| Besu IBFT 2.0 network, 4 validators | done; `besu/docker-compose.yml` |
| Python client, EIP-712 signing | done; `coral/` |
| Provider MRF parser | done; unit and billing class in the key; run on five real files |
| Payer TiC parser | done; run on one real Aetna file (1.69 GB gz) |
| Web app: provider/payer and CMS views | done; `app/` |
| Full-sheet run (one provider, 20 payers) | done; `results/evaluate_sheet.json`: 262,535 records, 61 records/s, verify 363,690 lines in 1.3 s |
| Five providers vs Aetna on 16 codes, synthetic attribution | done; `results/evaluate_pairs.json`: 52 pairs, 33 comparable, 45.5% exact |
| Key lifecycle on the network | done; `results/key_events.json` |
| Paper | complete; body 8 pages, 23 references, notes appended; numbers from `numbers.tex` |

## Run

```
python3 -m venv .venv && .venv/bin/pip install web3 eth-account eth-tester py-evm py-solc-x ijson fastapi uvicorn python-multipart pytest
.venv/bin/python -m pytest tests            # in-memory EVM
cd besu && docker compose up -d && cd ..    # 4 Besu validators, RPC on :8545
.venv/bin/python scripts/bootstrap.py       # deploy, register UCHealth Yampa Valley, United Healthcare, Aetna; write build/keys.json
.venv/bin/python scripts/seed_examples.py   # sign the ten example rates in data/examples.json
.venv/bin/uvicorn app.server:app --port 8000
```

Open http://127.0.0.1:8000. `/party`: all negotiated rates on the ledger, one search box; pending, propose, chargemaster, keys. `/cms`: registry and verify a published file.

Evaluation:

```
.venv/bin/python scripts/evaluate_sheet.py data/raw/<provider>_standardcharges.csv --provider TIN:84-0398876 --batch 100 --inflight 8
.venv/bin/python scripts/evaluate_pairs.py data/raw/tic/<aetna in-network>.json.gz
.venv/bin/python scripts/key_events.py           # rotates the Aetna signing key in build/keys.json
.venv/bin/python scripts/make_numbers.py && pdflatex abstract.tex && pdflatex abstract.tex
```

Provider files are found through `https://<provider domain>/cms-hpt.txt`. Payer file indexes: see `notes.tex`, Data.

## Layout

```
contracts/RateRegistry.sol   registry: entities, delegations, revocations, root replacement, rates, chargemaster versions
coral/typed.py               EIP-712 types and signing
coral/chain.py               ledger client (eth-tester or Besu RPC), RateIndex
coral/verify.py              one check: does a published line match a signed record
coral/mrf.py                 provider CSV and payer JSON parsers; Skipped counts
coral/match.py               pair provider and payer lines, agreement statistics, attribution
app/                         FastAPI server and static pages
besu/                        genesis, validator keys, docker-compose
scripts/bootstrap.py         deploy and register demo entities
scripts/seed_examples.py     sign the ten example rates and the chargemaster hash
scripts/evaluate_sheet.py    sign a whole provider sheet; cost; verify clean and altered copies (--skip-commit: verify only)
scripts/evaluate_pairs.py    five providers vs one payer file on the 16 Trilliant codes; synthetic attribution
scripts/key_events.py        key lifecycle on the running network: revoke, rotate, root delay and veto
scripts/make_numbers.py      results/*.json -> numbers.tex
data/examples.json           ten rates copied from the UCHealth file; version labels are examples
tests/                       contract and verifier tests
results/                     run output
```

Keys in `build/keys.json` are prototype keys written in clear. A deployment
keeps root keys in each entity's hardware module and signing keys in the system
that produces the rate sheet.
