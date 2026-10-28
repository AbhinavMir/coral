// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// CORAL: Committed Ratesheet on Ledger.
///
/// One registry. Four record types.
///   Entity        registrar binds an entity id (TIN, payer id) to a root key
///   Delegation    root key authorizes a signing key for a time window
///   Revocation    root key ends a signing key from a given time
///   Rate          provider signing key and payer signing key both sign one rate
///   Chargemaster  provider signing key signs a chargemaster version hash
///
/// Every action carries EIP-712 signatures. The transaction sender is a relayer
/// and holds no authority. Validity of a signing key is judged at the block
/// timestamp of the commit, which consensus sets, not the signer.
contract RateRegistry {
    // ---- types -----------------------------------------------------------

    enum Kind { None, Provider, Payer }
    enum Formula { Fixed, PercentOfCharges, PercentOfReference }

    struct Entity {
        Kind kind;
        address root;
        uint64 registeredAt;
    }

    struct Delegation {
        bytes32 entityId;
        uint64 notBefore;
        uint64 notAfter;     // 0 = no end
        uint64 revokedFrom;  // 0 = not revoked
    }

    struct Rate {
        bytes32 payerId;
        bytes32 planId;
        bytes32 providerId;      // contracting entity (TIN)
        bytes32 npi;             // billing NPI the line applies to
        bytes32 code;            // CPT/HCPCS/DRG, e.g. "CPT:99213"
        bytes32 modifier_;       // "" when none
        uint8 setting;           // 1 inpatient, 2 outpatient, 3 professional
        uint8 formula;           // Formula
        uint64 value;            // cents for Fixed; basis points for percent
        uint64 start;            // unix seconds, inclusive
        uint64 end;              // unix seconds, exclusive; 0 = open
        bytes32 contractVersion; // hash or label of the contract version
        bytes32 referenceVersion;// chargemaster or fee-schedule version id
    }

    struct Committed {
        uint64 committedAt;
        address providerSigner;
        address payerSigner;
    }

    struct ChargemasterVersion {
        bytes32 providerId;
        bytes32 version;
        bytes32 fileHash;
        uint64 effective;
    }

    // ---- EIP-712 ----------------------------------------------------------

    bytes32 private constant DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 public constant DELEGATION_TYPEHASH = keccak256(
        "Delegation(bytes32 entityId,address signer,uint64 notBefore,uint64 notAfter)"
    );
    bytes32 public constant REVOCATION_TYPEHASH = keccak256(
        "Revocation(bytes32 entityId,address signer,uint64 effectiveFrom)"
    );
    bytes32 public constant RATE_TYPEHASH = keccak256(
        "Rate(bytes32 payerId,bytes32 planId,bytes32 providerId,bytes32 npi,bytes32 code,bytes32 modifier_,uint8 setting,uint8 formula,uint64 value,uint64 start,uint64 end,bytes32 contractVersion,bytes32 referenceVersion)"
    );
    bytes32 public constant CHARGEMASTER_TYPEHASH = keccak256(
        "ChargemasterVersion(bytes32 providerId,bytes32 version,bytes32 fileHash,uint64 effective)"
    );

    bytes32 public immutable DOMAIN_SEPARATOR;

    // ---- state ------------------------------------------------------------

    address public registrar;
    mapping(bytes32 => Entity) public entities;
    mapping(address => Delegation) public delegations;      // signer -> delegation
    mapping(bytes32 => Committed) public rates;             // rate id -> commit
    mapping(bytes32 => Rate) public rateBodies;             // rate id -> body
    mapping(bytes32 => ChargemasterVersion) public chargemasters; // cm id -> body
    mapping(bytes32 => uint64) public chargemasterCommittedAt;

    // ---- events -----------------------------------------------------------

    event EntityRegistered(bytes32 indexed entityId, uint8 kind, address root);
    event SignerDelegated(bytes32 indexed entityId, address indexed signer, uint64 notBefore, uint64 notAfter);
    event SignerRevoked(bytes32 indexed entityId, address indexed signer, uint64 effectiveFrom);
    event RateCommitted(bytes32 indexed rateId, bytes32 indexed providerId, bytes32 indexed payerId, bytes32 planId, bytes32 code);
    event ChargemasterCommitted(bytes32 indexed cmId, bytes32 indexed providerId, bytes32 version, bytes32 fileHash, uint64 effective);

    // ---- errors -----------------------------------------------------------

    error NotRegistrar();
    error EntityExists();
    error UnknownEntity();
    error WrongKind();
    error BadSignature();
    error SignerNotAuthorized(address signer);
    error AlreadyCommitted();
    error DelegationExists();

    constructor(address registrar_) {
        registrar = registrar_;
        DOMAIN_SEPARATOR = keccak256(abi.encode(
            DOMAIN_TYPEHASH,
            keccak256("CORAL"),
            keccak256("1"),
            block.chainid,
            address(this)
        ));
    }

    // ---- registrar --------------------------------------------------------

    /// CMS (registrar) binds an entity id to its root key after out-of-band
    /// identity proofing (for example PECOS / I&A for providers, CCIIO for payers).
    function registerEntity(bytes32 entityId, Kind kind, address root) external {
        if (msg.sender != registrar) revert NotRegistrar();
        if (entities[entityId].kind != Kind.None) revert EntityExists();
        if (kind == Kind.None) revert WrongKind();
        entities[entityId] = Entity(kind, root, uint64(block.timestamp));
        emit EntityRegistered(entityId, uint8(kind), root);
    }

    /// Root key replacement after loss. Registrar only. Existing delegations
    /// made under the old root stay valid until revoked by the new root.
    function replaceRoot(bytes32 entityId, address newRoot) external {
        if (msg.sender != registrar) revert NotRegistrar();
        if (entities[entityId].kind == Kind.None) revert UnknownEntity();
        entities[entityId].root = newRoot;
        emit EntityRegistered(entityId, uint8(entities[entityId].kind), newRoot);
    }

    // ---- keys -------------------------------------------------------------

    function delegate(
        bytes32 entityId, address signer, uint64 notBefore, uint64 notAfter, bytes calldata rootSig
    ) external {
        Entity memory e = entities[entityId];
        if (e.kind == Kind.None) revert UnknownEntity();
        if (delegations[signer].entityId != bytes32(0)) revert DelegationExists();
        bytes32 structHash = keccak256(abi.encode(DELEGATION_TYPEHASH, entityId, signer, notBefore, notAfter));
        if (_recover(structHash, rootSig) != e.root) revert BadSignature();
        delegations[signer] = Delegation(entityId, notBefore, notAfter, 0);
        emit SignerDelegated(entityId, signer, notBefore, notAfter);
    }

    function revoke(bytes32 entityId, address signer, uint64 effectiveFrom, bytes calldata rootSig) external {
        Entity memory e = entities[entityId];
        if (e.kind == Kind.None) revert UnknownEntity();
        Delegation storage d = delegations[signer];
        if (d.entityId != entityId) revert SignerNotAuthorized(signer);
        bytes32 structHash = keccak256(abi.encode(REVOCATION_TYPEHASH, entityId, signer, effectiveFrom));
        if (_recover(structHash, rootSig) != e.root) revert BadSignature();
        d.revokedFrom = effectiveFrom;
        emit SignerRevoked(entityId, signer, effectiveFrom);
    }

    /// True when `signer` was an authorized key of `entityId` at time `t`.
    function authorizedAt(bytes32 entityId, address signer, uint64 t) public view returns (bool) {
        Delegation memory d = delegations[signer];
        if (d.entityId != entityId) return false;
        if (t < d.notBefore) return false;
        if (d.notAfter != 0 && t >= d.notAfter) return false;
        if (d.revokedFrom != 0 && t >= d.revokedFrom) return false;
        return true;
    }

    // ---- records ----------------------------------------------------------

    function rateId(Rate calldata r) public pure returns (bytes32) {
        return keccak256(abi.encode(
            RATE_TYPEHASH,
            r.payerId, r.planId, r.providerId, r.npi, r.code, r.modifier_,
            r.setting, r.formula, r.value, r.start, r.end,
            r.contractVersion, r.referenceVersion
        ));
    }

    /// Commit one rate with both signatures. Anyone can relay the transaction.
    function commitRate(Rate calldata r, bytes calldata providerSig, bytes calldata payerSig) external returns (bytes32 id) {
        id = rateId(r);
        if (rates[id].committedAt != 0) revert AlreadyCommitted();
        if (entities[r.providerId].kind != Kind.Provider) revert WrongKind();
        if (entities[r.payerId].kind != Kind.Payer) revert WrongKind();

        uint64 now_ = uint64(block.timestamp);
        address ps = _recover(id, providerSig);
        if (!authorizedAt(r.providerId, ps, now_)) revert SignerNotAuthorized(ps);
        address qs = _recover(id, payerSig);
        if (!authorizedAt(r.payerId, qs, now_)) revert SignerNotAuthorized(qs);

        rates[id] = Committed(now_, ps, qs);
        rateBodies[id] = r;
        emit RateCommitted(id, r.providerId, r.payerId, r.planId, r.code);
    }

    function chargemasterId(ChargemasterVersion calldata c) public pure returns (bytes32) {
        return keccak256(abi.encode(CHARGEMASTER_TYPEHASH, c.providerId, c.version, c.fileHash, c.effective));
    }

    /// Provider signs a chargemaster version. Needed to recompute percent-of-charges rates.
    function commitChargemaster(ChargemasterVersion calldata c, bytes calldata providerSig) external returns (bytes32 id) {
        id = chargemasterId(c);
        if (chargemasterCommittedAt[id] != 0) revert AlreadyCommitted();
        if (entities[c.providerId].kind != Kind.Provider) revert WrongKind();
        uint64 now_ = uint64(block.timestamp);
        address ps = _recover(id, providerSig);
        if (!authorizedAt(c.providerId, ps, now_)) revert SignerNotAuthorized(ps);
        chargemasters[id] = c;
        chargemasterCommittedAt[id] = now_;
        emit ChargemasterCommitted(id, c.providerId, c.version, c.fileHash, c.effective);
    }

    // ---- internal ---------------------------------------------------------

    function _recover(bytes32 structHash, bytes calldata sig) internal view returns (address) {
        if (sig.length != 65) revert BadSignature();
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR, structHash));
        bytes32 r; bytes32 s; uint8 v;
        assembly {
            r := calldataload(sig.offset)
            s := calldataload(add(sig.offset, 32))
            v := byte(0, calldataload(add(sig.offset, 64)))
        }
        if (v < 27) v += 27;
        address a = ecrecover(digest, v, r, s);
        if (a == address(0)) revert BadSignature();
        return a;
    }
}
