import time

import pytest
from eth_account import Account

from coral.chain import Chain
from coral.typed import Rate, ChargemasterVersion, sign_rate
from coral.verify import Line, verify_line

NOW = int(time.time())
DAY = 86400


@pytest.fixture
def world():
    ch = Chain.in_memory()
    registrar = ch.w3.eth.accounts[0]
    ch.deploy(registrar)
    prov_root, prov_sign = Account.create(), Account.create()
    pay_root, pay_sign = Account.create(), Account.create()
    ch.register_entity("TIN:84-0398876", "provider", prov_root.address, registrar)
    ch.register_entity("PAYER:UHC", "payer", pay_root.address, registrar)
    ch.delegate(prov_root.key, "TIN:84-0398876", prov_sign.address, NOW - DAY)
    ch.delegate(pay_root.key, "PAYER:UHC", pay_sign.address, NOW - DAY)
    return ch, registrar, prov_root, prov_sign, pay_root, pay_sign


def travel(ch, t: int):
    ch.w3.provider.ethereum_tester.time_travel(t)


def rate(**kw) -> Rate:
    base = dict(
        payerId="PAYER:UHC", planId="Broad Networks", providerId="TIN:84-0398876", npi="1790787307",
        code="CPT:70450", modifier_="", setting=2, billingClass=1, unit="", formula=0, value=114_615,
        start=NOW - 30 * DAY, end=NOW + 335 * DAY, contractVersion="example agreement, UHC", referenceVersion="",
    )
    base.update(kw)
    return Rate(**base)


def line(**kw) -> Line:
    base = dict(source="provider", payerId="PAYER:UHC", planId="Broad Networks", providerId="TIN:84-0398876",
                npi="1790787307", code="CPT:70450", modifier_="", setting=2, billingClass=1, unit="",
                amount_cents=114_615, percent_bp=None)
    base.update(kw)
    return Line(**base)


def test_commit_and_read(world):
    ch, _, _, ps, _, qs = world
    r = rate()
    rid = ch.commit_rate_with_keys(r, ps.key, qs.key)
    rec = ch.get_rate(rid)
    assert rec.rate == r
    assert rec.provider_signer == ps.address and rec.payer_signer == qs.address
    assert rec.committed_at > 0
    assert ch.get_rates([rid])[0].rate == r


def test_duplicate_rejected(world):
    ch, _, _, ps, _, qs = world
    ch.commit_rate_with_keys(rate(), ps.key, qs.key)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), ps.key, qs.key)


def test_unauthorized_key_rejected(world):
    ch, _, _, ps, _, _ = world
    stranger = Account.create()
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), ps.key, stranger.key)


def test_wrong_side_key_rejected(world):
    """A payer key cannot sign as the provider, even though it is delegated."""
    ch, _, _, ps, _, qs = world
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), qs.key, ps.key)


def test_revocation_blocks_future_commits(world):
    ch, _, prov_root, ps, _, qs = world
    ch.commit_rate_with_keys(rate(code="CPT:70450"), ps.key, qs.key)
    t = ch.w3.eth.get_block("latest").timestamp + 10
    ch.revoke(prov_root.key, "TIN:84-0398876", ps.address, t)
    travel(ch, t + 1)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(code="CPT:70460"), ps.key, qs.key)
    # the earlier record stays readable and its key was valid at its commit time
    recs = ch.rates_for(provider_id="TIN:84-0398876")
    assert len(recs) == 1
    assert ch.authorized_at("TIN:84-0398876", ps.address, recs[0].committed_at)
    assert not ch.authorized_at("TIN:84-0398876", ps.address, t + 1)


def test_backdated_revocation_rejected(world):
    """A root cannot revoke from before now, so it cannot repudiate a record its key signed."""
    ch, _, prov_root, ps, _, qs = world
    rid = ch.commit_rate_with_keys(rate(), ps.key, qs.key)
    committed = ch.get_rate(rid).committed_at
    with pytest.raises(Exception):
        ch.revoke(prov_root.key, "TIN:84-0398876", ps.address, committed - 1)
    assert ch.authorized_at("TIN:84-0398876", ps.address, committed)


def test_delegation_window(world):
    ch, _, prov_root, _, _, qs = world
    future = Account.create()
    ch.delegate(prov_root.key, "TIN:84-0398876", future.address, NOW + 10 * DAY)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), future.key, qs.key)
    assert not ch.authorized_at("TIN:84-0398876", future.address, NOW)
    assert ch.authorized_at("TIN:84-0398876", future.address, NOW + 11 * DAY)


def test_root_key_cannot_sign_rates(world):
    """Root keys delegate and revoke only. They never sign a rate."""
    ch, _, prov_root, _, _, qs = world
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), prov_root.key, qs.key)


def test_signature_bound_to_deployment(world):
    ch, registrar, _, ps, _, qs = world
    # second deployment on the same chain: different contract address, so a different domain
    other = Chain(ch.w3)
    other.deploy(registrar)
    assert other.contract.address != ch.contract.address
    r = rate()
    sig_other = sign_rate(ps.key, other.domain, r)
    with pytest.raises(Exception):
        ch.commit_rate(r, sig_other, sign_rate(qs.key, ch.domain, r))


