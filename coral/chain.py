"""Ledger client. Works against an in-process EVM (tests) or a Besu RPC endpoint."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from web3 import Web3

from .compile import compile_registry
from .typed import (
    KIND, Rate, ChargemasterVersion, b32, unb32, domain,
    sign_delegation, sign_revocation, sign_rate, sign_chargemaster,
)

DEPLOY_FILE = Path(__file__).resolve().parent.parent / "build" / "deployment.json"


@dataclass
class RateRecord:
    id: bytes
    rate: Rate
    committed_at: int
    provider_signer: str
    payer_signer: str


class Chain:
    def __init__(self, w3: Web3, address: str | None = None, relayer=None):
        self.w3 = w3
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
        address = None
        if DEPLOY_FILE.exists():
            address = json.loads(DEPLOY_FILE.read_text()).get("address")
        return cls(w3, address, relayer)

    def deploy(self, registrar: str) -> str:
        factory = self.w3.eth.contract(abi=self.abi, bytecode=self.bin)
        tx = factory.constructor(Web3.to_checksum_address(registrar)).transact({"from": self.relayer})
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.contract = self.w3.eth.contract(address=rcpt.contractAddress, abi=self.abi)
        return rcpt.contractAddress

    def save_deployment(self):
        DEPLOY_FILE.parent.mkdir(exist_ok=True)
        DEPLOY_FILE.write_text(json.dumps({"address": self.contract.address, "chainId": self.w3.eth.chain_id}))

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

    def replace_root(self, entity_id: str, new_root: str, registrar=None):
        return self._send(self.contract.functions.replaceRoot(b32(entity_id), new_root), registrar)

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
        ev = self.contract.events.RateCommitted().process_receipt(rcpt)
        return ev[0]["args"]["rateId"]

    def commit_rate_with_keys(self, rate: Rate, provider_key, payer_key) -> bytes:
        return self.commit_rate(rate, sign_rate(provider_key, self.domain, rate), sign_rate(payer_key, self.domain, rate))

    def commit_chargemaster(self, cm: ChargemasterVersion, provider_key) -> bytes:
        sig = sign_chargemaster(provider_key, self.domain, cm)
        rcpt = self._send(self.contract.functions.commitChargemaster(cm.tuple(), sig))
        ev = self.contract.events.ChargemasterCommitted().process_receipt(rcpt)
        return ev[0]["args"]["cmId"]

    def get_rate(self, rate_id: bytes) -> RateRecord | None:
        c = self.contract.functions.rates(rate_id).call()
        if c[0] == 0:
            return None
        b = self.contract.functions.rateBodies(rate_id).call()
        rate = Rate(
            payerId=unb32(b[0]), planId=unb32(b[1]), providerId=unb32(b[2]), npi=unb32(b[3]),
            code=unb32(b[4]), modifier_=unb32(b[5]), setting=b[6], formula=b[7], value=b[8],
            start=b[9], end=b[10], contractVersion=unb32(b[11]), referenceVersion=unb32(b[12]),
        )
        return RateRecord(rate_id, rate, c[0], c[1], c[2])

    def rates_for(self, provider_id: str | None = None, payer_id: str | None = None) -> list[RateRecord]:
        """All committed rates, filtered by indexed provider/payer, read from events."""
        flt = {}
        if provider_id:
            flt["providerId"] = b32(provider_id)
        if payer_id:
            flt["payerId"] = b32(payer_id)
        logs = self.contract.events.RateCommitted().get_logs(from_block=0, argument_filters=flt or None)
        return [self.get_rate(lg["args"]["rateId"]) for lg in logs]

    def find_rate(self, payer_id, plan_id, provider_id, npi, code, modifier, setting, at: int) -> list[RateRecord]:
        """Rates whose key matches and whose window covers `at`."""
        out = []
        for rec in self.rates_for(provider_id=provider_id, payer_id=payer_id):
            r = rec.rate
            if (r.planId, r.npi, r.code, r.modifier_, r.setting) != (plan_id, npi, code, modifier, setting):
                continue
            if at < r.start or (r.end and at >= r.end):
                continue
            out.append(rec)
        return out

    def chargemasters_for(self, provider_id: str) -> list[dict]:
        logs = self.contract.events.ChargemasterCommitted().get_logs(
            from_block=0, argument_filters={"providerId": b32(provider_id)})
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
        logs = self.contract.events.EntityRegistered().get_logs(from_block=0)
        seen = {}
        for lg in logs:
            a = lg["args"]
            seen[unb32(a["entityId"])] = {"id": unb32(a["entityId"]), "kind": {1: "provider", 2: "payer"}[a["kind"]], "root": a["root"]}
        return list(seen.values())

    def delegations_for(self, entity_id: str) -> list[dict]:
        logs = self.contract.events.SignerDelegated().get_logs(
            from_block=0, argument_filters={"entityId": b32(entity_id)})
        out = []
        for lg in logs:
            d = self.contract.functions.delegations(lg["args"]["signer"]).call()
            out.append({"signer": lg["args"]["signer"], "notBefore": d[1], "notAfter": d[2], "revokedFrom": d[3]})
        return out


class RateIndex:
    """All committed rates loaded once and indexed by key. Use for bulk verification."""

    def __init__(self, chain: Chain, provider_id: str | None = None, payer_id: str | None = None):
        self.records = chain.rates_for(provider_id=provider_id, payer_id=payer_id)
        self.by_key: dict[tuple, list[RateRecord]] = {}
        for rec in self.records:
            r = rec.rate
            self.by_key.setdefault((r.payerId, r.planId, r.providerId, r.npi, r.code, r.modifier_, r.setting), []).append(rec)

    def find_rate(self, payer_id, plan_id, provider_id, npi, code, modifier, setting, at: int) -> list[RateRecord]:
        return [rec for rec in self.by_key.get((payer_id, plan_id, provider_id, npi, code, modifier, setting), [])
                if rec.rate.start <= at and (rec.rate.end == 0 or at < rec.rate.end)]

    def any_for(self, payer_id, plan_id, provider_id, npi, code, modifier, setting) -> list[RateRecord]:
        return self.by_key.get((payer_id, plan_id, provider_id, npi, code, modifier, setting), [])


def _signing_middleware(acct):
    """web3 v7 middleware: sign transactions from `acct` locally."""
    from web3.middleware import SignAndSendRawMiddlewareBuilder
    return SignAndSendRawMiddlewareBuilder.build(acct)


def _commit_receipts(self, provider_id: str):
    """Transaction receipts of every RateCommitted for a provider (gas accounting)."""
    logs = self.contract.events.RateCommitted().get_logs(from_block=0, argument_filters={"providerId": b32(provider_id)})
    return [self.w3.eth.get_transaction_receipt(lg["transactionHash"]) for lg in logs]


Chain.commit_receipts = _commit_receipts
