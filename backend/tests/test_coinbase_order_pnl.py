"""Source-backed execution P/L from matched, complete Coinbase CFM orders."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tradebook.coinbase_order_pnl import calculate_execution, persist_execution
from tradebook.coinbase_order_pnl import collect_saved_executions
from tradebook.coinbase_order_sync import main as order_sync_main
from tradebook.domain import ClosedTradePacket, SourceEvent
from tradebook.store import connect, initialize, get_trade, upsert_packet, update_reason


def fixture():
    fills = [
        dict(order_id="buy", entry_id="fill-buy", trade_type="FILL", product_id="NOL-CDE", side="BUY", size="1", price="91.79", commission="0.992005", size_in_quote=False),
        dict(order_id="sell", entry_id="fill-sell", trade_type="FILL", product_id="NOL-CDE", side="SELL", size="1", price="96.18", commission="1.0818", size_in_quote=False),
    ]
    timestamps = [datetime(2026, 9, 21, tzinfo=timezone.utc), datetime(2026, 9, 24, tzinfo=timezone.utc)]
    packet = ClosedTradePacket(source_key="coinbase.cfm.dated_future", account_id="acct", close_id="close", product_type="dated_future", market="NOL-CDE", position_side="long", entry_time=timestamps[0], close_time=timestamps[1], closed_quantity=Decimal("1"), quantity_unit="contracts", entry_price=Decimal("91.79"), exit_price=Decimal("96.18"), price_currency="USD", fee_usd=Decimal("2.073805"), fee_currency_assumed=True, source_events=[SourceEvent(event_id=f["entry_id"], occurred_at=t, payload=f) for f, t in zip(fills, timestamps)])
    orders = [
        dict(order_id="buy", product_id="NOL-CDE", retail_portfolio_id="acct", product_type="FUTURE", side="BUY", status="FILLED", number_of_fills="1", filled_size="1", average_filled_price="91.79", filled_value="917.9", total_fees="0.992005", total_value_after_fees="918.892005"),
        dict(order_id="sell", product_id="NOL-CDE", retail_portfolio_id="acct", product_type="FUTURE", side="SELL", status="FILLED", number_of_fills="1", filled_size="1", average_filled_price="96.18", filled_value="961.8", total_fees="1.0818", total_value_after_fees="960.7182"),
    ]
    return packet, orders


def test_long_order_execution_uses_actual_filled_value_not_unverified_multiplier():
    packet, orders = fixture()
    result = calculate_execution(packet, orders)
    assert result.gross_usd == Decimal("43.9")
    assert result.net_after_fees_usd == Decimal("41.826195")
    assert result.entry_notional_usd == Decimal("917.9")
    assert result.order_ids == ("buy", "sell")


def test_short_execution_changes_sign():
    packet, orders = fixture()
    packet = packet.model_copy(update={"position_side": "short", "entry_price": Decimal("321.9"), "exit_price": Decimal("332.65"), "fee_usd": Decimal("0.878455")})
    packet.source_events[0].payload.update(side="SELL", price="321.9", order_id="short")
    packet.source_events[1].payload.update(side="BUY", price="332.65", order_id="cover")
    packet.source_events[0].payload["commission"] = "0.425805"
    packet.source_events[1].payload["commission"] = "0.45265"
    orders[0].update(order_id="short", side="SELL", average_filled_price="321.9", filled_value="321.9", total_fees="0.425805", total_value_after_fees="321.474195")
    orders[1].update(order_id="cover", side="BUY", average_filled_price="332.65", filled_value="332.65", total_fees="0.45265", total_value_after_fees="333.10265")
    result = calculate_execution(packet, orders)
    assert result.gross_usd == Decimal("-10.75")
    assert result.net_after_fees_usd == Decimal("-11.628455")


@pytest.mark.parametrize("change", [
    lambda o: o[0].update(retail_portfolio_id="other"),
    lambda o: o[0].update(total_fees="0"),
    lambda o: o[0].update(filled_value="91.79"),
    lambda o: o[0].update(total_value_after_fees="917.9"),
    lambda o: o[1].update(filled_size="2"),
    lambda o: o[1].update(number_of_fills="2"),
    lambda o: o[1].update(status="OPEN"),
    lambda o: o[1].update(average_filled_price="99"),
    lambda o: o[1].update(total_fees="NaN"),
    lambda o: o[1].update(product_id="other"),
])
def test_mismatched_or_incomplete_order_cannot_publish_pnl(change):
    packet, orders = fixture()
    change(orders)
    with pytest.raises(ValueError):
        calculate_execution(packet, orders)


def test_persist_execution_preserves_reasons_and_raw_fills_on_replay(tmp_path):
    packet, orders = fixture()
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    trade_id = upsert_packet(db, packet)
    update_reason(db, trade_id, "thesis", 0)
    result = calculate_execution(packet, orders)
    persist_execution(db, trade_id, packet, orders, result)
    first = get_trade(db, trade_id)
    assert (first["gross_pnl_usd"], first["execution_net_pnl_usd"], first["position_notional_usd"]) == ("43.9", "41.826195", "917.9")
    assert first["net_pnl_usd"] is None and first["pnl_method"] == "coinbase_orders_ex_funding"
    assert (first["reason"], first["reason_revision"]) == ("thesis", 1)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 4
    upsert_packet(db, packet)
    persist_execution(db, trade_id, packet, orders, result)
    assert get_trade(db, trade_id) == first
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 4
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_replay_with_changed_packet_clears_stale_execution(tmp_path):
    packet, orders = fixture()
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    trade_id = upsert_packet(db, packet)
    persist_execution(db, trade_id, packet, orders, calculate_execution(packet, orders))
    changed = packet.model_copy(update={"fee_usd": Decimal("2.08")})
    upsert_packet(db, changed)
    row = get_trade(db, trade_id)
    assert row["execution_net_pnl_usd"] is None and row["gross_pnl_usd"] is None
    db.close()


def test_collect_saved_execution_uses_only_selected_market_and_matched_order_ids(tmp_path):
    packet, orders = fixture()
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    trade_id = upsert_packet(db, packet)
    paths = []
    class HTTP:
        def get(self, path):
            paths.append(path)
            return {"order": next(o for o in orders if path.endswith("/" + o["order_id"]))}
    collected = collect_saved_executions(db, ["NOL-CDE"], HTTP())
    assert len(collected) == 1 and collected[0][0] == trade_id
    assert collected[0][3].gross_usd == Decimal("43.9")
    assert paths == ["/api/v3/brokerage/orders/historical/buy", "/api/v3/brokerage/orders/historical/sell"]
    assert get_trade(db, trade_id)["gross_pnl_usd"] is None  # collection never writes
    with pytest.raises(ValueError):
        collect_saved_executions(db, ["different-market"], HTTP())
    db.close()


def test_order_sync_requires_explicit_read_and_write_and_backs_up(tmp_path):
    packet, orders = fixture()
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db); trade_id = upsert_packet(db, packet)
    update_reason(db, trade_id, "my reason", 0); db.close()
    class HTTP:
        def get(self, path):
            return {"order": next(o for o in orders if path.endswith("/" + o["order_id"]))}
    flags = ["--db", str(path), "--market", "NOL-CDE"]
    assert order_sync_main(flags + ["--dry-run"], http=HTTP()) == 2
    assert order_sync_main(flags + ["--confirm-live-read", "--dry-run"], http=HTTP()) == 0
    db = connect(path)
    assert get_trade(db, trade_id)["gross_pnl_usd"] is None
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    db.close()
    assert order_sync_main(flags + ["--confirm-live-read"], http=HTTP()) == 2
    assert order_sync_main(flags + ["--confirm-live-read", "--confirm-local-import"], http=HTTP()) == 0
    db = connect(path)
    assert get_trade(db, trade_id)["execution_net_pnl_usd"] == "41.826195"
    assert get_trade(db, trade_id)["reason"] == "my reason"
    db.close()
    assert len(list(tmp_path.glob("*.backup.sqlite3"))) == 1


def test_order_execution_is_public_as_calculated_values_not_raw_order_payload(tmp_path):
    from fastapi.testclient import TestClient
    from tradebook.api import create_app
    packet, orders = fixture()
    path = tmp_path / "api.sqlite3"
    db = connect(path); initialize(db)
    trade_id = upsert_packet(db, packet)
    persist_execution(db, trade_id, packet, orders, calculate_execution(packet, orders))
    db.close()
    client = TestClient(create_app(path))
    for response in (client.get("/api/trades?recent=false"), client.get(f"/api/trades/{trade_id}")):
        assert response.status_code == 200
        value = response.json().get("items", [response.json()])[0]
        assert value["gross_pnl_usd"] == "43.9"
        assert value["execution_net_pnl_usd"] == "41.826195"
        assert value["net_pnl_usd"] is None
        assert value["pnl_method"] == "coinbase_orders_ex_funding"
        assert "packet_json" not in value and "raw_json" not in value


def second_fixture():
    packet, orders = fixture()
    packet = packet.model_copy(deep=True, update={"close_id": "second-close"})
    for event in packet.source_events:
        event.event_id += '-2'
        event.payload['entry_id'] = event.event_id
        event.payload['order_id'] += '-2'
    for order in orders:
        order['order_id'] += '-2'
    return packet, orders


def test_multiple_closes_in_one_market_enrich_individually_and_replay_is_noop(tmp_path):
    packet, orders = fixture()
    other, other_orders = second_fixture()
    path = tmp_path / 'book.sqlite3'
    with connect(path) as db:
        initialize(db)
        ids = [upsert_packet(db, p) for p in (packet, other)]
        update_reason(db, ids[1], 'keep this reason', 0)
    calls = []
    class HTTP:
        def get(self, route):
            calls.append(route)
            return {'order': next(o for o in orders + other_orders if route.endswith('/' + o['order_id']))}
    args = ['--db', str(path), '--market', 'NOL-CDE', '--portfolio-id', 'acct',
            '--pending-only', '--confirm-live-read']
    assert order_sync_main(args + ['--dry-run'], http=HTTP()) == 0
    with connect(path) as db:
        assert not db.execute('SELECT * FROM execution_economics').fetchall()
    calls.clear()
    assert order_sync_main(args + ['--confirm-local-import'], http=HTTP()) == 0
    assert len(calls) == 4
    with connect(path) as db:
        assert all(get_trade(db, i)['position_notional_usd'] == '917.9' for i in ids)
        assert get_trade(db, ids[1])['reason'] == 'keep this reason'
        assert get_trade(db, ids[1])['reason_revision'] == 1
        assert all(get_trade(db, i)['net_pnl_usd'] is None for i in ids)
        # Strict manual selection also supports multiple saved closes now.
        assert len(collect_saved_executions(db, ['NOL-CDE'], HTTP())) == 2
    calls.clear()
    before = path.stat().st_mtime_ns
    assert order_sync_main(args + ['--confirm-local-import'], http=HTTP()) == 0
    assert not calls and path.stat().st_mtime_ns == before
    assert len(list(tmp_path.glob('*.backup.sqlite3'))) == 1


def test_pending_enrichment_isolates_bad_order_and_skips_partial_and_other_account(tmp_path):
    from tradebook.coinbase_order_pnl import collect_pending_executions
    packet, orders = fixture()
    bad, bad_orders = second_fixture()
    bad_orders[0]['total_fees'] = '999'
    partial = packet.model_copy(deep=True, update={'close_id': 'partial', 'closed_quantity': Decimal('0.5')})
    foreign = packet.model_copy(deep=True, update={'close_id': 'foreign', 'account_id': 'another-account'})
    path = tmp_path / 'book.sqlite3'
    with connect(path) as db:
        initialize(db)
        ids = [upsert_packet(db, p) for p in (packet, bad, partial, foreign)]
    calls = []
    class HTTP:
        def get(self, route):
            calls.append(route)
            return {'order': next(o for o in orders + bad_orders if route.endswith('/' + o['order_id']))}
    args = ['--db', str(path), '--market', 'NOL-CDE', '--portfolio-id', 'acct',
            '--pending-only', '--confirm-live-read', '--confirm-local-import']
    assert order_sync_main(args, http=HTTP()) == 2  # Failure is visible, valid close still saved.
    assert len(calls) == 4
    with connect(path) as db:
        assert get_trade(db, ids[0])['execution_net_pnl_usd'] == '41.826195'
        assert all(get_trade(db, i)['execution_net_pnl_usd'] is None for i in ids[1:])
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_pending_enrichment_bounds_order_reads(tmp_path):
    from tradebook.coinbase_order_pnl import collect_pending_executions
    packet, orders = fixture()
    other, other_orders = second_fixture()
    with connect(tmp_path / 'book.sqlite3') as db:
        initialize(db)
        for p in (packet, other): upsert_packet(db, p)
        calls = []
        class HTTP:
            def get(self, route):
                calls.append(route)
                return {'order': next(o for o in orders + other_orders if route.endswith('/' + o['order_id']))}
        collected, skipped, failed, deferred = collect_pending_executions(db, ['NOL-CDE'], 'acct', HTTP(), max_closes=1)
        assert (len(collected), skipped, failed, deferred) == (1, 0, 0, 1)
        assert len(calls) == 2


def test_order_batch_rolls_back_if_saved_packet_changes_before_persistence(tmp_path, monkeypatch):
    from tradebook import coinbase_order_sync
    packet, orders = fixture()
    other, other_orders = second_fixture()
    path = tmp_path / 'book.sqlite3'
    with connect(path) as db:
        initialize(db)
        for p in (packet, other): upsert_packet(db, p)
    class HTTP:
        def get(self, route):
            return {'order': next(o for o in orders + other_orders if route.endswith('/' + o['order_id']))}
    calls = []
    def persist(db, *args):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('saved packet changed during collection')
        persist_execution(db, *args)
    monkeypatch.setattr(coinbase_order_sync, 'persist_execution', persist)
    args = ['--db', str(path), '--market', 'NOL-CDE', '--confirm-live-read', '--confirm-local-import']
    assert order_sync_main(args, http=HTTP()) == 2
    with connect(path) as db:
        assert not db.execute('SELECT * FROM execution_economics').fetchall()
        assert db.execute('SELECT count(*) FROM source_events').fetchone()[0] == 4
