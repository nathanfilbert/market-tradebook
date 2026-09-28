from decimal import Decimal

from tradebook.coinbase_products import classify_product
from tradebook.coinbase_spot_inventory import build_spot_fifo


def spec(quote):
    return classify_product({"product_id": f"BTC-{quote}", "product_type": "SPOT",
                             "base_currency_id": "BTC", "quote_currency_id": quote},
                            account_family="SPOT", source_reference="fixture")


def fill(event_id, side, qty, time, quote="USD", *, size_in_quote=False, base_size=None):
    result = {"entry_id": event_id, "order_id": f"order-{event_id}",
              "trade_type": "FILL", "side": side, "size": qty, "price": "10",
              "trade_time": time, "sequence_timestamp": time, "product_id": f"BTC-{quote}",
              "retail_portfolio_id": "portfolio", "size_in_quote": size_in_quote}
    if base_size is not None:
        result["base_size"] = base_size
    return result


def wallet(event_id, kind, qty, time, *, order_id=None, product_id=None):
    result = {"id": event_id, "type": kind, "status": "completed",
              "created_at": time, "amount": {"amount": qty, "currency": "BTC"}}
    if order_id is not None:
        result["advanced_trade_fill"] = {"order_id": order_id, "product_id": product_id,
                                          "fill_price": "10"}
    return result


def assemble(events, fills, **overrides):
    balance = sum((Decimal(x["amount"]["amount"]) for x in events), Decimal(0))
    return build_spot_fifo(events, fills, asset="BTC", account_id="portfolio",
                           wallet_account_id="wallet-btc", wallet_balance=balance,
                           quantity_increment=Decimal("0.00000001"),
                           product_specs={"BTC-USD": spec("USD"), "BTC-USDC": spec("USDC")},
                           **overrides)


