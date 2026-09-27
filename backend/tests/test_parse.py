# backend/tests/test_parse.py
from decimal import Decimal
from tradebook.parse import project
from test_domain import packet
from tradebook.domain import ClosedTradePacket


def row(**changes):
    return project(ClosedTradePacket.model_validate(packet(**changes)))


def test_long_usd_close_and_notional():
    r = row()
    assert (r["position_notional_usd"], r["gross_pnl_usd"], r["net_pnl_usd"]) == (
        "6000.00", "200.00", "188.00")


def test_short_contract_multiplier_and_funding():
    r = row(product_type="perpetual", position_side="short", closed_quantity="2",
        quantity_unit="contracts", entry_price="70000", exit_price="68000",
        contract_multiplier="0.01", fee_usd="5", funding_usd="-2")
    assert (r["position_notional_usd"], r["gross_pnl_usd"], r["net_pnl_usd"]) == (
        "1400.00", "40.00", "33.00")


def test_missing_cost_or_usd_conversion_stays_null():
    assert row(funding_usd=None)["net_pnl_usd"] is None
    r = row(price_currency="EUR")
    assert r["position_notional_usd"] is None
    assert r["gross_pnl_usd"] is None
    assert r["net_pnl_usd"] is None


def test_missing_source_prices_or_multiplier_are_preserved_as_unknown():
    r = row(entry_time=None, entry_price=None, contract_multiplier=None)
    assert r["entry_time"] is None
    assert r["position_notional_usd"] is None
    assert r["gross_pnl_usd"] is None
    assert r["net_pnl_usd"] is None


def test_reported_amounts_are_not_overwritten():
    r = row(reported_gross_usd="201", reported_net_usd="187")
    assert r["reported_gross_usd"] == "201"
    assert r["reported_net_usd"] == "187"
    assert r["net_pnl_usd"] == "188.00"
    assert r["reconciliation_status"] == "mismatch"


def test_reconciliation_distinguishes_match_absence_and_unknown():
    assert row(reported_gross_usd="200.00", reported_net_usd="188.00")["reconciliation_status"] == "match"
    assert row()["reconciliation_status"] == "not_available"
    assert row(price_currency="EUR", reported_net_usd="188")["reconciliation_status"] == "not_comparable"


def test_spot_short_does_not_claim_supported_calculated_pnl():
    r = row(position_side="short")
    assert r["gross_pnl_usd"] is None
    assert r["net_pnl_usd"] is None
    assert r["position_notional_usd"] is None
