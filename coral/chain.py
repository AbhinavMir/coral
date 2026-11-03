"""Ledger client. Works against an in-process EVM (tests) or a Besu RPC endpoint."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD

from .compile import compile_registry
from .typed import (
    KIND, Rate, ChargemasterVersion, b32, unb32, domain,
    sign_delegation, sign_revocation, sign_root_veto, sign_rate, sign_chargemaster,
)

DEPLOY_FILE = Path(__file__).resolve().parent.parent / "build" / "deployment.json"


@dataclass
class RateRecord:
    id: bytes
    rate: Rate
    committed_at: int
    provider_signer: str
    payer_signer: str
    superseded_by: bytes = b"\0" * 32
    superseded_at: int = 0

    @property
    def effective_end(self) -> int:
        """Own end, or the start of the record that superseded this one."""
        return self.superseded_at if self.superseded_at else self.rate.end

    def covers(self, at: int) -> bool:
        return self.rate.start <= at and (self.effective_end == 0 or at < self.effective_end)


def _record(rate_id: bytes, b, c) -> RateRecord:
    rate = Rate(
        payerId=unb32(b[0]), planId=unb32(b[1]), providerId=unb32(b[2]), npi=unb32(b[3]),
        code=unb32(b[4]), modifier_=unb32(b[5]), setting=b[6], billingClass=b[7], unit=unb32(b[8]),
        formula=b[9], value=b[10], start=b[11], end=b[12],
        contractVersion=unb32(b[13]), referenceVersion=unb32(b[14]), supersedes=bytes(b[15]),
    )
    return RateRecord(rate_id, rate, c[0], c[1], c[2], bytes(c[3]), c[4])


class Chain:
    def __init__(self, w3: Web3, address: str | None = None, relayer=None, deploy_block: int = 0):
        self.w3 = w3
        self.deploy_block = deploy_block
        art = compile_registry()
        self.abi = art["abi"]
        self.bin = art["bin"]
        self.relayer = relayer or w3.eth.accounts[0]
        self.contract = w3.eth.contract(address=address, abi=self.abi) if address else None

    # ---- construction -----------------------------------------------------

    @classmethod
    def in_memory(cls):
        from web3 import EthereumTesterProvider
        return cls(Web3(EthereumTesterProvider()))

    @classmethod
    def rpc(cls, url: str | None = None, relayer_key: str | None = None):
        url = url or os.environ.get("CORAL_RPC", "http://127.0.0.1:8545")
        from web3.middleware import ExtraDataToPOAMiddleware
        w3 = Web3(Web3.HTTPProvider(url))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # IBFT extraData
        key = relayer_key or os.environ.get("CORAL_RELAYER_KEY")
        relayer = None
        if key:
            acct = Account.from_key(key)
            w3.middleware_onion.inject(_signing_middleware(acct), layer=0)
            relayer = acct.address
        address, deploy_block = None, 0
        if DEPLOY_FILE.exists():
            d = json.loads(DEPLOY_FILE.read_text())
            address, deploy_block = d.get("address"), d.get("block", 0)
        return cls(w3, address, relayer, deploy_block)

    def deploy(self, registrar: str) -> str:
        factory = self.w3.eth.contract(abi=self.abi, bytecode=self.bin)
        tx = factory.constructor(Web3.to_checksum_address(registrar)).transact({"from": self.relayer})
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.contract = self.w3.eth.contract(address=rcpt.contractAddress, abi=self.abi)
        self.deploy_block = rcpt.blockNumber
        return rcpt.contractAddress

    def save_deployment(self):
        DEPLOY_FILE.parent.mkdir(exist_ok=True)
        DEPLOY_FILE.write_text(json.dumps({"address": self.contract.address, "chainId": self.w3.eth.chain_id, "block": self.deploy_block}))

    @property
    def domain(self) -> dict:
        return domain(self.w3.eth.chain_id, self.contract.address)

    def _send(self, fn, sender=None):
        tx = fn.transact({"from": sender or self.relayer})
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx)
        if rcpt.status != 1:
            raise RuntimeError("transaction reverted")
        return rcpt

    # ---- registrar --------------------------------------------------------

    def register_entity(self, entity_id: str, kind: str, root: str, registrar=None):
        return self._send(self.contract.functions.registerEntity(b32(entity_id), KIND[kind], root), registrar)

    def propose_root(self, entity_id: str, new_root: str, registrar=None):
        return self._send(self.contract.functions.proposeRoot(b32(entity_id), new_root), registrar)

    def activate_root(self, entity_id: str):
        return self._send(self.contract.functions.activateRoot(b32(entity_id)))

    def veto_root(self, root_key, entity_id: str, new_root: str):
        sig = sign_root_veto(root_key, self.domain, entity_id, new_root)
        return self._send(self.contract.functions.vetoRoot(b32(entity_id), sig))

    # ---- keys -------------------------------------------------------------

    def delegate(self, root_key, entity_id: str, signer: str, not_before: int, not_after: int = 0):
        sig = sign_delegation(root_key, self.domain, entity_id, signer, not_before, not_after)
        return self._send(self.contract.functions.delegate(b32(entity_id), signer, not_before, not_after, sig))

    def revoke(self, root_key, entity_id: str, signer: str, effective_from: int):
        sig = sign_revocation(root_key, self.domain, entity_id, signer, effective_from)
        return self._send(self.contract.functions.revoke(b32(entity_id), signer, effective_from, sig))

    def authorized_at(self, entity_id: str, signer: str, t: int) -> bool:
        return self.contract.functions.authorizedAt(b32(entity_id), signer, t).call()

    # ---- records ----------------------------------------------------------

    def rate_id(self, rate: Rate) -> bytes:
        return self.contract.functions.rateId(rate.tuple()).call()

    def commit_rate(self, rate: Rate, provider_sig: bytes, payer_sig: bytes) -> bytes:
        rcpt = self._send(self.contract.functions.commitRate(rate.tuple(), provider_sig, payer_sig))
        ev = self.contract.events.RateCommitted().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]["rateId"]

    def commit_rate_with_keys(self, rate: Rate, provider_key, payer_key) -> bytes:
        return self.commit_rate(rate, sign_rate(provider_key, self.domain, rate), sign_rate(payer_key, self.domain, rate))

    def commit_rates(self, rates: list[Rate], provider_sigs: list[bytes], payer_sigs: list[bytes]):
        """Many records in one transaction. Returns the receipt."""
        return self._send(self.contract.functions.commitRates([r.tuple() for r in rates], provider_sigs, payer_sigs))

    def commit_chargemaster(self, cm: ChargemasterVersion, provider_key) -> bytes:
        sig = sign_chargemaster(provider_key, self.domain, cm)
        rcpt = self._send(self.contract.functions.commitChargemaster(cm.tuple(), sig))
        ev = self.contract.events.ChargemasterCommitted().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]["cmId"]

    def get_rate(self, rate_id: bytes) -> RateRecord | None:
        c = self.contract.functions.rates(rate_id).call()
        if c[0] == 0:
            return None
        b = self.contract.functions.rateBodies(rate_id).call()
        return _record(rate_id, b, c)

    def get_rates(self, ids: list[bytes], batch: int = 500) -> list[RateRecord]:
        """Bodies and commit data for many ids, `batch` per RPC call."""
        out = []
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            bodies, commits = self.contract.functions.getRates(chunk).call()
            out += [_record(rid, b, c) for rid, b, c in zip(chunk, bodies, commits)]
        return out

    def logs(self, event, argument_filters=None, step: int = 4000) -> list:
        """All logs of `event` since deployment, in block ranges under Besu's 5000-block eth_getLogs limit."""
        out = []
        head = self.w3.eth.block_number
        start = self.deploy_block
        while start <= head:
            end = min(start + step - 1, head)
            out += event.get_logs(from_block=start, to_block=end, argument_filters=argument_filters)
            start = end + 1
        return out

    def rates_for(self, provider_id: str | None = None, payer_id: str | None = None) -> list[RateRecord]:
        """All committed rates, filtered by indexed provider/payer, read from events."""
        flt = {}
        if provider_id:
            flt["providerId"] = b32(provider_id)
        if payer_id:
            flt["payerId"] = b32(payer_id)
        logs = self.logs(self.contract.events.RateCommitted(), flt or None)
        return self.get_rates([lg["args"]["rateId"] for lg in logs])

    def any_for(self, key: tuple) -> list[RateRecord]:
        """Every record for one key, superseded or not."""
        return [rec for rec in self.rates_for(provider_id=key[2], payer_id=key[0]) if rec.rate.key() == key]

    def find_rate(self, key: tuple, at: int) -> list[RateRecord]:
        """Records whose key matches and whose effective window covers `at`."""
        return [rec for rec in self.any_for(key) if rec.covers(at)]

    def chargemasters_for(self, provider_id: str) -> list[dict]:
        logs = self.logs(self.contract.events.ChargemasterCommitted(), {"providerId": b32(provider_id)})
        return [{
            "id": lg["args"]["cmId"], "version": unb32(lg["args"]["version"]),
            "fileHash": bytes(lg["args"]["fileHash"]), "effective": lg["args"]["effective"],
        } for lg in logs]

    def entity(self, entity_id: str) -> dict | None:
        e = self.contract.functions.entities(b32(entity_id)).call()
        if e[0] == 0:
            return None
        return {"kind": {1: "provider", 2: "payer"}[e[0]], "root": e[1], "registeredAt": e[2]}

    def entities(self) -> list[dict]:
        logs = self.logs(self.contract.events.EntityRegistered())
        seen = {}
        for lg in logs:
            a = lg["args"]
            seen[unb32(a["entityId"])] = {"id": unb32(a["entityId"]), "kind": {1: "provider", 2: "payer"}[a["kind"]], "root": a["root"]}
        return list(seen.values())

    def delegations_for(self, entity_id: str) -> list[dict]:
        logs = self.logs(self.contract.events.SignerDelegated(), {"entityId": b32(entity_id)})
        out = []
        for lg in logs:
            d = self.contract.functions.delegations(lg["args"]["signer"]).call()
            out.append({"signer": lg["args"]["signer"], "notBefore": d[1], "notAfter": d[2], "revokedFrom": d[3]})
        return out


