// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// CORAL: Committed Ratesheet on Ledger.
///
/// One registry. Five record types.
///   Entity        registrar binds an entity id (TIN, payer id) to a root key
///   Delegation    root key authorizes a signing key for a time window
///   Revocation    root key ends a signing key from a time at or after now
///   Rate          provider signing key and payer signing key both sign one rate
///   Chargemaster  provider signing key signs a chargemaster version hash
///
/// Every action carries EIP-712 signatures. The transaction sender is a relayer
/// and holds no authority. Validity of a signing key is judged at the block
/// timestamp of the commit, which consensus sets, not the signer.
///
/// One live record per key at any time. A record names the record it replaces
/// (supersedes); the old record then ends where the new one starts. Two
/// records for one key with overlapping windows and no supersedes link are
/// rejected, so a verifier never finds two answers for one date.
contract RateRegistry {
    // ---- types -----------------------------------------------------------

    enum Kind { None, Provider, Payer }
    enum Formula { Fixed, PercentOfCharges, PercentOfReference }

    struct Entity {
        Kind kind;
        address root;
        uint64 registeredAt;
    }

    struct PendingRoot {
        address newRoot;
        uint64 activatesAt;
    }

    struct Delegation {
        bytes32 entityId;
        uint64 notBefore;
        uint64 notAfter;     // 0 = no end
        uint64 revokedFrom;  // 0 = not revoked
    }

    struct Rate {
        // key: these fields identify one rate
        bytes32 payerId;
        bytes32 planId;
        bytes32 providerId;      // contracting entity (TIN)
        bytes32 npi;             // billing NPI the line applies to
        bytes32 code;            // CPT/HCPCS/DRG, e.g. "CPT:99213"
        bytes32 modifier_;       // "" when none
        uint8 setting;           // 1 inpatient, 2 outpatient
        uint8 billingClass;      // 1 facility, 2 professional
        bytes32 unit;            // drug unit of measure, e.g. "1 ML"; "" when none
        // value
        uint8 formula;           // Formula
        uint64 value;            // cents for Fixed; basis points for percent
        uint64 start;            // unix seconds, inclusive
        uint64 end;              // unix seconds, exclusive; 0 = open
        bytes32 contractVersion; // hash or label of the contract version
        bytes32 referenceVersion;// chargemaster or fee-schedule version id
        bytes32 supersedes;      // id of the record this one replaces; 0 = none
    }

    struct Committed {
        uint64 committedAt;
        address providerSigner;
        address payerSigner;
        bytes32 supersededBy;    // 0 = live to its own end
        uint64 supersededAt;     // start of the superseding record
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
    bytes32 public constant ROOT_VETO_TYPEHASH = keccak256(
        "RootVeto(bytes32 entityId,address newRoot,uint64 activatesAt)"
    );
    bytes32 public constant RATE_TYPEHASH = keccak256(
        "Rate(bytes32 payerId,bytes32 planId,bytes32 providerId,bytes32 npi,bytes32 code,bytes32 modifier_,uint8 setting,uint8 billingClass,bytes32 unit,uint8 formula,uint64 value,uint64 start,uint64 end,bytes32 contractVersion,bytes32 referenceVersion,bytes32 supersedes)"
    );
    bytes32 public constant CHARGEMASTER_TYPEHASH = keccak256(
        "ChargemasterVersion(bytes32 providerId,bytes32 version,bytes32 fileHash,uint64 effective)"
    );

    bytes32 public immutable DOMAIN_SEPARATOR;

    /// Delay between a registrar's root replacement and its activation. The
    /// current root can veto during the delay.
    uint64 public constant ROOT_DELAY = 7 days;

    // ---- state ------------------------------------------------------------

    address public registrar;
    mapping(bytes32 => Entity) public entities;
    mapping(bytes32 => PendingRoot) public pendingRoots;
    mapping(address => Delegation) public delegations;      // signer -> delegation
    mapping(bytes32 => Committed) public rates;             // rate id -> commit
    mapping(bytes32 => Rate) public rateBodies;             // rate id -> body
    mapping(bytes32 => bytes32[]) public keyRecords;        // key hash -> rate ids
    mapping(bytes32 => ChargemasterVersion) public chargemasters; // cm id -> body
    mapping(bytes32 => uint64) public chargemasterCommittedAt;

    // ---- events -----------------------------------------------------------

    event EntityRegistered(bytes32 indexed entityId, uint8 kind, address root);
    event RootProposed(bytes32 indexed entityId, address newRoot, uint64 activatesAt);
    event RootVetoed(bytes32 indexed entityId, address newRoot);
    event SignerDelegated(bytes32 indexed entityId, address indexed signer, uint64 notBefore, uint64 notAfter);
    event SignerRevoked(bytes32 indexed entityId, address indexed signer, uint64 effectiveFrom);
    event RateCommitted(bytes32 indexed rateId, bytes32 indexed providerId, bytes32 indexed payerId, bytes32 planId, bytes32 code);
    event RateSuperseded(bytes32 indexed rateId, bytes32 indexed by, uint64 at);
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
    error BackdatedRevocation();
    error NoPendingRoot();
    error RootDelayNotPassed();
    error UnknownRecord();
    error WrongKey();
    error AlreadySuperseded();
    error OverlappingWindow(bytes32 existing);
    error BadWindow();
    error LengthMismatch();
    error RevocationNotEarlier();
    error RootCannotSign();

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

    /// Root key replacement after loss. Registrar proposes; the change takes
    /// effect after ROOT_DELAY unless the current root vetoes it. Existing
    /// delegations made under the old root stay valid until revoked.
    function proposeRoot(bytes32 entityId, address newRoot) external {
        if (msg.sender != registrar) revert NotRegistrar();
        if (entities[entityId].kind == Kind.None) revert UnknownEntity();
        uint64 at = uint64(block.timestamp) + ROOT_DELAY;
        pendingRoots[entityId] = PendingRoot(newRoot, at);
        emit RootProposed(entityId, newRoot, at);
    }

    function activateRoot(bytes32 entityId) external {
        PendingRoot memory p = pendingRoots[entityId];
        if (p.newRoot == address(0)) revert NoPendingRoot();
        if (block.timestamp < p.activatesAt) revert RootDelayNotPassed();
        entities[entityId].root = p.newRoot;
        delete pendingRoots[entityId];
        emit EntityRegistered(entityId, uint8(entities[entityId].kind), p.newRoot);
    }

    /// The current root cancels a pending replacement. Needs the root's signature.
    function vetoRoot(bytes32 entityId, bytes calldata rootSig) external {
        PendingRoot memory p = pendingRoots[entityId];
        if (p.newRoot == address(0)) revert NoPendingRoot();
        bytes32 structHash = keccak256(abi.encode(ROOT_VETO_TYPEHASH, entityId, p.newRoot, p.activatesAt));
        if (_recover(structHash, rootSig) != entities[entityId].root) revert BadSignature();
        delete pendingRoots[entityId];
        emit RootVetoed(entityId, p.newRoot);
    }

    // ---- keys -------------------------------------------------------------

    function delegate(
        bytes32 entityId, address signer, uint64 notBefore, uint64 notAfter, bytes calldata rootSig
    ) external {
        Entity memory e = entities[entityId];
        if (e.kind == Kind.None) revert UnknownEntity();
        if (delegations[signer].entityId != bytes32(0)) revert DelegationExists();
        if (signer == e.root) revert RootCannotSign();
        if (notAfter != 0 && notAfter <= notBefore) revert BadWindow();
        bytes32 structHash = keccak256(abi.encode(DELEGATION_TYPEHASH, entityId, signer, notBefore, notAfter));
        if (_recover(structHash, rootSig) != e.root) revert BadSignature();
        delegations[signer] = Delegation(entityId, notBefore, notAfter, 0);
        emit SignerDelegated(entityId, signer, notBefore, notAfter);
    }

    /// A revocation takes effect at or after the block in which it is committed.
    /// A root cannot backdate a revocation to before a record its key signed.
    /// A second revocation can only move the time earlier, so a signed
    /// revocation for a later time cannot be replayed to reopen the key.
    function revoke(bytes32 entityId, address signer, uint64 effectiveFrom, bytes calldata rootSig) external {
        Entity memory e = entities[entityId];
        if (e.kind == Kind.None) revert UnknownEntity();
        if (effectiveFrom < block.timestamp) revert BackdatedRevocation();
        Delegation storage d = delegations[signer];
        if (d.entityId != entityId) revert SignerNotAuthorized(signer);
        bytes32 structHash = keccak256(abi.encode(REVOCATION_TYPEHASH, entityId, signer, effectiveFrom));
        if (_recover(structHash, rootSig) != e.root) revert BadSignature();
        if (d.revokedFrom != 0 && effectiveFrom >= d.revokedFrom) revert RevocationNotEarlier();
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

    /// EIP-712 struct hash. abi.encode of static fields is a concatenation of
    /// 32-byte words, so two encodes joined equal one encode of all fields.
    function rateId(Rate calldata r) public pure returns (bytes32) {
        return keccak256(bytes.concat(
            abi.encode(RATE_TYPEHASH, r.payerId, r.planId, r.providerId, r.npi, r.code, r.modifier_, r.setting, r.billingClass),
            abi.encode(r.unit, r.formula, r.value, r.start, r.end, r.contractVersion, r.referenceVersion, r.supersedes)
        ));
    }

    /// Hash of the fields that identify one rate.
    function rateKey(Rate calldata r) public pure returns (bytes32) {
        return keccak256(abi.encode(
            r.payerId, r.planId, r.providerId, r.npi, r.code, r.modifier_, r.setting, r.billingClass, r.unit
        ));
    }

    function rateKeyOf(bytes32 id) public view returns (bytes32) {
        Rate storage r = rateBodies[id];
        return keccak256(abi.encode(
            r.payerId, r.planId, r.providerId, r.npi, r.code, r.modifier_, r.setting, r.billingClass, r.unit
        ));
    }

    /// Effective window end of a committed record: its own end, or the start of
    /// the record that superseded it.
    function effectiveEnd(bytes32 id) public view returns (uint64) {
        Committed storage c = rates[id];
        if (c.supersededBy != bytes32(0)) return c.supersededAt;
        return rateBodies[id].end;
    }

    /// Commit one rate with both signatures. Anyone can relay the transaction.
    function commitRate(Rate calldata r, bytes calldata providerSig, bytes calldata payerSig) external returns (bytes32 id) {
        return _commit(r, providerSig, payerSig);
    }

    /// Commit many rates in one transaction. All succeed or none.
    function commitRates(Rate[] calldata rs, bytes[] calldata providerSigs, bytes[] calldata payerSigs) external {
        if (rs.length != providerSigs.length || rs.length != payerSigs.length) revert LengthMismatch();
        for (uint256 i = 0; i < rs.length; i++) {
            _commit(rs[i], providerSigs[i], payerSigs[i]);
        }
    }

    function _commit(Rate calldata r, bytes calldata providerSig, bytes calldata payerSig) internal returns (bytes32 id) {
        id = rateId(r);
        if (rates[id].committedAt != 0) revert AlreadyCommitted();
        if (entities[r.providerId].kind != Kind.Provider) revert WrongKind();
        if (entities[r.payerId].kind != Kind.Payer) revert WrongKind();
        if (r.end != 0 && r.end <= r.start) revert BadWindow();

        uint64 now_ = uint64(block.timestamp);
        address ps = _recover(id, providerSig);
        if (!authorizedAt(r.providerId, ps, now_)) revert SignerNotAuthorized(ps);
        address qs = _recover(id, payerSig);
        if (!authorizedAt(r.payerId, qs, now_)) revert SignerNotAuthorized(qs);

        bytes32 k = rateKey(r);
        if (r.supersedes != bytes32(0)) _supersede(r, id, k);
        _checkOverlap(r, k);

        rates[id] = Committed(now_, ps, qs, bytes32(0), 0);
        rateBodies[id] = r;
        keyRecords[k].push(id);
        emit RateCommitted(id, r.providerId, r.payerId, r.planId, r.code);
    }

    function _supersede(Rate calldata r, bytes32 id, bytes32 k) internal {
        bytes32 old = r.supersedes;
        Committed storage c = rates[old];
        if (c.committedAt == 0) revert UnknownRecord();
        if (rateKeyOf(old) != k) revert WrongKey();
        if (c.supersededBy != bytes32(0)) revert AlreadySuperseded();
        if (r.start < rateBodies[old].start) revert BadWindow();
        uint64 oldEnd = rateBodies[old].end;
        if (oldEnd != 0 && r.start >= oldEnd) revert BadWindow();   // the old record is not live at the new start
        c.supersededBy = id;
        c.supersededAt = r.start;
        emit RateSuperseded(old, id, r.start);
    }

    function _checkOverlap(Rate calldata r, bytes32 k) internal view {
        bytes32[] storage ids = keyRecords[k];
        for (uint256 i = 0; i < ids.length; i++) {
            bytes32 other = ids[i];
            uint64 s = rateBodies[other].start;
            uint64 e = effectiveEnd(other);
            // [s, e) and [r.start, r.end) overlap when each starts before the other ends
            bool otherEndsFirst = e != 0 && e <= r.start;
            bool newEndsFirst = r.end != 0 && r.end <= s;
            if (!otherEndsFirst && !newEndsFirst) revert OverlappingWindow(other);
        }
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

    // ---- reads ------------------------------------------------------------

    /// Bodies and commit data for many ids in one call.
    function getRates(bytes32[] calldata ids) external view returns (Rate[] memory bodies, Committed[] memory commits) {
        bodies = new Rate[](ids.length);
        commits = new Committed[](ids.length);
        for (uint256 i = 0; i < ids.length; i++) {
            bodies[i] = rateBodies[ids[i]];
            commits[i] = rates[ids[i]];
        }
    }

    function keyRecordCount(bytes32 k) external view returns (uint256) {
        return keyRecords[k].length;
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
