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
