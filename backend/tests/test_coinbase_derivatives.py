from decimal import Decimal
from dataclasses import replace
import pytest
from tradebook.coinbase_closes import assemble_closes
from tradebook.coinbase_products import classify_product


def future(expiry):
    return classify_product({"product_id":"BTC-CFM", "product_type":"FUTURE", "quote_currency_id":"USD", "future_product_details":{"contract_expiry_type":expiry, "risk_managed_by":"MANAGED_BY_FCM"}}, account_family="CFM", source_reference="fixture:product")

def fill(id, side, qty, price, time):
    return {"entry_id":id, "trade_type":"FILL", "side":side, "size":qty, "price":price, "fee":"0", "fee_currency":"USD", "trade_time":time, "product_id":"BTC-CFM"}

@pytest.mark.parametrize("expiry,kind", [("PERPETUAL","perpetual"),("EXPIRING","dated_future")])
def test_cfm_reduction_and_flip_do_not_invent_contract_economics(expiry, kind):
    result = assemble_closes([
        fill("open", "BUY", "2", "100", "2026-01-01T00:00:00Z"),
        fill("reverse", "SELL", "3", "110", "2026-01-02T00:00:00Z"),
    ], product=future(expiry), source_key="coinbase", account_id="cfm")
    assert len(result.closes) == 1
    p=result.closes[0]
    assert p.product_type == kind and p.closed_quantity == Decimal("2")
    assert p.contract_multiplier is None and p.funding_usd is None and p.fee_usd == Decimal("0")
    assert p.source_events[0].event_id == "open" and p.source_events[1].event_id == "reverse"
    assert result.unresolved

def test_unknown_units_fail_closed():
    spec = replace(future("PERPETUAL"), quantity_unit="")
    result=assemble_closes([fill("x", "BUY", "1", "10", "2026-01-01T00:00:00Z")], product=spec, source_key="coinbase", account_id="a")
    assert not result.closes and result.unresolved

def test_unknown_fee_currency_is_not_reported_as_usd():
    entry = fill("open", "BUY", "1", "10", "2026-01-01T00:00:00Z")
    close = fill("close", "SELL", "1", "11", "2026-01-02T00:00:00Z")
    entry.update(fee="0.2", fee_currency="BTC")
    close.update(fee="0.3", fee_currency="BTC")
    result = assemble_closes([entry, close], product=future("PERPETUAL"), source_key="coinbase", account_id="cfm")
    assert result.closes[0].fee_usd is None

def test_short_entry_reduction_is_supported():
    result = assemble_closes([
        fill("open-short", "SELL", "2", "100", "2026-01-01T00:00:00Z"),
        fill("buy-back", "BUY", "1", "90", "2026-01-02T00:00:00Z"),
    ], product=future("PERPETUAL"), source_key="coinbase", account_id="cfm")
    assert len(result.closes) == 1
    assert result.closes[0].position_side == "short"
    assert result.closes[0].closed_quantity == Decimal("1")


def test_multiple_cfm_entry_fills_emit_close_with_unknown_entry_basis():
    result = assemble_closes([
        fill("entry-one", "BUY", "1", "100", "2026-01-01T00:00:00Z"),
        fill("entry-two", "BUY", "2", "101", "2026-01-01T01:00:00Z"),
        fill("close-one", "SELL", "1", "102", "2026-01-02T00:00:00Z"),
        fill("close-two", "SELL", "2", "103", "2026-01-03T00:00:00Z"),
    ], product=future("EXPIRING"), source_key="coinbase.cfm.dated_future", account_id="cfm")
    assert result.unresolved == ()
    assert [p.closed_quantity for p in result.closes] == [Decimal("1"), Decimal("2")]
    assert all(p.entry_price is None and p.entry_time is None and p.fee_usd is None for p in result.closes)
    assert all(p.position_side == "long" and p.contract_multiplier is None for p in result.closes)
    assert [e.event_id for e in result.closes[0].source_events] == ["entry-one", "entry-two", "close-one"]


def test_multi_entry_cfm_close_replays_with_immutable_source_events(tmp_path):
    from tradebook.store import connect, initialize, upsert_packet
    result = assemble_closes([
        fill("entry-one", "SELL", "1", "100", "2026-01-01T00:00:00Z"),
        fill("entry-two", "SELL", "1", "101", "2026-01-01T01:00:00Z"),
        fill("exit-one", "BUY", "1", "99", "2026-01-02T00:00:00Z"),
        fill("exit-two", "BUY", "1", "98", "2026-01-03T00:00:00Z"),
    ], product=future("EXPIRING"), source_key="coinbase.cfm.dated_future", account_id="cfm")
    assert result.unresolved == () and len(result.closes) == 2
    assert all(p.position_side == "short" and p.entry_price is None for p in result.closes)
    db = connect(tmp_path / "derivative.sqlite3"); initialize(db)
    for packet in (*result.closes, *result.closes):
        upsert_packet(db, packet)
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
    assert all(row[0] is None for row in db.execute("SELECT gross_pnl_usd FROM trades"))


def test_duplicate_derivative_fill_id_refuses_all_close_packets():
    result = assemble_closes([
        fill("a", "BUY", "1", "100", "2026-01-01T00:00:00Z"),
        fill("b", "BUY", "1", "101", "2026-01-01T01:00:00Z"),
        fill("x", "SELL", "1", "102", "2026-01-02T00:00:00Z"),
        fill("x", "SELL", "1", "103", "2026-01-03T00:00:00Z"),
    ], product=future("EXPIRING"), source_key="coinbase.cfm.dated_future", account_id="cfm")
    assert result.closes == ()
    assert any("duplicate" in reason for reason in result.unresolved)


def test_multi_entry_reversal_does_not_drop_the_new_side():
    result = assemble_closes([
        fill("a", "BUY", "1", "100", "2026-01-01T00:00:00Z"),
        fill("b", "BUY", "1", "101", "2026-01-01T01:00:00Z"),
        fill("reverse", "SELL", "3", "102", "2026-01-02T00:00:00Z"),
    ], product=future("EXPIRING"), source_key="coinbase.cfm.dated_future", account_id="cfm")
    assert result.unresolved and result.closes == ()