def test_root_replacement_waits_and_can_be_vetoed(world):
    ch, registrar, prov_root, ps, _, qs = world
    new_root = Account.create()
    ch.propose_root("TIN:84-0398876", new_root.address, registrar)
    with pytest.raises(Exception):
        ch.activate_root("TIN:84-0398876")          # delay not passed
    ch.veto_root(prov_root.key, "TIN:84-0398876", new_root.address)
    with pytest.raises(Exception):
        ch.activate_root("TIN:84-0398876")          # vetoed: nothing pending
    assert ch.entity("TIN:84-0398876")["root"] == prov_root.address


def test_root_replacement_after_delay(world):
    ch, registrar, _, ps, _, qs = world
    new_root = Account.create()
    ch.propose_root("TIN:84-0398876", new_root.address, registrar)
    travel(ch, ch.w3.eth.get_block("latest").timestamp + 8 * DAY)
    ch.activate_root("TIN:84-0398876")
    new_signer = Account.create()
    ch.delegate(new_root.key, "TIN:84-0398876", new_signer.address, NOW - 1)
    ch.commit_rate_with_keys(rate(), new_signer.key, qs.key)
    # the old signing key still works until the new root revokes it
    ch.commit_rate_with_keys(rate(code="CPT:70460"), ps.key, qs.key)


def test_overlapping_window_rejected(world):
    ch, _, _, ps, _, qs = world
    ch.commit_rate_with_keys(rate(value=100), ps.key, qs.key)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(value=200), ps.key, qs.key)                      # same window
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(value=200, start=NOW, end=0), ps.key, qs.key)   # open-ended, overlaps
    # adjacent windows are fine
    ch.commit_rate_with_keys(rate(value=200, start=NOW + 335 * DAY, end=0), ps.key, qs.key)
    # a different key is a different record
    ch.commit_rate_with_keys(rate(value=200, setting=1), ps.key, qs.key)
    ch.commit_rate_with_keys(rate(value=200, unit="1 ML"), ps.key, qs.key)


def test_supersede_amends_mid_window(world):
    ch, _, _, ps, _, qs = world
    old = ch.commit_rate_with_keys(rate(value=100), ps.key, qs.key)
    new = ch.commit_rate_with_keys(rate(value=120, start=NOW + 10 * DAY, supersedes=old), ps.key, qs.key)
    assert ch.get_rate(old).superseded_by == new
    assert ch.get_rate(old).effective_end == NOW + 10 * DAY
    assert verify_line(ch, line(amount_cents=100), NOW).status == "match"
    assert verify_line(ch, line(amount_cents=100), NOW + 11 * DAY).status == "amount_differs"
    assert verify_line(ch, line(amount_cents=120), NOW + 11 * DAY).status == "match"
    with pytest.raises(Exception):   # a record can be superseded once
        ch.commit_rate_with_keys(rate(value=130, start=NOW + 20 * DAY, supersedes=old), ps.key, qs.key)
    with pytest.raises(Exception):   # supersedes must name a record with the same key
        ch.commit_rate_with_keys(rate(value=130, setting=1, supersedes=new), ps.key, qs.key)


def test_batch_commit(world):
    ch, _, _, ps, _, qs = world
    rs = [rate(code=f"CPT:7045{i}") for i in range(5)]
    rcpt = ch.commit_rates(rs, [sign_rate(ps.key, ch.domain, r) for r in rs], [sign_rate(qs.key, ch.domain, r) for r in rs])
    assert rcpt.status == 1
    assert len(ch.rates_for(provider_id="TIN:84-0398876")) == 5
    # one bad signature fails the whole batch
    bad = [sign_rate(qs.key, ch.domain, r) for r in rs]
    bad[2] = sign_rate(Account.create().key, ch.domain, rs[2])
    rs2 = [rate(code=f"CPT:7046{i}") for i in range(5)]
    with pytest.raises(Exception):
        ch.commit_rates(rs2, [sign_rate(ps.key, ch.domain, r) for r in rs2], bad)
    assert len(ch.rates_for(provider_id="TIN:84-0398876")) == 5


def test_verify_fixed(world):
    ch, _, _, ps, _, qs = world
    ch.commit_rate_with_keys(rate(value=114_615), ps.key, qs.key)
    assert verify_line(ch, line(), NOW).status == "match"
    assert verify_line(ch, line(amount_cents=129_010), NOW).status == "amount_differs"
    assert verify_line(ch, line(code="CPT:70470"), NOW).status == "no_record"
    assert verify_line(ch, line(unit="1 ML"), NOW).status == "no_record"
    assert verify_line(ch, line(), NOW + 400 * DAY).status == "expired"


def test_verify_percent_of_charges(world):
    ch, _, _, ps, _, qs = world
    cm = ChargemasterVersion("TIN:84-0398876", "example chargemaster", b"\x11" * 32, NOW - 60 * DAY)
    ch.commit_chargemaster(cm, ps.key)
    ch.commit_rate_with_keys(rate(formula=1, value=8_440, referenceVersion="example chargemaster"), ps.key, qs.key)
    ln = line(gross_charge_cents=135_800)
    v = verify_line(ch, ln, NOW)
    assert v.status == "match" and v.expected_cents == 114_615
    ln.gross_charge_cents = None
    assert verify_line(ch, ln, NOW).status == "needs_chargemaster"
    assert ch.chargemasters_for("TIN:84-0398876")[0]["version"] == "example chargemaster"
