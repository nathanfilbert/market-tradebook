"""Order P/L for closes that share one entry fill (AVAX short 4, covered 2 + 2)."""
import copy
from decimal import Decimal
import json
import sqlite3

import pytest

from tradebook.coinbase_order_pnl import (calculate_execution, check_entry_allocation,
                                          collect_pending_executions, collect_saved_executions,
                                          persist_execution)
from tradebook.coinbase_order_sync import main as order_sync_main
from tradebook.coinbase_products import classify_product
from tradebook.coinbase_source import CoinbaseCFMDatedFutureSource
from tradebook.store import connect, get_trade, initialize, update_notes, update_reason, upsert_packet

MARKET = "AVP-20DEC30-CDE"
ACCOUNT = "portfolio"
ENTRY_FILL = "5605ef579b6cbe62d75f22a1d5671d0f56ba245b31d220caccafd7ab85b2a64d"
EXIT_FILLS = ("fb3a45f58325d7221f824641d021a1dcc772d72a0646ddacaca788cd7855dbc7",
              "52920f3a8624d9904662afaa11b59d9912346b7b21829482f838c6a7bcb55c9b")


def fill(entry_id, order_id, when, side, size, price, commission):
    return {"entry_id": entry_id, "trade_id": entry_id, "order_id": order_id, "trade_time": when,
            "sequence_timestamp": when, "trade_type": "FILL", "side": side, "size": size, "price": price,
            "commission": commission, "commission_currency": None, "size_in_quote": False,
            "product_id": MARKET, "position_effect": None, "retail_portfolio_id": ACCOUNT}


def order(order_id, side, size, price, value, fee, after):
    return {"order_id": order_id, "product_id": MARKET, "retail_portfolio_id": ACCOUNT, "product_type": "FUTURE",
            "side": side, "status": "FILLED", "number_of_fills": "1", "filled_size": size,
            "average_filled_price": price, "filled_value": value, "total_fees": fee,
            "total_value_after_fees": after, "completion_percentage": "100.00"}


AVAX_FILLS = [
    fill(ENTRY_FILL, "92abc475-cc7b-4327-b171-7a03b7054736", "2026-10-12T17:18:07.632929Z", "SELL", "4", "10.47", "0.8588"),
    fill(EXIT_FILLS[0], "5de0557c-f0cf-4ed2-b83e-8999b1c87c96", "2026-10-12T21:54:52.410708Z", "BUY", "2", "10.37", "0.4274"),
    fill(EXIT_FILLS[1], "eb6639ba-6225-4bd4-8f6f-1c86233ae85c", "2026-10-12T21:57:30.049661Z", "BUY", "2", "10.32", "0.4264"),
]
AVAX_ORDERS = [
    order("92abc475-cc7b-4327-b171-7a03b7054736", "SELL", "4", "10.47", "418.8", "0.8588", "417.9412"),
    order("5de0557c-f0cf-4ed2-b83e-8999b1c87c96", "BUY", "2", "10.37", "207.4", "0.4274", "207.8274"),
    order("eb6639ba-6225-4bd4-8f6f-1c86233ae85c", "BUY", "2", "10.32", "206.4", "0.4264", "206.8264"),
]


def packets(fills=None):
    spec = classify_product({"product_id": MARKET, "product_type": "FUTURE", "quote_currency_id": "USD",
                             "future_product_details": {"contract_expiry_type": "EXPIRING",
                                                        "risk_managed_by": "MANAGED_BY_FCM"}},
                            account_family="CFM", source_reference="fixture")
    source = CoinbaseCFMDatedFutureSource(ACCOUNT, spec, copy.deepcopy(fills or AVAX_FILLS))
    closes = list(source.iter_closes())
    assert not source.unresolved
    return closes


def orders_for(packet, orders=AVAX_ORDERS):
    by_id = {o["order_id"]: o for o in orders}
    return [copy.deepcopy(by_id[e.payload["order_id"]]) for e in packet.source_events]


class HTTP:
    def __init__(self, orders=AVAX_ORDERS):
        self.orders, self.calls = orders, []

    def get(self, route):
        self.calls.append(route)
        return {"order": copy.deepcopy(next(o for o in self.orders if route.endswith("/" + o["order_id"])))}


def book(tmp_path, closes):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    ids = [upsert_packet(db, p) for p in closes]
    return path, db, ids


