#!/bin/sh
# Fresh ledger, then every network run of the paper, in order. Logs to results/rerun_all.log.
set -e
cd "$(dirname "$0")/.."
P=.venv/bin/python
echo "== $(date) reset network"
(cd besu && docker compose down -v >/dev/null 2>&1 || true)
rm -rf besu/data/cms/* besu/data/providers/* besu/data/payers/* besu/data/auditor/* build/deploy.json build/keys.json build/chargemasters
(cd besu && docker compose up -d)
echo "== $(date) wait for blocks"
for i in $(seq 1 60); do
  n=$(curl -s -X POST -H 'content-type: application/json' --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' http://127.0.0.1:8545 | sed 's/.*"result":"0x\([0-9a-f]*\)".*/\1/')
  if [ -n "$n" ] && [ "$n" != "0" ] && [ "$((16#$n))" -gt 2 ]; then break; fi
  sleep 5
done
echo "== $(date) block $((16#$n)); bootstrap"
$P scripts/bootstrap.py
$P scripts/seed_examples.py
echo "== $(date) full-sheet run"
$P scripts/evaluate_sheet.py data/raw/840398876_yampa-valley-medical-center_standardcharges.csv --provider TIN:84-0398876 --batch 100 --inflight 8 > results/evaluate_sheet.log 2>&1
echo "== $(date) pair run"
$P scripts/evaluate_pairs.py data/raw/tic/2026-10-05_pl-1s7-tr25_Aetna-Life-Insurance-Company.json.gz > results/evaluate_pairs.log 2>&1
echo "== $(date) key events"
$P scripts/key_events.py > results/key_events.log 2>&1
echo "== $(date) derived"
$P scripts/sheet_stats.py data/raw/840398876_yampa-valley-medical-center_standardcharges.csv TIN:84-0398876
$P scripts/evaluate_pairs_coarse.py
$P scripts/pairs_sensitivity.py
$P scripts/make_numbers.py > /dev/null
echo "== $(date) done"
