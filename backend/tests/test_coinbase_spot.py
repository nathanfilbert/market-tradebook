from decimal import Decimal
from datetime import datetime, timezone

import pytest
from tradebook.coinbase_closes import assemble_closes
from tradebook.coinbase_products import classify_product

SPEC = classify_product({"product_id":"BTC-USD", "product_type":"SPOT", "base_currency_id":"BTC", "quote_currency_id":"USD"}, account_family="SPOT", source_reference="fixture:product")

def fill(id, side, qty, price, time, fee="0"):
    return {"entry_id": id, "trade_type":"FILL", "side":side, "size":qty, "price":price, "fee":fee, "fee_currency":"USD", "trade_time":time, "product_id":"BTC-USD"}

def test_spot_long_partial_close_preserves_sources_and_known_usd_fee():
    result = assemble_closes([
        fill("buy-1", "BUY", "2", "10", "2026-01-01T00:00:00Z"),
        fill("sell-1", "SELL", "0.5", "12", "2026-01-02T00:00:00Z", "0.1"),
    ], product=SPEC, source_key="coinbase", account_id="a")
    assert len(result.closes) == 1 and result.unresolved == ()
    p = result.closes[0]
    assert p.closed_quantity == Decimal("0.5") and p.position_side == "long"
    assert p.fee_usd == Decimal("0.1") and p.funding_usd is None
    assert [e.event_id for e in p.source_events] == ["buy-1", "sell-1"]
    assert p.close_id

def test_spot_short_position_is_not_emitted():
    result = assemble_closes([fill("s1", "SELL", "1", "10", "2026-01-01T00:00:00Z")], product=SPEC, source_key="coinbase", account_id="a")
    assert not result.closes and result.unresolved

def test_missing_entry_fee_is_not_reported_as_partial_known_cost():
    buy = fill("b", "BUY", "1", "10", "2026-01-01T00:00:00Z")
    buy.pop("fee")
    result = assemble_closes([buy, fill("s", "SELL", "1", "12", "2026-01-02T00:00:00Z")],
        product=SPEC, source_key="coinbase", account_id="a")
    assert len(result.closes) == 1
    assert result.closes[0].fee_usd is None
    assert result.closes[0].contract_multiplier == Decimal("1")

def test_repeated_partial_closes_allocate_entry_fee_without_drift():
    result = assemble_closes([
        fill("buy", "BUY", "2", "10", "2026-01-01T00:00:00Z", "0.2"),
        fill("sell1", "SELL", "0.5", "12", "2026-01-02T00:00:00Z", "0.1"),
        fill("sell2", "SELL", "0.5", "13", "2026-01-03T00:00:00Z", "0.1"),
    ], product=SPEC, source_key="coinbase", account_id="a")
    assert [p.fee_usd for p in result.closes] == [Decimal("0.15"), Decimal("0.15")]

def test_source_events_preserve_fill_payload_and_unknown_fee_stays_unknown():
    entry = fill("buy", "BUY", "1", "10", "2026-01-01T00:00:00Z", None)
    exit_fill = fill("sell", "SELL", "1", "12", "2026-01-02T00:00:00Z", None)
    result = assemble_closes([entry, exit_fill], product=SPEC, source_key="coinbase", account_id="a")
    assert result.closes[0].fee_usd is None
    assert result.closes[0].source_events[0].payload == entry
    assert result.closes[0].source_events[1].payload == exit_fill

@pytest.mark.parametrize("field,value", [("entry_id", ""), ("size", "bad"), ("trade_time", "not-time")])
def test_invalid_source_fields_are_quarantined(field, value):
    bad = fill("x", "BUY", "1", "10", "2026-01-01T00:00:00Z")
    bad[field] = value
    result = assemble_closes([bad], product=SPEC, source_key="coinbase", account_id="a")
    assert not result.closes and result.unresolved

def test_adjustment_is_quarantined():
    adjustment = fill("a", "BUY", "1", "10", "2026-01-01T00:00:00Z")
    adjustment["trade_type"] = "ADJUSTMENT"
    result = assemble_closes([adjustment], product=SPEC, source_key="coinbase", account_id="a")
    assert not result.closes and "adjustment" in result.unresolved[0]

def test_multiple_open_lots_are_fifo_matched():
    result = assemble_closes([
        fill("b1", "BUY", "1", "10", "2026-01-01T00:00:00Z"),
        fill("b2", "BUY", "1", "11", "2026-01-01T00:01:00Z"),
        fill("s", "SELL", "1", "12", "2026-01-02T00:00:00Z"),
    ], product=SPEC, source_key="coinbase", account_id="a")
    assert len(result.closes) == 1 and result.closes[0].entry_price == Decimal("10")
    assert not result.unresolved

def test_fifo_orders_by_trade_time_and_splits_one_sale_across_buy_lots():
    fills = [
        fill("later", "BUY", "1", "11", "2026-01-02T00:00:00Z", "0.4"),
        fill("older", "BUY", "1", "10", "2026-01-01T00:00:00Z", "0.2"),
        fill("exit", "SELL", "1.5", "12", "2026-01-03T00:00:00Z", "0.3"),
    ]
    result = assemble_closes(fills, product=SPEC, source_key="coinbase.spot", account_id="a")
    assert result.unresolved == ()
    assert [(p.source_events[0].event_id, p.closed_quantity, p.entry_price, p.fee_usd)
            for p in result.closes] == [
        ("older", Decimal("1"), Decimal("10"), Decimal("0.4")),
        ("later", Decimal("0.5"), Decimal("11"), Decimal("0.3")),
    ]
    assert result.closes[0].close_id != result.closes[1].close_id
    assert [p.close_id for p in result.closes] == [p.close_id for p in assemble_closes(
        list(reversed(fills)), product=SPEC, source_key="coinbase.spot", account_id="a").closes]
