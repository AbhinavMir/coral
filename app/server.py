"""CORAL web app: provider and payer page, CMS page.

Prototype. Signing keys are held by this server (build/keys.json) and chosen by
the entity named in the request. A deployment replaces `_sign_for` with the
entity's own signing service; nothing else changes.

Run: .venv/bin/uvicorn app.server:app --reload --port 8000
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import uuid
from pathlib import Path

from eth_account import Account
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from coral.chain import Chain, DEPLOY_FILE, RateIndex
from coral.typed import (Rate, ChargemasterVersion, sign_rate, SETTING_NAME, FORMULA_NAME, FORMULA, SETTING,
                         BILLING_CLASS, BILLING_CLASS_NAME)
from coral.verify import Line, verify_line
from coral.mrf import provider_csv, provider_header, payer_json, gross_charges, Skipped

ROOT = Path(__file__).resolve().parent
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"
PENDING_FILE = DEPLOY_FILE.parent / "pending.json"
CDM_DIR = DEPLOY_FILE.parent / "chargemasters"   # signed chargemaster files, named by SHA-256
RELAYER_KEY = "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"

app = FastAPI(title="CORAL")
chain = Chain.rpc(relayer_key=RELAYER_KEY)
keys = json.loads(KEYS_FILE.read_text())


def _pending() -> list[dict]:
    return json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else []


def _save_pending(items: list[dict]):
    PENDING_FILE.write_text(json.dumps(items, indent=2))


def _entity(eid: str) -> dict:
    e = keys["entities"].get(eid)
    if not e:
        raise HTTPException(404, f"unknown entity {eid}")
    return e


def _sign_for(eid: str, rate: Rate) -> bytes:
    """Prototype: server-held signing key. Replace with the entity's signing service."""
    return sign_rate(_entity(eid)["signer"]["key"], chain.domain, rate)


_gross_cache: dict[str, dict] = {}


def _gross_index(file_hash: str) -> dict | None:
    """List prices from the signed chargemaster file with this hash, if the server holds the file."""
    if file_hash not in _gross_cache:
        f = CDM_DIR / file_hash
        _gross_cache[file_hash] = gross_charges(f) if f.exists() else None
    return _gross_cache[file_hash]


def _list_price(rec) -> int | None:
    """List price in cents that a percent-of-charges record applies to, from the chargemaster version it names."""
    r = rec.rate
    if r.formula != 1 or not r.referenceVersion:
        return None
    for c in chain.chargemasters_for(r.providerId):
        if c["version"] == r.referenceVersion:
            idx = _gross_index(c["fileHash"].hex())
            return idx.get((r.code, r.setting, r.unit)) if idx else None
    return None


def _rate_json(rec) -> dict:
    r = rec.rate
    gross = _list_price(rec)
    return {
        "listPriceCents": gross,
        "amountCents": r.value if r.formula == 0 else (round(gross * r.value / 10000) if gross is not None else None),
        "id": "0x" + rec.id.hex(), "committedAt": rec.committed_at,
        "providerSigner": rec.provider_signer, "payerSigner": rec.payer_signer,
        "payerId": r.payerId, "planId": r.planId, "providerId": r.providerId, "npi": r.npi,
        "code": r.code, "modifier": r.modifier_, "setting": SETTING_NAME[r.setting],
        "billingClass": BILLING_CLASS_NAME[r.billingClass], "unit": r.unit,
        "formula": FORMULA_NAME[r.formula], "value": r.value,
        "valueText": f"${r.value/100:,.2f}" if r.formula == 0 else f"{r.value/100:.2f}%",
        "start": r.start, "end": r.end, "effectiveEnd": rec.effective_end,
        "contractVersion": r.contractVersion, "referenceVersion": r.referenceVersion,
        "supersedes": "0x" + r.supersedes.hex() if any(r.supersedes) else None,
        "supersededBy": "0x" + rec.superseded_by.hex() if any(rec.superseded_by) else None,
        "supersededAt": rec.superseded_at,
    }