def test_short_four_covered_two_and_two_gets_exact_share_of_entry():
    first, second = packets()
    assert (first.closed_quantity, second.closed_quantity) == (2, 2)
    assert (first.fee_usd, second.fee_usd) == (Decimal("0.8568"), Decimal("0.8558"))
    one = calculate_execution(first, orders_for(first))
    two = calculate_execution(second, orders_for(second))
    assert (one.entry_notional_usd, one.gross_usd, one.net_after_fees_usd) == (Decimal("209.4"), Decimal("2.0"), Decimal("1.1432"))
    assert (two.entry_notional_usd, two.gross_usd, two.net_after_fees_usd) == (Decimal("209.4"), Decimal("3.0"), Decimal("2.1442"))
    assert one.gross_usd + two.gross_usd == 5
    assert first.fee_usd + second.fee_usd == Decimal("1.7126")
    assert one.net_after_fees_usd + two.net_after_fees_usd == Decimal("3.2874")
    assert one.entry_notional_usd + two.entry_notional_usd == Decimal("418.8")


def test_long_equivalent_changes_sign():
    fills = copy.deepcopy(AVAX_FILLS)
    orders = copy.deepcopy(AVAX_ORDERS)
    for f, o in zip(fills, orders):
        side = "BUY" if f["side"] == "SELL" else "SELL"
        f["side"] = o["side"] = side
    orders[0]["total_value_after_fees"] = "419.6588"
    orders[1]["total_value_after_fees"] = "206.9726"
    orders[2]["total_value_after_fees"] = "205.9736"
    first, second = packets(fills)
    assert first.position_side == "long"
    one = calculate_execution(first, orders_for(first, orders))
    two = calculate_execution(second, orders_for(second, orders))
    assert (one.gross_usd, one.net_after_fees_usd) == (Decimal("-2.0"), Decimal("-2.8568"))
    assert (two.gross_usd, two.net_after_fees_usd) == (Decimal("-3.0"), Decimal("-3.8558"))


def test_full_pipeline_preserves_ids_and_notes_and_replay_is_noop(tmp_path):
    closes = packets()
    path, db, ids = book(tmp_path, closes)
    assert len(set(ids)) == 2
    update_reason(db, ids[0], "fade the bounce", 0)
    update_notes(db, ids[1], "covered the rest", 0)
    before = {i: dict(db.execute("SELECT * FROM trades WHERE id=?", (i,)).fetchone()) for i in ids}
    history = db.execute("SELECT * FROM reason_history UNION ALL SELECT * FROM notes_history").fetchall()
    fills_before = [tuple(r) for r in db.execute("SELECT * FROM source_events ORDER BY event_id")]
    db.close()
    args = ["--db", str(path), "--market", MARKET, "--portfolio-id", ACCOUNT, "--pending-only", "--confirm-live-read"]
    assert order_sync_main(args + ["--dry-run"], http=HTTP()) == 0
    with connect(path) as db:
        assert not db.execute("SELECT * FROM execution_economics").fetchall()
    assert order_sync_main(args + ["--confirm-local-import"], http=HTTP()) == 0
    assert len(list(tmp_path.glob("*.backup.sqlite3"))) == 1
    db = connect(path)
    rows = [get_trade(db, i) for i in ids]
    assert [(r["gross_pnl_usd"], r["execution_net_pnl_usd"], r["position_notional_usd"]) for r in rows] == [
        ("2.0", "1.1432", "209.4"), ("3.0", "2.1442", "209.4")]
    assert all(r["net_pnl_usd"] is None and r["funding_usd"] is None for r in rows)
    assert all(r["pnl_method"] == "coinbase_orders_ex_funding" for r in rows)
    for i in ids:
        assert dict(db.execute("SELECT * FROM trades WHERE id=?", (i,)).fetchone()) == before[i]
    assert db.execute("SELECT * FROM reason_history UNION ALL SELECT * FROM notes_history").fetchall() == history
    orders_saved = db.execute("SELECT count(*) FROM source_events WHERE event_id LIKE '[\"orders\"%'").fetchone()[0]
    assert orders_saved == 3  # the shared entry order is stored once
    assert [tuple(r) for r in db.execute(
        "SELECT * FROM source_events WHERE event_id NOT LIKE '[%' ORDER BY event_id")] == fills_before
    assert [get_trade(db, i)["close_id"] for i in ids] == [before[i]["close_id"] for i in ids]
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    db.close()
    # Repeated sync reads nothing and writes nothing.
    stamp = path.stat().st_mtime_ns
    http = HTTP()
    assert order_sync_main(args + ["--confirm-local-import"], http=http) == 0
    assert not http.calls and path.stat().st_mtime_ns == stamp


