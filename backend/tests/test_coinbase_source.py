from dataclasses import replace
from decimal import Decimal

import pytest

from tradebook.coinbase_products import classify_product
from tradebook.coinbase_source import CoinbaseSpotSource, CoinbaseCFMPerpetualSource, CoinbaseCFMDatedFutureSource
from tradebook.source import TradeSource
from tradebook.store import connect, initialize, update_reason, get_trade
from tradebook.sync import sync


def spec(kind="SPOT", expiry=None):
    product = {"product_id": "BTC-USD" if kind == "SPOT" else "BTC-CFM", "product_type": kind, "quote_currency_id": "USD"}
    if kind == "SPOT":
        product["base_currency_id"] = "BTC"
        family = "SPOT"
    else:
        product["future_product_details"] = {"contract_expiry_type": expiry, "risk_managed_by": "MANAGED_BY_FCM"}
        family = "CFM"
    return classify_product(product, account_family=family, source_reference="fixture:verified-product")


def row(i, side, time, *, product="BTC-USD", fee="0.1", **extra):
    return {"entry_id": i, "trade_id": "trade-" + i, "trade_time": time, "trade_type": "FILL",
            "price": "10", "size": "1", "commission": fee, "product_id": product,
            "sequence_timestamp": time, "size_in_quote": False, "side": side,
            "retail_portfolio_id": "acct", **extra}


