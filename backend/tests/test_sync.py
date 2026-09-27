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