def test_first_close_alone_while_rest_is_still_open(tmp_path):
    first = packets(AVAX_FILLS[:2])
    assert len(first) == 1
    path, db, ids = book(tmp_path, first)
    collected, skipped, failed, deferred = collect_pending_executions(db, [MARKET], ACCOUNT, HTTP())
    assert (len(collected), skipped, failed, deferred) == (1, 0, 0, 0)
    persist_execution(db, *collected[0][:4])
    assert get_trade(db, ids[0])["execution_net_pnl_usd"] == "1.1432"
    # The later close arrives; the first packet is unchanged so its economics stay.
    both = packets()
    assert both[0] == first[0]
    upsert_packet(db, both[1])
    assert get_trade(db, ids[0])["execution_net_pnl_usd"] == "1.1432"
    collected, *_ = collect_pending_executions(db, [MARKET], ACCOUNT, HTTP())
    assert len(collected) == 1
    persist_execution(db, *collected[0][:4])
    total = sum(Decimal(get_trade(db, i)["execution_net_pnl_usd"]) for i in (ids[0], collected[0][0]))
    assert total == Decimal("3.2874")
    db.close()


def test_strict_manual_selection_supports_partial_entries(tmp_path):
    path, db, ids = book(tmp_path, packets())
    collected = collect_saved_executions(db, [MARKET], HTTP())
    assert sorted(str(c[3].net_after_fees_usd) for c in collected) == ["1.1432", "2.1442"]
    db.close()


def oversubscribed_packet(closes, quantity="3"):
    """A forged sibling claiming more of the entry than remains."""
    return closes[1].model_copy(deep=True, update={"closed_quantity": Decimal(quantity), "close_id": "forged"})


@pytest.mark.parametrize("change", [
    lambda o: o[0].update(filled_size="2"),                  # entry order differs from its fill
    lambda o: o[0].update(number_of_fills="2"),              # multi-fill entry
    lambda o: o[0].update(status="OPEN"),
    lambda o: o[0].update(retail_portfolio_id="other"),
    lambda o: o[0].update(product_id="BIP-20DEC30-CDE"),
    lambda o: o[0].update(side="BUY"),
    lambda o: o[0].update(filled_value="419"),               # value drifts from constant contract size
    lambda o: o[0].update(total_fees="0.8589", total_value_after_fees="417.9411"),
    lambda o: o[1].update(filled_size="4"),                  # split exit
    lambda o: o[1].update(order_id=o[0]["order_id"]),
])
def test_bad_or_incomplete_order_is_refused(change):
    first, _ = packets()
    orders = orders_for(first)
    change(orders)
    with pytest.raises(ValueError):
        calculate_execution(first, orders)


def test_inexact_share_is_refused_not_rounded():
    fills = copy.deepcopy(AVAX_FILLS[:2])
    fills[0].update(size="3", commission="0.1")
    orders = copy.deepcopy(AVAX_ORDERS[:2])
    orders[0].update(filled_size="3", filled_value="314.1", total_fees="0.1", total_value_after_fees="314.0")
    first, = packets(fills)
    with pytest.raises(ValueError, match="exactly"):
        calculate_execution(first, orders_for(first, orders))


def test_split_or_reversing_exit_stays_unsupported(tmp_path):
    fills = copy.deepcopy(AVAX_FILLS[:2])
    fills[1].update(size="6", commission="1.2822")  # covers 4 and reverses 2 long
    from tradebook.coinbase_closes import assemble_closes
    spec = classify_product({"product_id": MARKET, "product_type": "FUTURE", "quote_currency_id": "USD",
               "future_product_details": {"contract_expiry_type": "EXPIRING", "risk_managed_by": "MANAGED_BY_FCM"}},
              account_family="CFM", source_reference="fixture")
    closes = assemble_closes(fills, product=spec, source_key="coinbase.cfm.dated_future", account_id=ACCOUNT,
                             assume_cfm_commission_usd=True).closes
    assert len(closes) == 1 and closes[0].closed_quantity == 4
    path, db, ids = book(tmp_path, closes)
    collected, skipped, failed, deferred = collect_pending_executions(db, [MARKET], ACCOUNT, HTTP())
    assert (len(collected), skipped, failed) == (0, 1, 0)
    db.close()


