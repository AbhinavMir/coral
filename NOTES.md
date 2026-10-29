# CORAL working notes

Findings for the paper. Dates are absolute. Numbers come from runs in this repo
unless a source is named.

## Problem

- Hospitals publish negotiated rates under 45 CFR 180 (Hospital Price Transparency). Payers publish in-network rates under the Transparency in Coverage rule (85 FR 72158). Each file is produced by one party. No file carries the other party's signature.
- Trilliant Health, 30 April 2026: 450 hospitals, Aetna and UnitedHealthcare, 16 codes, data from August 2025. Exact match 9.0% inpatient, 10.8% GI, 11.7% orthopedic. Within 10%: 50.8%, 41.7%, 47.4%. More than 50% apart: 17.9% for inpatient and GI.
- The comparison cannot say which file is correct. Neither file links a line to a contract version, an effective date, or the other party.
- Rates are formulas: fixed amount, percent of billed charges, or percent of a reference fee schedule. In the one real provider file profiled (UCHealth Yampa Valley Medical Center, file dated 2025-11-01, CMS format 3.0.0): 633,324 rows, 454,793 with a dollar amount, 228,164 (36%) percent of total billed charges, 30 payer/plan pairs, one type 2 NPI in the header (1790787307). A percent-of-charges line changes value when the chargemaster changes. The payer does not sign the chargemaster.

## Prior art

- No paper or product found on jointly signed negotiated rates or a ledger that holds rate records (six searches, 27 October 2026).
- Claim-status networks on a ledger exist: Change Healthcare on Hyperledger Fabric (2018), Avaneer Health, Synaptic Health Alliance (provider directory), MediLedger 340B working group (2021). None holds the rate with both signatures.
- Transparency logs (Certificate Transparency, Trillian, SCITT) are append-only under one operator. A ledger with several validators is justified only when the parties distrust each other and accept no single operator. Cite the three-condition test from the blockchain-health position paper. Equivocation detection: PeerReview (SOSP 2007), SUNDR fork consistency. arXiv 2308.04781: an append on a chain is not proof the counterparty received it.
- FHIR Provenance, W3C PROV, C2PA, HL7 AI Transparency on FHIR cover record provenance, not contract terms.

## Keys

Who signs, with what, and how a verifier knows the key was valid.

### Identity roots

- Provider contracting entity: the tax identification number (TIN). Contracts are signed at the TIN. A TIN covers one or more type 2 NPIs. CMS already binds NPIs to organizations through NPPES and PECOS, and authorizes staff to act for an organization through the Identity & Access Management System (I&A).
- Payer entity: a payer id. Candidates: the HIOS issuer id (used in Transparency in Coverage files), the NAIC company code, or the X12 payer id. The prototype uses an opaque string.
- Registrar: CMS. CMS already receives both files and already proofs both kinds of organization. CMS binds an entity id to a root public key once, after out-of-band proofing. CMS never signs a rate.
- Entity ids in the prototype: `TIN:84-0398876` (UCHealth Yampa Valley Medical Center, TIN from the file name, NPI 1790787307 from the file header), `PAYER:UHC`, `PAYER:AETNA` (payer names as written in the file). Any string up to 32 bytes works; longer ids are hashed and the file must carry the full text.

### Key hierarchy

- Root key: one per entity. Held offline, in a hardware module. It signs two things only: delegations and revocations. It never signs a rate. The contract enforces this: `commitRate` checks the signer against delegations, and a root key has no delegation.
- Signing key: one or more per entity, delegated by the root for a window `[notBefore, notAfter)`. These are the keys inside the contract-management or revenue-cycle system that produces the rate sheet. Rotation = delegate a new key, revoke the old one from now.
- Delegation and revocation records go on the ledger. A verifier answers "was key K authorized for entity E at time T" from the ledger alone.

### What time is a signature judged against

- Validity is checked at the block timestamp of the commit, which IBFT consensus sets. The signer does not choose it. A key revoked at T cannot commit at T+1, and a compromised key cannot backdate a commit to before its revocation.
- Records committed before a revocation stay valid. Test `test_revocation_blocks_future_commits` covers this.
- Consequence: the ledger's clock is the contract's clock. Effective dates inside the rate (`start`, `end`) are contract terms, signed by both parties, and are separate from the commit time.

### Signature format

