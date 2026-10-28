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
    ch.register_entity("TIN:12-3456789", "provider", prov_root.address, registrar)
    ch.register_entity("PAYER:UHC", "payer", pay_root.address, registrar)
    ch.delegate(prov_root.key, "TIN:12-3456789", prov_sign.address, NOW - DAY)
    ch.delegate(pay_root.key, "PAYER:UHC", pay_sign.address, NOW - DAY)
    return ch, registrar, prov_root, prov_sign, pay_root, pay_sign


def rate(**kw) -> Rate:
    base = dict(
        payerId="PAYER:UHC", planId="PLAN:UHC-CHOICE-PLUS", providerId="TIN:12-3456789", npi="1234567893",
        code="CPT:99213", modifier_="", setting=2, formula=0, value=12_500,
        start=NOW - 30 * DAY, end=NOW + 335 * DAY, contractVersion="C2026-01", referenceVersion="",
    )
    base.update(kw)
    return Rate(**base)


def test_commit_and_read(world):
    ch, _, _, ps, _, qs = world
    r = rate()
    rid = ch.commit_rate_with_keys(r, ps.key, qs.key)
    rec = ch.get_rate(rid)
    assert rec.rate == r
    assert rec.provider_signer == ps.address and rec.payer_signer == qs.address
    assert rec.committed_at > 0


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
    ch.commit_rate_with_keys(rate(code="CPT:99213"), ps.key, qs.key)
    ch.revoke(prov_root.key, "TIN:12-3456789", ps.address, NOW - 1)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(code="CPT:99214"), ps.key, qs.key)
    # the earlier record stays readable and valid
    assert len(ch.rates_for(provider_id="TIN:12-3456789")) == 1


def test_delegation_window(world):
    ch, _, prov_root, _, _, qs = world
    future = Account.create()
    ch.delegate(prov_root.key, "TIN:12-3456789", future.address, NOW + 10 * DAY)
    with pytest.raises(Exception):
        ch.commit_rate_with_keys(rate(), future.key, qs.key)
    assert not ch.authorized_at("TIN:12-3456789", future.address, NOW)
    assert ch.authorized_at("TIN:12-3456789", future.address, NOW + 11 * DAY)


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


def test_replace_root_after_loss(world):
    ch, registrar, _, ps, _, qs = world
    new_root = Account.create()
    ch.replace_root("TIN:12-3456789", new_root.address, registrar)
    new_signer = Account.create()
    ch.delegate(new_root.key, "TIN:12-3456789", new_signer.address, NOW - 1)
    ch.commit_rate_with_keys(rate(), new_signer.key, qs.key)


def test_verify_fixed(world):
    ch, _, _, ps, _, qs = world
    ch.commit_rate_with_keys(rate(value=12_500), ps.key, qs.key)
    line = Line("hospital", "PAYER:UHC", "PLAN:UHC-CHOICE-PLUS", "TIN:12-3456789", "1234567893",
                "CPT:99213", "", 2, amount_cents=12_500, percent_bp=None)
    assert verify_line(ch, line, NOW).status == "match"
    line.amount_cents = 13_000
    assert verify_line(ch, line, NOW).status == "amount_differs"
    line.code = "CPT:99215"
    assert verify_line(ch, line, NOW).status == "no_record"
    assert verify_line(ch, Line("hospital", "PAYER:UHC", "PLAN:UHC-CHOICE-PLUS", "TIN:12-3456789", "1234567893",
                                "CPT:99213", "", 2, 12_500, None), NOW + 400 * DAY).status == "expired"


def test_verify_percent_of_charges(world):
    ch, _, _, ps, _, qs = world
    cm = ChargemasterVersion("TIN:12-3456789", "CDM-2026-Q1", b"\x11" * 32, NOW - 60 * DAY)
    ch.commit_chargemaster(cm, ps.key)
    ch.commit_rate_with_keys(rate(formula=1, value=4_500, referenceVersion="CDM-2026-Q1"), ps.key, qs.key)
    line = Line("hospital", "PAYER:UHC", "PLAN:UHC-CHOICE-PLUS", "TIN:12-3456789", "1234567893",
                "CPT:99213", "", 2, amount_cents=9_000, percent_bp=None, gross_charge_cents=20_000)
    v = verify_line(ch, line, NOW)
    assert v.status == "match" and v.expected_cents == 9_000
    line.gross_charge_cents = None
    assert verify_line(ch, line, NOW).status == "needs_chargemaster"
    assert ch.chargemasters_for("TIN:12-3456789")[0]["version"] == "CDM-2026-Q1"
