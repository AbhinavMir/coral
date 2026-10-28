"""EIP-712 typed data for CORAL records.

Every record is signed as typed data so a signing tool shows the fields to the
person who signs. The domain binds chain id and contract address, so a
signature for one deployment is not valid on another.
"""
from dataclasses import dataclass, asdict

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

SETTING = {"inpatient": 1, "outpatient": 2, "professional": 3}
SETTING_NAME = {v: k for k, v in SETTING.items()}
FORMULA = {"fixed": 0, "percent_of_charges": 1, "percent_of_reference": 2}
FORMULA_NAME = {v: k for k, v in FORMULA.items()}
KIND = {"provider": 1, "payer": 2}

TYPES = {
    "Delegation": [
        {"name": "entityId", "type": "bytes32"},
        {"name": "signer", "type": "address"},
        {"name": "notBefore", "type": "uint64"},
        {"name": "notAfter", "type": "uint64"},
    ],
    "Revocation": [
        {"name": "entityId", "type": "bytes32"},
        {"name": "signer", "type": "address"},
        {"name": "effectiveFrom", "type": "uint64"},
    ],
    "Rate": [
        {"name": "payerId", "type": "bytes32"},
        {"name": "planId", "type": "bytes32"},
        {"name": "providerId", "type": "bytes32"},
        {"name": "npi", "type": "bytes32"},
        {"name": "code", "type": "bytes32"},
        {"name": "modifier_", "type": "bytes32"},
        {"name": "setting", "type": "uint8"},
        {"name": "formula", "type": "uint8"},
        {"name": "value", "type": "uint64"},
        {"name": "start", "type": "uint64"},
        {"name": "end", "type": "uint64"},
        {"name": "contractVersion", "type": "bytes32"},
        {"name": "referenceVersion", "type": "bytes32"},
    ],
    "ChargemasterVersion": [
        {"name": "providerId", "type": "bytes32"},
        {"name": "version", "type": "bytes32"},
        {"name": "fileHash", "type": "bytes32"},
        {"name": "effective", "type": "uint64"},
    ],
}


def b32(text: str) -> bytes:
    """Short identifier (NPI, code, plan id) as right-padded bytes32.

    Identifiers longer than 32 bytes are hashed; the ledger then holds the hash
    and the file must carry the full text.
    """
    raw = text.encode()
    if len(raw) > 32:
        return keccak(raw)
    return raw.ljust(32, b"\0")


def unb32(b: bytes) -> str:
    return bytes(b).rstrip(b"\0").decode(errors="replace")


@dataclass
class Rate:
    payerId: str
    planId: str
    providerId: str
    npi: str
    code: str
    modifier_: str
    setting: int
    formula: int
    value: int        # cents for fixed; basis points for percent (10000 = 100%)
    start: int        # unix seconds, inclusive
    end: int          # unix seconds, exclusive; 0 = open
    contractVersion: str
    referenceVersion: str

    def message(self) -> dict:
        d = asdict(self)
        for k in ("payerId", "planId", "providerId", "npi", "code", "modifier_", "contractVersion", "referenceVersion"):
            d[k] = b32(d[k])
        return d

    def tuple(self) -> tuple:
        m = self.message()
        return tuple(m[f["name"]] for f in TYPES["Rate"])


@dataclass
class ChargemasterVersion:
    providerId: str
    version: str
    fileHash: bytes
    effective: int

    def message(self) -> dict:
        return {
            "providerId": b32(self.providerId),
            "version": b32(self.version),
            "fileHash": self.fileHash,
            "effective": self.effective,
        }

    def tuple(self) -> tuple:
        m = self.message()
        return (m["providerId"], m["version"], m["fileHash"], m["effective"])


def domain(chain_id: int, contract: str) -> dict:
    return {
        "name": "CORAL",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": to_checksum_address(contract),
    }


def sign(key, dom: dict, primary: str, message: dict) -> bytes:
    """Sign one typed record with a private key. Returns the 65-byte signature."""
    signable = encode_typed_data(dom, {primary: TYPES[primary]}, message)
    return Account.sign_message(signable, private_key=key).signature


def sign_delegation(root_key, dom, entity_id: str, signer: str, not_before: int, not_after: int) -> bytes:
    return sign(root_key, dom, "Delegation", {
        "entityId": b32(entity_id), "signer": to_checksum_address(signer),
        "notBefore": not_before, "notAfter": not_after,
    })


def sign_revocation(root_key, dom, entity_id: str, signer: str, effective_from: int) -> bytes:
    return sign(root_key, dom, "Revocation", {
        "entityId": b32(entity_id), "signer": to_checksum_address(signer), "effectiveFrom": effective_from,
    })


def sign_rate(key, dom, rate: Rate) -> bytes:
    return sign(key, dom, "Rate", rate.message())


def sign_chargemaster(key, dom, cm: ChargemasterVersion) -> bytes:
    return sign(key, dom, "ChargemasterVersion", cm.message())