class RateIn(BaseModel):
    payerId: str
    planId: str
    providerId: str
    npi: str
    code: str
    modifier: str = ""
    setting: str
    billingClass: str = "facility"
    unit: str = ""
    formula: str
    value: float          # dollars for fixed, percent for percentage formulas
    start: str            # YYYY-MM-DD
    end: str = ""         # YYYY-MM-DD or empty
    contractVersion: str
    referenceVersion: str = ""
    supersedes: str = ""  # record id this one replaces, hex

    def to_rate(self) -> Rate:
        start = int(time.mktime(time.strptime(self.start, "%Y-%m-%d")))
        end = int(time.mktime(time.strptime(self.end, "%Y-%m-%d"))) if self.end else 0
        return Rate(
            payerId=self.payerId, planId=self.planId, providerId=self.providerId, npi=self.npi,
            code=self.code, modifier_=self.modifier, setting=SETTING[self.setting],
            billingClass=BILLING_CLASS[self.billingClass], unit=self.unit, formula=FORMULA[self.formula],
            value=round(self.value * 100), start=start, end=end,
            contractVersion=self.contractVersion, referenceVersion=self.referenceVersion,
            supersedes=bytes.fromhex(self.supersedes.removeprefix("0x")) if self.supersedes else b"\0" * 32,
        )


# ---- shared ---------------------------------------------------------------

@app.get("/api/status")
def status():
    return {
        "chainId": chain.w3.eth.chain_id, "block": chain.w3.eth.block_number,
        "contract": chain.contract.address, "peers": chain.w3.net.peer_count,
        "registrar": keys["registrar"]["address"],
    }


@app.get("/api/entities")
def entities():
    out = []
    for e in chain.entities():
        meta = keys["entities"].get(e["id"], {})
        out.append({**e, "name": meta.get("name", ""), "npi": meta.get("npi", ""), "signers": chain.delegations_for(e["id"])})
    return out


@app.get("/api/rates")
def rates(providerId: str | None = None, payerId: str | None = None):
    recs = chain.rates_for(provider_id=providerId, payer_id=payerId)
    return [_rate_json(r) for r in sorted(recs, key=lambda r: r.committed_at, reverse=True)]


@app.get("/api/rates/{rate_id}")
def rate(rate_id: str):
    rec = chain.get_rate(bytes.fromhex(rate_id.removeprefix("0x")))
    if not rec:
        raise HTTPException(404, "no such record")
    return _rate_json(rec)


# ---- provider / payer -------------------------------------------------------

@app.get("/api/pending")
def pending(entityId: str):
    """Proposals that involve this entity: ones it made and ones awaiting its signature."""
    return [p for p in _pending() if entityId in (p["rate"]["providerId"], p["rate"]["payerId"])]


@app.get("/api/examples")
def examples():
    """Ten example rates copied from the UCHealth file, for the propose form. See data/examples.json."""
    return json.loads((ROOT.parent / "data" / "examples.json").read_text())


@app.post("/api/propose")
def propose(entityId: str, body: RateIn):
    e = _entity(entityId)
    if entityId not in (body.providerId, body.payerId):
        raise HTTPException(400, "proposer must be the provider or the payer on the rate")
    rate = body.to_rate()
    sig = _sign_for(entityId, rate)
    items = _pending()
    item = {
        "id": uuid.uuid4().hex[:12], "rate": body.model_dump(), "rateId": "0x" + chain.rate_id(rate).hex(),
        "proposer": entityId, "proposerName": e["name"], "proposerSig": sig.hex(), "proposedAt": int(time.time()),
        "awaiting": body.payerId if entityId == body.providerId else body.providerId,
    }
    items.append(item)
    _save_pending(items)
    return item


@app.post("/api/countersign")
def countersign(entityId: str, pendingId: str):
    items = _pending()
    item = next((p for p in items if p["id"] == pendingId), None)
    if not item:
        raise HTTPException(404, "no such proposal")
    if item["awaiting"] != entityId:
        raise HTTPException(400, f"this proposal awaits {item['awaiting']}")
    rate = RateIn(**item["rate"]).to_rate()
    my_sig = _sign_for(entityId, rate)
    their_sig = bytes.fromhex(item["proposerSig"])
    if entityId == rate.providerId:
        rid = chain.commit_rate(rate, my_sig, their_sig)
    else:
        rid = chain.commit_rate(rate, their_sig, my_sig)
    _save_pending([p for p in items if p["id"] != pendingId])
    return _rate_json(chain.get_rate(rid))


@app.post("/api/reject")
def reject(entityId: str, pendingId: str):
    items = _pending()
    item = next((p for p in items if p["id"] == pendingId), None)
    if not item or entityId not in (item["proposer"], item["awaiting"]):
        raise HTTPException(404, "no such proposal")
    _save_pending([p for p in items if p["id"] != pendingId])
    return {"removed": pendingId}


