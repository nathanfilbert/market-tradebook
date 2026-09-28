# backend/tests/test_sync.py
from tradebook.mock import MockSource
from tradebook.sync import sync
from tradebook.store import connect, initialize, list_trades


def test_mock_sync_is_repeatable_and_has_no_manual_trade_inputs(tmp_path):
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    source = MockSource()
    assert sync(db, source) == 4
    assert sync(db, source) == 4
    rows = list_trades(db)
    assert len(rows) == 4
    assert {r["product_type"] for r in rows} == {"spot", "perpetual", "dated_future"}
    assert all(r["reason"] is None for r in rows)
    assert all(r["position_notional_usd"] is not None for r in rows)


def test_coinbase_selection_is_explicit_and_does_not_default_to_mock():
    from tradebook.sync import build_parser, main
    import pytest
    assert build_parser().parse_args([]).source is None
    with pytest.raises(SystemExit):
        main([])
    assert build_parser().parse_args(["--source", "mock"]).source == "mock"
    assert build_parser().parse_args(["--mock"]).mock

def test_packet_batch_rolls_back_first_trade_when_later_packet_conflicts(tmp_path):
    import pytest
    from tradebook.store import upsert_packets_and_events
    packets = list(MockSource().iter_closes())
    second = packets[1].model_copy(deep=True)
    second.source_events[0].payload["kind"] = "conflicting entry"
    db = connect(tmp_path / "atomic.sqlite3")
    initialize(db)
    with pytest.raises(ValueError, match="source event changed"):
        upsert_packets_and_events(db, [packets[0], second], source_key="mock.tradebook", account_id="fictional-demo")
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0

def test_fresh_mock_sync_does_not_create_empty_backup(tmp_path):
    from tradebook.sync import main
    path = tmp_path / "mock.sqlite3"
    assert main(["--mock", "--db", str(path)]) == 0
    assert path.exists()
    assert not path.with_suffix(".sqlite3.bak").exists()


def test_sync_stages_open_and_adjusted_fills_and_projects_atomically(tmp_path):
    from tradebook.coinbase_products import classify_product
    from tradebook.coinbase_source import CoinbaseSpotSource
    from tradebook.store import get_trade, update_reason
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    spec = classify_product({"product_id":"BTC-USD", "product_type":"SPOT", "base_currency_id":"BTC", "quote_currency_id":"USD"}, account_family="SPOT", source_reference="test")
    def fill(i, side, size, kind="FILL"):
        return {"entry_id":i,"trade_id":i,"trade_time":"2026-01-01T00:00:00Z","trade_type":kind,"price":"10","size":size,"commission":"0","product_id":"BTC-USD","sequence_timestamp":i,"size_in_quote":False,"side":side,"retail_portfolio_id":"acct"}
    source = CoinbaseSpotSource("acct", spec, [fill("open", "BUY", "1"), fill("adj", "BUY", "1", "ADJUSTMENT")])
    assert sync(db, source) == 0
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    assert not list_trades(db)


def test_batch_failure_rolls_back_trades_events_and_cursor_preserves_reason(tmp_path):
    from tradebook.store import get_trade, update_reason
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    source = MockSource()
    assert sync(db, source) == 4
    row = list_trades(db)[0]
    update_reason(db, row["id"], "manual", 0)
    assert sync(db, source) == 4
    assert get_trade(db, row["id"])["reason"] == "manual"


def test_late_projection_error_rolls_back_earlier_close_and_raw_fills(tmp_path):
    import pytest
    from tradebook.mock import MockSource
    from tradebook.domain import ClosedTradePacket
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    class BrokenSource:
        source_key = "mock"
        account_id = "demo"
        _fills = ({"entry_id":"raw-1", "trade_type":"FILL"},)
        def iter_closes(self):
            first = list(MockSource().iter_closes())[0]
            yield first
            yield first.model_copy(update={"source_key":"other"})
    with pytest.raises(ValueError, match="packet account/source mismatch"):
        sync(db, BrokenSource())
    assert not list_trades(db)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_asset_source_stages_wallet_inventory_events_idempotently(tmp_path):
    import pytest
    db = connect(tmp_path / "asset.sqlite3"); initialize(db)
    class Source:
        source_key = "coinbase.spot"
        account_id = "portfolio"
        wallet_account_id = "wallet-btc"
        _fills = ()
        _wallet_events = ({"id": "deposit", "type": "pro_deposit",
                           "amount": {"amount": "1", "currency": "BTC"}},)
        def iter_closes(self): return iter(())
    source = Source()
    assert sync(db, source) == 0
    assert sync(db, source) == 0
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
    source._wallet_events = ({"id": "deposit", "type": "pro_deposit",
                              "amount": {"amount": "2", "currency": "BTC"}},)
    with pytest.raises(ValueError, match="source event changed"):
        sync(db, source)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