def test_three_adapters_accept_verified_product_and_preserve_raw_evidence():
    sources = [CoinbaseSpotSource("acct", spec(), [row("b", "BUY", "2026-01-01T00:00:00Z"), row("s", "SELL", "2026-01-02T00:00:00Z")]),
               CoinbaseCFMPerpetualSource("acct", spec("FUTURE", "PERPETUAL"), [row("b", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM"), row("s", "SELL", "2026-01-02T00:00:00Z", product="BTC-CFM")]),
               CoinbaseCFMDatedFutureSource("acct", spec("FUTURE", "EXPIRING"), [row("b", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM"), row("s", "SELL", "2026-01-02T00:00:00Z", product="BTC-CFM")])]
    for source, kind in zip(sources, ("spot", "perpetual", "dated_future")):
        assert isinstance(source, TradeSource)
        packet, = source.iter_closes()
        assert packet.product_type == kind
        assert packet.fee_usd == (None if kind == "spot" else Decimal("0.2"))
        assert packet.fee_currency_assumed is (kind != "spot")
        assert packet.source_events[0].payload["commission"] == "0.1"
        assert packet.source_events[0].payload["retail_portfolio_id"] == "acct"
    assert [source.source_key for source in sources] == [
        "coinbase.spot", "coinbase.cfm.us_perpetual", "coinbase.cfm.dated_future"]


@pytest.mark.parametrize("expiry,source_class", [
    ("PERPETUAL", CoinbaseCFMPerpetualSource), ("EXPIRING", CoinbaseCFMDatedFutureSource)])
def test_cfm_commissions_are_assumed_usd_without_changing_raw_fills(expiry, source_class):
    fills = [row("entry", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM", fee="0.2"),
             row("exit", "SELL", "2026-01-02T00:00:00Z", product="BTC-CFM", fee="0.3")]
    packet, = source_class("acct", spec("FUTURE", expiry), fills).iter_closes()
    assert packet.fee_usd == Decimal("0.5")
    assert packet.fee_currency_assumed is True
    assert [event.payload["commission"] for event in packet.source_events] == ["0.2", "0.3"]
    assert all(event.payload.get("fee_currency") is None for event in packet.source_events)


def test_reproject_saved_two_fill_cfm_trade_retains_reason_and_evidence(tmp_path):
    from tradebook.coinbase_closes import assume_saved_cfm_commission_usd
    from tradebook.store import upsert_packet
    fills = [row("entry", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM", fee="0.2"),
             row("exit", "SELL", "2026-01-02T00:00:00Z", product="BTC-CFM", fee="0.3")]
    packet, = CoinbaseCFMDatedFutureSource("acct", spec("FUTURE", "EXPIRING"), fills).iter_closes()
    legacy = packet.model_copy(update={"fee_usd": None, "fee_currency_assumed": False})
    db = connect(tmp_path / "reproject.sqlite3"); initialize(db)
    trade_id = upsert_packet(db, legacy)
    update_reason(db, trade_id, "user's reason", 0)
    evidence = tuple(row[0] for row in db.execute("SELECT raw_json FROM source_events ORDER BY event_id"))
    updated = assume_saved_cfm_commission_usd(legacy)
    assert upsert_packet(db, updated) == trade_id
    saved = get_trade(db, trade_id)
    assert saved["fee_usd"] == "0.5" and saved["fee_currency_assumed"] == 1
    assert saved["reason"] == "user's reason" and saved["reason_revision"] == 1
    assert saved["gross_pnl_usd"] is None and saved["net_pnl_usd"] is None
    assert evidence == tuple(row[0] for row in db.execute("SELECT raw_json FROM source_events ORDER BY event_id"))


@pytest.mark.parametrize("currency_field", ["fee_currency", "commission_currency"])
def test_explicit_non_usd_commission_currency_is_not_assumed_usd(currency_field):
    entry = row("entry", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM")
    exit = row("exit", "SELL", "2026-01-02T00:00:00Z", product="BTC-CFM")
    entry[currency_field] = "BTC"
    exit[currency_field] = "BTC"
    packet, = CoinbaseCFMDatedFutureSource("acct", spec("FUTURE", "EXPIRING"), [entry, exit]).iter_closes()
    assert packet.fee_usd is None and packet.fee_currency_assumed is False


@pytest.mark.parametrize("change,reason", [(dict(size_in_quote="unknown"), "quote"), (dict(trade_type="ADJUSTMENT"), "adjustment"), (dict(price=None), "missing")])
def test_unsafe_fill_fails_closed(change, reason):
    fills = [row("b", "BUY", "2026-01-01T00:00:00Z", **change), row("s", "SELL", "2026-01-02T00:00:00Z")]
    source = CoinbaseSpotSource("acct", spec(), fills)
    assert not list(source.iter_closes())
    assert any(reason in r.lower() for r in source.unresolved)


def test_account_product_duplicate_and_source_key_mismatch_fail_closed():
    for source in (CoinbaseSpotSource("other", spec(), [row("a", "BUY", "2026-01-01T00:00:00Z")]), CoinbaseSpotSource("acct", replace(spec(), product_id="OTHER"), [row("p", "BUY", "2026-01-01T00:00:00Z")]),
                   CoinbaseSpotSource("acct", spec(), [row("x", "BUY", "2026-01-01T00:00:00Z"), row("x", "SELL", "2026-01-02T00:00:00Z")])):
        assert not list(source.iter_closes())
        assert source.unresolved
    with pytest.raises(ValueError, match="source key mismatch"):
        CoinbaseSpotSource("acct", spec(), [row("b", "BUY", "2026-01-01T00:00:00Z"), row("s", "SELL", "2026-01-02T00:00:00Z")], source_key="wrong")


def test_spot_sell_allocates_oldest_buys_first():
    buys = [row("b1", "BUY", "2026-01-01T00:00:00Z", size="1", price="10"),
            row("b2", "BUY", "2026-01-02T00:00:00Z", size="2", price="20")]
    sells = [row("s1", "SELL", "2026-01-03T00:00:00Z", size="1.5", price="30")]

    source = CoinbaseSpotSource("acct", spec(), buys + sells)
    closes = list(source.iter_closes())

    assert [(p.closed_quantity, p.entry_price, p.exit_price) for p in closes] == [
        (Decimal("1"), Decimal("10"), Decimal("30")),
        (Decimal("0.5"), Decimal("20"), Decimal("30")),
    ]
    assert not source.unresolved


def test_fifo_split_packets_sync_as_distinct_rows_with_shared_exit_event(tmp_path):
    db = connect(tmp_path / "fifo.sqlite3")
    initialize(db)
    fills = [row("b1", "BUY", "2026-01-01T00:00:00Z", size="1", price="10"),
             row("b2", "BUY", "2026-01-02T00:00:00Z", size="2", price="20"),
             row("s1", "SELL", "2026-01-03T00:00:00Z", size="1.5", price="30")]

    assert sync(db, CoinbaseSpotSource("acct", spec(), fills)) == 2
    rows = db.execute("SELECT closed_quantity, entry_price FROM trades ORDER BY entry_time").fetchall()
    assert [(Decimal(r[0]), Decimal(r[1])) for r in rows] == [
        (Decimal("1"), Decimal("10")), (Decimal("0.5"), Decimal("20"))]


def test_sync_replay_preserves_reason_in_scratch_database(tmp_path):
    db = connect(tmp_path / "replay.sqlite3")
    initialize(db)
    source = CoinbaseSpotSource("acct", spec(), [row("b", "BUY", "2026-01-01T00:00:00Z"), row("s", "SELL", "2026-01-02T00:00:00Z")])
    assert sync(db, source) == 1
    trade_id = db.execute("SELECT id FROM trades").fetchone()[0]
    update_reason(db, trade_id, "preserve this", 0)
    assert sync(db, source) == 1
    assert get_trade(db, trade_id)["reason"] == "preserve this"
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 1

def test_quote_sized_spot_fill_uses_source_price_to_derive_base_quantity():
    buy = row("q", "BUY", "2026-01-01T00:00:00Z", size="20", price="10", size_in_quote=True)
    sell = row("s", "SELL", "2026-01-02T00:00:00Z", size="1", price="12")
    source = CoinbaseSpotSource("acct", spec(), [buy, sell])
    packets = list(source.iter_closes())
    assert source.unresolved == ()
    assert len(packets) == 1
    assert packets[0].closed_quantity == Decimal("1")
    assert packets[0].entry_price == Decimal("10")
    assert packets[0].source_events[0].payload["size"] == "20"
    assert packets[0].source_events[0].payload["base_size"] == "2"

def test_quote_sized_derivative_fill_remains_unavailable_without_contract_unit_rule():
    source = CoinbaseCFMDatedFutureSource("acct", spec("FUTURE", "EXPIRING"), [
        row("b", "BUY", "2026-01-01T00:00:00Z", product="BTC-CFM", size_in_quote=True)])
    assert list(source.iter_closes()) == []
    assert any("derivative quote-sized" in reason for reason in source.unresolved)

def test_nonterminating_quote_to_base_conversion_is_unresolved():
    buy = row("q", "BUY", "2026-01-01T00:00:00Z", size="1", price="3", size_in_quote=True)
    source = CoinbaseSpotSource("acct", spec(), [buy])
    assert list(source.iter_closes()) == []
    assert any("non-exact quote-sized" in reason for reason in source.unresolved)

def test_large_exact_quote_to_base_conversion_refuses_precision_loss():
    size = "123456789012345678901234567890.123456789"
    buy = row("q", "BUY", "2026-01-01T00:00:00Z", size=size, price="1", size_in_quote=True)
    sell = row("s", "SELL", "2026-01-02T00:00:00Z", size="1", price="2")
    source = CoinbaseSpotSource("acct", spec(), [buy, sell])
    assert list(source.iter_closes()) == []
    assert any("non-exact quote-sized" in reason for reason in source.unresolved)