def test_transfer_in_sources_unknown_basis_close_without_duplicate_fill():
    events = [wallet("deposit", "pro_deposit", "1", "2025-01-01T00:00:00Z"),
              wallet("tx-sell", "advanced_trade_fill", "-0.4", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    result = assemble(events, [fill("sell", "SELL", "0.4", "2025-01-02T00:00:00Z")])
    assert result.unresolved == ()
    packet, = result.packets
    assert packet.closed_quantity == Decimal("0.4") and packet.entry_price is None
    assert packet.position_side == "long" and packet.contract_multiplier == Decimal(1)
    assert packet.reported_gross_usd is None and packet.reported_net_usd is None
    assert [e.event_id for e in packet.source_events] == [
        "wallet:wallet-btc:deposit", "fill:sell", "wallet:wallet-btc:tx-sell"]
    assert assemble(list(reversed(events)), [fill("sell", "SELL", "0.4", "2025-01-02T00:00:00Z")]).packets[0].close_id == packet.close_id


def test_cross_quote_buy_consumed_by_usd_sale_has_unknown_usd_basis():
    events = [wallet("tx-buy", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USDC"),
              wallet("tx-sell", "advanced_trade_fill", "-0.5", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    fills = [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z", "USDC"),
             fill("sell", "SELL", "0.5", "2025-01-02T00:00:00Z")]
    packet, = assemble(events, fills).packets
    assert packet.entry_price is None and packet.market == "BTC-USD"
    assert packet.source_events[0].event_id == "fill:buy"
    assert packet.basis_status == "cross_currency_unavailable"
    assert packet.basis_currency == "USDC"


def test_shared_entry_event_remains_immutable_across_different_exit_quotes(tmp_path):
    from tradebook.store import connect, initialize, upsert_packet
    events = [wallet("t-buy", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USDC"),
              wallet("t-usd", "advanced_trade_fill", "-0.5", "2025-01-02T00:00:00Z",
                     order_id="order-usd", product_id="BTC-USD"),
              wallet("t-usdc", "advanced_trade_fill", "-0.5", "2025-01-03T00:00:00Z",
                     order_id="order-usdc", product_id="BTC-USDC")]
    fills = [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z", "USDC"),
             fill("usd", "SELL", "0.5", "2025-01-02T00:00:00Z"),
             fill("usdc", "SELL", "0.5", "2025-01-03T00:00:00Z", "USDC")]
    result = assemble(events, fills)
    assert result.unresolved == () and len(result.packets) == 2
    db = connect(tmp_path / "cross.sqlite3"); initialize(db)
    for packet in result.packets:
        upsert_packet(db, packet)
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
    assert {row[0] for row in db.execute("SELECT basis_status FROM trades")} == {
        "cross_currency_unavailable", "known_quote_basis"}


def test_partial_buy_used_by_two_sells_replays_without_source_drift(tmp_path):
    from tradebook.store import connect, initialize, upsert_packet
    events = [wallet("t-buy", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD"),
              wallet("t-sell-a", "advanced_trade_fill", "-0.5", "2025-01-02T00:00:00Z",
                     order_id="order-sell-a", product_id="BTC-USD"),
              wallet("t-sell-b", "advanced_trade_fill", "-0.5", "2025-01-03T00:00:00Z",
                     order_id="order-sell-b", product_id="BTC-USD")]
    fills = [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z"),
             fill("sell-a", "SELL", "0.5", "2025-01-02T00:00:00Z"),
             fill("sell-b", "SELL", "0.5", "2025-01-03T00:00:00Z")]
    result = assemble(events, fills)
    assert result.unresolved == () and len(result.packets) == 2
    db = connect(tmp_path / "partial.sqlite3"); initialize(db)
    for packet in result.packets:
        upsert_packet(db, packet)
    for packet in result.packets:
        upsert_packet(db, packet)
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 2


def test_withdrawal_consumes_fifo_without_emitting_trade_row():
    events = [wallet("deposit", "pro_deposit", "1", "2025-01-01T00:00:00Z"),
              wallet("withdrawal", "pro_withdrawal", "-0.6", "2025-01-02T00:00:00Z"),
              wallet("tx-sell", "advanced_trade_fill", "-0.4", "2025-01-03T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    result = assemble(events, [fill("sell", "SELL", "0.4", "2025-01-03T00:00:00Z")])
    assert result.unresolved == () and len(result.packets) == 1
    assert result.packets[0].closed_quantity == Decimal("0.4")


def test_wallet_signed_amount_controls_direction_not_legacy_type_name():
    events = [wallet("incoming", "pro_withdrawal", "1", "2025-01-01T00:00:00Z"),
              wallet("outgoing", "pro_deposit", "-0.6", "2025-01-02T00:00:00Z"),
              wallet("tx-sell", "advanced_trade_fill", "-0.4", "2025-01-03T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    result = assemble(events, [fill("sell", "SELL", "0.4", "2025-01-03T00:00:00Z")])
    assert result.unresolved == ()
    assert len(result.packets) == 1
    assert result.packets[0].source_events[0].event_id == "wallet:wallet-btc:incoming"


def test_mismatched_wallet_fill_quantity_or_balance_fails_closed():
    events = [wallet("tx-buy", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD")]
    assert assemble(events, [fill("buy", "BUY", "2", "2025-01-01T00:00:00Z")]).packets == ()
    result = build_spot_fifo(events, [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z")],
                             asset="BTC", account_id="portfolio", wallet_account_id="wallet-btc",
                             wallet_balance=Decimal("2"), quantity_increment=Decimal("0.00000001"),
                             product_specs={"BTC-USD": spec("USD")})
    assert result.packets == () and result.unresolved


def test_quote_sized_fill_without_exact_base_size_and_duplicate_wallet_id_fail_closed():
    events = [wallet("tx-buy", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD")]
    assert assemble(events, [fill("buy", "BUY", "10", "2025-01-01T00:00:00Z", size_in_quote=True)]).packets == ()
    assert assemble(events + events, [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z")]).packets == ()


def test_nonterminating_quote_fill_can_use_unique_source_wallet_base_quantity():
    events = [wallet("tx-buy", "advanced_trade_fill", "0.33333333", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD"),
              wallet("tx-sell", "advanced_trade_fill", "-0.33333333", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    events[0]["advanced_trade_fill"]["fill_price"] = "3"
    buy = fill("buy", "BUY", "1", "2025-01-01T00:00:00Z", size_in_quote=True,
               base_size="0.33333333")
    buy["price"] = "3"
    buy["base_size_source_tx"] = "tx-buy"
    sell = fill("sell", "SELL", "0.33333333", "2025-01-02T00:00:00Z")
    result = assemble(events, [buy, sell])
    assert result.unresolved == ()
    packet, = result.packets
    assert packet.closed_quantity == Decimal("0.33333333")
    assert "wallet:wallet-btc:tx-buy" in [event.event_id for event in packet.source_events]


def test_nonterminating_quote_fill_auto_derives_source_wallet_base_quantity():
    events = [wallet("tx-buy", "advanced_trade_fill", "0.33333333", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD"),
              wallet("tx-sell", "advanced_trade_fill", "-0.33333333", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    events[0]["advanced_trade_fill"]["fill_price"] = "3"
    buy = fill("buy", "BUY", "1", "2025-01-01T00:00:00Z", size_in_quote=True)
    buy["price"] = "3"
    result = assemble(events, [buy, fill("sell", "SELL", "0.33333333", "2025-01-02T00:00:00Z")])
    assert result.unresolved == ()
    packet, = result.packets
    assert packet.closed_quantity == Decimal("0.33333333")
    assert "wallet:wallet-btc:tx-buy" in [event.event_id for event in packet.source_events]


def test_quote_wallet_evidence_must_match_the_order_and_be_unique():
    events = [wallet("tx1", "advanced_trade_fill", "0.33333333", "2025-01-01T00:00:00Z",
                     order_id="order-buy", product_id="BTC-USD"),
              wallet("tx2", "advanced_trade_fill", "0.33333333", "2025-01-01T00:00:01Z",
                     order_id="order-buy", product_id="BTC-USD")]
    for tx in events:
        tx["advanced_trade_fill"]["fill_price"] = "3"
    buys = [fill("buy", "BUY", "1", "2025-01-01T00:00:00Z", size_in_quote=True, base_size="0.33333333"),
            fill("buy2", "BUY", "1", "2025-01-01T00:00:01Z", size_in_quote=True, base_size="0.33333333")]
    for buy in buys:
        buy["price"] = "3"
        buy["order_id"] = "order-buy"
    buys[0]["base_size_source_tx"] = "tx1"
    buys[1]["base_size_source_tx"] = "tx2"
    result = assemble(events, buys)
    assert result.packets == () and any("ambiguous quote-sized" in r for r in result.unresolved)


def test_same_execution_time_uses_distinct_source_posting_times_within_one_order():
    time = "2025-01-01T00:00:00Z"
    events = [wallet("tx1", "advanced_trade_fill", "1", time,
                     order_id="order-pair", product_id="BTC-USD"),
              wallet("tx2", "advanced_trade_fill", "1", time,
                     order_id="order-pair", product_id="BTC-USD"),
              wallet("tx-sell", "advanced_trade_fill", "-1", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    earlier = fill("early", "BUY", "1", time)
    later = fill("late", "BUY", "1", time)
    earlier.update(order_id="order-pair", sequence_timestamp="2025-01-01T00:00:01Z", price="9")
    later.update(order_id="order-pair", sequence_timestamp="2025-01-01T00:00:02Z", price="11")
    events[0]["advanced_trade_fill"]["fill_price"] = "9"
    events[1]["advanced_trade_fill"]["fill_price"] = "11"
    result = assemble(events, [later, earlier, fill("sell", "SELL", "1", "2025-01-02T00:00:00Z")])
    assert result.unresolved == ()
    packet, = result.packets
    assert packet.entry_price == Decimal("9")


def test_order_aggregate_match_cannot_hide_swapped_per_fill_quantities():
    events = [wallet("tx-small", "advanced_trade_fill", "1", "2025-01-01T00:00:00Z",
                     order_id="order-pair", product_id="BTC-USD"),
              wallet("tx-large", "advanced_trade_fill", "2", "2025-01-01T00:00:01Z",
                     order_id="order-pair", product_id="BTC-USD"),
              wallet("tx-sell", "advanced_trade_fill", "-1", "2025-01-02T00:00:00Z",
                     order_id="order-sell", product_id="BTC-USD")]
    events[0]["advanced_trade_fill"]["fill_price"] = "10"
    events[1]["advanced_trade_fill"]["fill_price"] = "11"
    small = fill("small", "BUY", "2", "2025-01-01T00:00:00Z")
    large = fill("large", "BUY", "1", "2025-01-01T00:00:01Z")
    small.update(order_id="order-pair", price="10")
    large.update(order_id="order-pair", price="11")
    result = assemble(events, [small, large, fill("sell", "SELL", "1", "2025-01-02T00:00:00Z")])
    assert result.packets == ()
    assert any("per-fill" in reason for reason in result.unresolved)