def test_oversubscribed_sibling_refuses_every_close_of_that_entry(tmp_path):
    closes = packets()
    path, db, ids = book(tmp_path, [*closes, oversubscribed_packet(closes)])
    http = HTTP()
    collected, skipped, failed, deferred = collect_pending_executions(db, [MARKET], ACCOUNT, http)
    # The forged close is a split exit (skipped); both real closes are refused.
    assert not collected and (skipped, failed) == (1, 2)
    db.close()


def test_duplicate_close_with_shared_closing_fill_is_refused(tmp_path):
    first, second = packets()
    twin = second.model_copy(deep=True, update={"close_id": "twin"})
    path, db, ids = book(tmp_path, [first, second, twin])
    collected, skipped, failed, deferred = collect_pending_executions(db, [MARKET], ACCOUNT, HTTP())
    assert not collected and failed == 3
    db.close()
    with pytest.raises(ValueError, match="shared"):
        check_entry_allocation_from_book(tmp_path / "again", [first, second, twin], second)


def test_inconsistent_saved_entry_is_refused(tmp_path):
    first, second = packets()
    other = second.model_copy(deep=True)
    other.source_events[0].payload["price"] = "10.48"
    with pytest.raises(ValueError):
        check_entry_allocation_from_book(tmp_path, [first, other], first)


def check_entry_allocation_from_book(tmp_path, closes, target):
    tmp_path.mkdir(exist_ok=True)
    path, db, ids = book(tmp_path, closes)
    try:
        result = calculate_execution(target, orders_for(target))
        check_entry_allocation(db, ids[closes.index(target)], target, orders_for(target), result)
    finally:
        db.close()


def test_stale_collection_cannot_oversubscribe_at_write_time(tmp_path):
    closes = packets()
    path, db, ids = book(tmp_path, closes)
    collected, *_ = collect_pending_executions(db, [MARKET], ACCOUNT, HTTP())
    assert len(collected) == 2
    # A conflicting close lands after collection but before the write.
    upsert_packet(db, oversubscribed_packet(closes))
    with pytest.raises(ValueError, match="exceeds|shared"):
        persist_execution(db, *collected[0][:4])
    with pytest.raises(ValueError, match="exceeds|shared"):
        persist_execution(db, *collected[1][:4])
    assert not db.execute("SELECT * FROM execution_economics").fetchall()
    assert db.execute("SELECT count(*) FROM source_events WHERE event_id LIKE '[\"orders\"%'").fetchone()[0] == 0
    db.close()


def test_already_enriched_sibling_counts_toward_the_cap(tmp_path):
    closes = packets()
    path, db, ids = book(tmp_path, closes)
    first_orders = orders_for(closes[0])
    persist_execution(db, ids[0], closes[0], first_orders, calculate_execution(closes[0], first_orders))
    # Corrupt the saved economics of the enriched sibling to claim the whole entry.
    db.execute("UPDATE execution_economics SET entry_notional_usd='418.8' WHERE trade_id=?", (ids[0],))
    second_orders = orders_for(closes[1])
    with pytest.raises(ValueError, match="exceeds"):
        persist_execution(db, ids[1], closes[1], second_orders, calculate_execution(closes[1], second_orders))
    db.close()


def test_cli_write_failure_rolls_back_whole_batch(tmp_path):
    closes = packets()
    path, db, ids = book(tmp_path, closes)
    db.close()
    orders = copy.deepcopy(AVAX_ORDERS)
    calls = []
    from tradebook import coinbase_order_sync
    real = coinbase_order_sync.persist_execution
    def persist(db, *args):
        calls.append(1)
        if len(calls) == 2:
            upsert_packet(db, oversubscribed_packet(closes))
        real(db, *args)
    coinbase_order_sync.persist_execution = persist
    try:
        args = ["--db", str(path), "--market", MARKET, "--portfolio-id", ACCOUNT, "--pending-only",
                "--confirm-live-read", "--confirm-local-import"]
        assert order_sync_main(args, http=HTTP(orders)) == 2
    finally:
        coinbase_order_sync.persist_execution = real
    with connect(path) as db:
        assert not db.execute("SELECT * FROM execution_economics").fetchall()
        assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