@app.post("/api/chargemaster")
async def chargemaster(entityId: str = Form(...), version: str = Form(...), effective: str = Form(...), file: UploadFile = File(...)):
    e = _entity(entityId)
    if e["kind"] != "provider":
        raise HTTPException(400, "only providers sign chargemasters")
    data = await file.read()
    cm = ChargemasterVersion(entityId, version, hashlib.sha256(data).digest(),
                             int(time.mktime(time.strptime(effective, "%Y-%m-%d"))))
    cid = chain.commit_chargemaster(cm, e["signer"]["key"])
    CDM_DIR.mkdir(exist_ok=True)
    (CDM_DIR / cm.fileHash.hex()).write_bytes(data)
    _gross_cache.pop(cm.fileHash.hex(), None)
    return {"id": "0x" + cid.hex(), "version": version, "sha256": cm.fileHash.hex(), "bytes": len(data)}


@app.get("/api/chargemasters")
def chargemasters(entityId: str):
    return [{**c, "id": "0x" + c["id"].hex(), "fileHash": c["fileHash"].hex()} for c in chain.chargemasters_for(entityId)]


@app.post("/api/keys/rotate")
def rotate(entityId: str):
    """Root key delegates a new signing key and revokes the old one from now.

    Records signed before now stay valid; the old key cannot sign new ones.
    """
    e = _entity(entityId)
    now = int(time.time())
    new = Account.create()
    chain.delegate(e["root"]["key"], entityId, new.address, now)
    # a revocation must not be earlier than the block that carries it
    chain.revoke(e["root"]["key"], entityId, e["signer"]["address"], now + 10)
    e["signer"] = {"address": new.address, "key": new.key.hex()}
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    return {"entityId": entityId, "newSigner": new.address, "signers": chain.delegations_for(entityId)}



# ---- CMS --------------------------------------------------------------------

@app.post("/api/verify")
async def verify(kind: str = Form(...), entityId: str = Form(...), asOf: str = Form(...), file: UploadFile = File(...),
                 planId: str = Form("")):
    """Verify every negotiated line of a published file against the ledger.

    kind "provider": CMS v2 tall CSV. The provider is `entityId`; the billing NPI comes from the file
    header; payer names in the file are matched to registered payer names; plan names are plan ids.
    kind "payer": Transparency in Coverage in-network JSON for payer `entityId` and plan `planId`;
    NPIs in the file are matched to registered providers.
    """
    data = await file.read()
    tmp = DEPLOY_FILE.parent / f"upload-{uuid.uuid4().hex[:8]}"
    tmp.write_bytes(data)
    at = int(time.mktime(time.strptime(asOf, "%Y-%m-%d")))
    ents = keys["entities"]
    rows, counts, skipped = [], {}, Skipped()
    try:
        if kind == "provider":
            hdr = provider_header(tmp)
            npi = hdr.get("type_2_npi", "").strip()
            if not npi:
                raise HTTPException(400, "file header has no type_2_npi")
            payer_ids = {e["name"]: eid for eid, e in ents.items() if e["kind"] == "payer"}
            lines = provider_csv(tmp, entityId, npi, payer_ids, skipped=skipped)
        elif kind == "payer":
            if not planId:
                raise HTTPException(400, "planId is required for a payer file")
            provider_map = {e["npi"]: eid for eid, e in ents.items() if e["kind"] == "provider" and e.get("npi")}
            lines = payer_json(tmp, entityId, planId, provider_map, skipped=skipped)
        else:
            raise HTTPException(400, "kind must be provider or payer")
        index = RateIndex(chain)
        for line in lines:
            if line.amount_cents is None and line.percent_bp is None:
                skipped.add("no_dollar_or_percent")
                continue
            v = verify_line(index, line, at)
            counts[v.status] = counts.get(v.status, 0) + 1
            rows.append({
                "payerId": line.payerId, "planId": line.planId, "providerId": line.providerId, "npi": line.npi,
                "code": line.code, "modifier": line.modifier_, "setting": SETTING_NAME[line.setting],
                "billingClass": BILLING_CLASS_NAME[line.billingClass], "unit": line.unit,
                "published": line.amount_cents if line.amount_cents is not None else line.percent_bp,
                "publishedKind": "cents" if line.amount_cents is not None else "bp",
                "expected": v.expected_cents, "status": v.status,
                "recordId": "0x" + v.record.id.hex() if v.record else None,
            })
    finally:
        tmp.unlink(missing_ok=True)
    return {"lines": len(rows), "counts": counts, "skipped": dict(skipped.counts), "rows": rows[:2000]}


# ---- pages ------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/party")
def party_page():
    return FileResponse(ROOT / "static" / "party.html")


@app.get("/cms")
def cms_page():
    return FileResponse(ROOT / "static" / "cms.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
