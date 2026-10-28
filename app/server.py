"""CORAL web app: provider, payer, and CMS views over the ledger.

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
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from coral.chain import Chain, DEPLOY_FILE
from coral.typed import Rate, ChargemasterVersion, sign_rate, SETTING_NAME, FORMULA_NAME, FORMULA, SETTING
from coral.verify import Line, verify_line
from coral.mrf import provider_csv

ROOT = Path(__file__).resolve().parent
KEYS_FILE = DEPLOY_FILE.parent / "keys.json"
PENDING_FILE = DEPLOY_FILE.parent / "pending.json"
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


def _rate_json(rec) -> dict:
    r = rec.rate
    return {
        "id": "0x" + rec.id.hex(), "committedAt": rec.committed_at,
        "providerSigner": rec.provider_signer, "payerSigner": rec.payer_signer,
        "payerId": r.payerId, "planId": r.planId, "providerId": r.providerId, "npi": r.npi,
        "code": r.code, "modifier": r.modifier_, "setting": SETTING_NAME[r.setting],
        "formula": FORMULA_NAME[r.formula], "value": r.value,
        "valueText": f"${r.value/100:,.2f}" if r.formula == 0 else f"{r.value/100:.2f}%",
        "start": r.start, "end": r.end, "contractVersion": r.contractVersion, "referenceVersion": r.referenceVersion,
    }


class RateIn(BaseModel):
    payerId: str
    planId: str
    providerId: str
    npi: str
    code: str
    modifier: str = ""
    setting: str
    formula: str
    value: float          # dollars for fixed, percent for percentage formulas
    start: str            # YYYY-MM-DD
    end: str = ""         # YYYY-MM-DD or empty
    contractVersion: str
    referenceVersion: str = ""

    def to_rate(self) -> Rate:
        start = int(time.mktime(time.strptime(self.start, "%Y-%m-%d")))
        end = int(time.mktime(time.strptime(self.end, "%Y-%m-%d"))) if self.end else 0
        return Rate(
            payerId=self.payerId, planId=self.planId, providerId=self.providerId, npi=self.npi,
            code=self.code, modifier_=self.modifier, setting=SETTING[self.setting], formula=FORMULA[self.formula],
            value=round(self.value * 100), start=start, end=end,
            contractVersion=self.contractVersion, referenceVersion=self.referenceVersion,
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
        out.append({**e, "name": meta.get("name", ""), "signers": chain.delegations_for(e["id"])})
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
    chain.revoke(e["root"]["key"], entityId, e["signer"]["address"], now)
    e["signer"] = {"address": new.address, "key": new.key.hex()}
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    return {"entityId": entityId, "newSigner": new.address, "signers": chain.delegations_for(entityId)}


@app.get("/api/export/mrf")
def export_mrf(entityId: str):
    """Provider MRF lines (CMS v2 tall CSV subset) with a coral_record_id column."""
    e = _entity(entityId)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["description", "code|1", "code|1|type", "modifiers", "setting", "payer_name", "plan_name",
                "standard_charge|negotiated_dollar", "standard_charge|negotiated_percentage",
                "standard_charge|methodology", "coral_record_id"])
    for rec in chain.rates_for(provider_id=entityId):
        r = rec.rate
        ctype, code = r.code.split(":", 1)
        dollar = f"{r.value/100:.2f}" if r.formula == 0 else ""
        pct = f"{r.value/100:.2f}" if r.formula != 0 else ""
        method = {0: "fee schedule", 1: "percent of total billed charges", 2: "other"}[r.formula]
        w.writerow(["", code, ctype, r.modifier_, SETTING_NAME[r.setting], r.payerId, r.planId,
                    dollar, pct, method, "0x" + rec.id.hex()])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{entityId}-mrf.csv"'})


# ---- CMS --------------------------------------------------------------------

@app.post("/api/verify")
async def verify(providerId: str = Form(...), npi: str = Form(...), payerMap: str = Form(...),
                 asOf: str = Form(...), file: UploadFile = File(...)):
    """Verify every negotiated line of an uploaded provider MRF CSV against the ledger.

    payerMap: JSON {"payer_name|plan_name": ["PAYER:UHC", "PLAN:..."]} mapping file names to ledger ids.
    """
    data = await file.read()
    tmp = DEPLOY_FILE.parent / f"upload-{uuid.uuid4().hex[:8]}.csv"
    tmp.write_bytes(data)
    pm = {tuple(k.split("|", 1)): tuple(v) for k, v in json.loads(payerMap).items()}
    at = int(time.mktime(time.strptime(asOf, "%Y-%m-%d")))
    rows, counts = [], {}
    try:
        for line in provider_csv(tmp, providerId, npi, pm):
            v = verify_line(chain, line, at)
            counts[v.status] = counts.get(v.status, 0) + 1
            rows.append({
                "payerId": line.payerId, "planId": line.planId, "code": line.code, "modifier": line.modifier_,
                "setting": SETTING_NAME[line.setting],
                "published": line.amount_cents, "expected": v.expected_cents, "status": v.status,
                "recordId": "0x" + v.record.id.hex() if v.record else None,
            })
    finally:
        tmp.unlink(missing_ok=True)
    return {"lines": len(rows), "counts": counts, "rows": rows[:2000]}


@app.get("/api/verify/record")
def verify_record(rateId: str, amount: float | None = None):
    """Check one claim line: does this record exist, and is it live today?"""
    rec = chain.get_rate(bytes.fromhex(rateId.removeprefix("0x")))
    if not rec:
        return {"status": "no_record"}
    now = int(time.time())
    live = rec.rate.start <= now and (rec.rate.end == 0 or now < rec.rate.end)
    out = {"status": "live" if live else "expired", "record": _rate_json(rec)}
    if amount is not None and rec.rate.formula == 0:
        out["amountMatches"] = round(amount * 100) == rec.rate.value
    return out


@app.get("/api/coverage")
def coverage():
    """Per provider-payer pair: signed records, live records, signers in use."""
    now = int(time.time())
    pairs = {}
    for rec in chain.rates_for():
        k = (rec.rate.providerId, rec.rate.payerId)
        p = pairs.setdefault(k, {"providerId": k[0], "payerId": k[1], "records": 0, "live": 0, "plans": set(), "codes": set()})
        p["records"] += 1
        if rec.rate.start <= now and (rec.rate.end == 0 or now < rec.rate.end):
            p["live"] += 1
        p["plans"].add(rec.rate.planId)
        p["codes"].add(rec.rate.code)
    return [{**p, "plans": sorted(p["plans"]), "codes": len(p["codes"])} for p in pairs.values()]


@app.get("/api/ledger")
def ledger(n: int = 20):
    blocks = []
    head = chain.w3.eth.block_number
    for b in range(head, max(-1, head - n), -1):
        blk = chain.w3.eth.get_block(b)
        blocks.append({"number": b, "timestamp": blk.timestamp, "txs": len(blk.transactions), "hash": blk.hash.hex()})
    return blocks


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