- EIP-712 typed data. A signing tool shows the fields (payer, plan, NPI, code, modifier, setting, formula, value, start, end, contract version, reference version) to the person who signs. The domain separator binds chain id and contract address, so a signature for one deployment cannot be replayed on another (`test_signature_bound_to_deployment`).
- Both parties sign the same struct hash. The record id is that hash. Anyone can relay the transaction; the relayer has no authority. In the prototype CMS runs the relayer.
- Two signatures, one transaction. The first signature is held off-ledger until the second exists (the app's "pending" list). A proposal that is never countersigned leaves no ledger trace. This is deliberate for the first paper; an on-ledger proposal record would show who refused to sign and is deferred.

### Failure cases

- Signing key compromised: root revokes from the time of compromise. Records committed after that time by that key are invalid by the rule above; records before it stand. Which records to contest is a contract question, not a ledger one.
- Root key lost or compromised: CMS replaces the root (`replaceRoot`) after re-proofing, the same as a lost passport. Existing delegations stay until the new root revokes them.
- Registrar key compromised: a governance action by the validators. Out of scope; note it.
- Node (validator) keys are separate from record signing keys. The four validators in the prototype stand for CMS, a provider association, a payer association, and an auditor. No validator can sign a rate.

### Why not X.509 or DirectTrust certificates

- DirectTrust certificates bind an organization to a domain for Direct messaging, and a CA hierarchy already exists in healthcare. They could stand in for the root key. The ledger still needs the delegation and revocation records to answer "valid at time T" without an OCSP responder. A deployment can accept either an on-ledger root registration or an X.509 chain to a trusted CA; the contract only needs the root's public key.

## Design

- Record key: payer, plan id, provider TIN, NPI, code, modifier, setting. Record value: formula type and value, start, end, contract version, reference version.
- Record types implemented: Entity (registrar), Delegation, Revocation, Rate (both sign), ChargemasterVersion (provider signs a SHA-256 of the file). Amendment, witness checkpoint, and on-ledger proposal are deferred.
- Verifier: recomputes the amount from signed inputs only. Fixed: the value. Percent of charges: value × gross charge from the chargemaster version named in the record. Percent of reference: value × reference amount from the named schedule version.
- Provider MRF lines and payer TiC lines carry the record id. X12 837 and 835 could carry it in a REF segment; claims are out of scope for this paper.
- First-paper scope: one check. Does a published line match a jointly signed record that covers its date.

## Build

- Contract: `contracts/RateRegistry.sol`, Solidity 0.8.24, 7,220 bytes of bytecode, 35 ABI entries. EVM Shanghai (PUSH0).
- Network: Hyperledger Besu (image `hyperledger/besu:latest`, 26.10.2), IBFT 2.0, four validators in Docker, 2 s block period, zero base fee, chain id 2741. `besu/docker-compose.yml`. Fixed IPs because Besu rejects hostnames in enode URLs.
- Client: Python, web3 7, eth-account. `coral/chain.py`, `coral/typed.py`. In-memory EVM (eth-tester) for tests.
- Reads come from events. Besu caps `eth_getLogs` at 5000 blocks per call, so `Chain.logs` scans from the deploy block (stored in `build/deployment.json`) in 4000-block ranges. Found when the app returned 500 after block 5000.
- Tests: 11, all pass. Cover commit, duplicate, unauthorized key, wrong-side key, revocation, delegation window, root-cannot-sign, domain binding, root replacement, verify fixed, verify percent-of-charges.
- App: FastAPI + static HTML. The propose form loads one of ten examples from `data/examples.json`: codes, plans, settings, gross charges, and amounts copied from the UCHealth file (CT head, ED level 3, chest X-ray, metabolic panel, colonoscopy, knee arthroplasty, MS-DRG 470 and 871, enoxaparin; fee schedule, case rate, and percent of charges). Contract and chargemaster version labels are marked as examples because the file does not carry them. `scripts/seed_examples.py` signs all ten plus the SHA-256 of the file as the chargemaster version. `/party` for provider and payer: Negotiated rates (each row shows both signers), Pending (countersign), Propose, Chargemaster (providers only), Keys. `/cms`: Registry (entity, NPI, root key, signing keys) and Verify a published file (provider CSV or payer TiC JSON; the NPI comes from the file header, payer names in the file are matched to registered names, plan names are plan ids). Export, coverage, claim-line check, and the block list were removed on 2026-10-29: claims are out of scope, the rest were views, not actions. Keys are server-held in the prototype.
- Parsers: provider CSV (CMS v2/v3 tall), payer TiC in-network JSON (streamed). Payer parser is written and untested on a real file.

## Results (run 2026-10-27, `results/evaluate_provider.json`)

- Input: UCHealth Yampa Valley Medical Center file, payer "United Healthcare", plan "Broad Networks". First 200 distinct (code, modifier, setting) lines with a dollar amount.
- Parse: 0.8 s for the 141 MB file to the first 200 lines.
- Commit: 200 records, one per block, 399 s wall, 1.99 s per record. Gas 258,923 mean per record (min and max in the JSON). Transaction input 740 bytes mean. Sequential because the client waited for each receipt; the block gas limit allows many per block.
- Verify, clean copy: 200 of 200 match. Index load 2.2 s, verification 2.2 s for 200 lines.
- Verify, tampered copy (20 of 200 amounts altered by ±10%, +50%, or −50%): 180 match, 20 amount_differs. No false positives, no misses.
- Key rotation on the ledger (run before the reset): new key delegated, old key revoked from now; a commit with an unauthorized key reverts with `SignerNotAuthorized`; `authorizedAt(old, commitTime)` true and `authorizedAt(old, now)` false.
- The payer signature in the 200-record run is a prototype key, not UnitedHealthcare's agreement. The run shows mechanism and cost, not that the file is correct.

## Open items

- Payer side on real data: fetch one Transparency in Coverage in-network file for the same provider (UHC publishes an index at transparency-in-coverage.uhc.com) and run `coral/mrf.py:payer_json`. Files are large; the parser streams.
- Evaluation on the Trilliant pairing (450 hospitals) needs hospital files found through each hospital's `cms-hpt.txt` and two payer indexes. Of ten hospitals tried on 2026-10-27, four served a `cms-hpt.txt` (NYP, UCHealth, OHSU, Houston Methodist); the rest returned HTML or a block page.
- Batching commits: N transactions per block, measure records per second.
- On-ledger proposal record (who proposed, who did not sign). Deferred.
- Amendments that reference the record they replace. Deferred.

## Venue

- AMIA Annual Symposium, Paper - Regular: 10 pages, abstract 125-150 words, one column, Letter, Vancouver references. 2027 symposium 6-10 November, San Diego. Call for papers not yet out; past deadlines mid March.
- Alternatives: IEEE ICBC 2027 (deadline 14 January 2027), Blockchain in Healthcare Today (10,000 words, USD 1,285 APC, PubMed Central), Journal of Medical Systems (IF 5.7).