class RateIndex:
    """All committed rates loaded once and indexed by key. Use for bulk verification."""

    def __init__(self, chain: Chain, provider_id: str | None = None, payer_id: str | None = None, records=None):
        self.records = records if records is not None else chain.rates_for(provider_id=provider_id, payer_id=payer_id)
        self.by_key: dict[tuple, list[RateRecord]] = {}
        for rec in self.records:
            self.by_key.setdefault(rec.rate.key(), []).append(rec)

    def find_rate(self, key: tuple, at: int) -> list[RateRecord]:
        return [rec for rec in self.by_key.get(key, []) if rec.covers(at)]

    def any_for(self, key: tuple) -> list[RateRecord]:
        return self.by_key.get(key, [])


def _signing_middleware(acct):
    """web3 v7 middleware: sign transactions from `acct` locally."""
    from web3.middleware import SignAndSendRawMiddlewareBuilder
    return SignAndSendRawMiddlewareBuilder.build(acct)


def _commit_receipts(self, provider_id: str):
    """Transaction receipts of every RateCommitted for a provider (gas accounting)."""
    logs = self.logs(self.contract.events.RateCommitted(), {"providerId": b32(provider_id)})
    return [self.w3.eth.get_transaction_receipt(lg["transactionHash"]) for lg in logs]


Chain.commit_receipts = _commit_receipts
