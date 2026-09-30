"""Regression for an ambiguous older episode followed by an independent close."""
import sqlite3
import pytest

from tradebook import coinbase_auth, sync
from tradebook.coinbase_products import classify_product
from tradebook.coinbase_source import CoinbaseCFMDatedFutureSource
from tradebook.store import connect, get_trade, list_trades, update_reason


def source():
    spec = classify_product({"product_id": "BTC-CFM", "product_type": "FUTURE", "quote_currency_id": "USD",
                             "future_product_details": {"contract_expiry_type": "EXPIRING", "risk_managed_by": "MANAGED_BY_FCM"}},
                            account_family="CFM", source_reference="fixture")
    def fill(i, side, qty, day):
        return {"entry_id": i, "trade_id": i, "trade_time": f"2026-09-{day:02}T12:00:00Z",
                "sequence_timestamp": str(day), "trade_type": "FILL", "price": "100", "size": str(qty),
                "commission": "0.1", "product_id": "BTC-CFM", "size_in_quote": False,
                "side": side, "retail_portfolio_id": "portfolio"}
    return CoinbaseCFMDatedFutureSource("portfolio", spec, [
        fill("old-a", "BUY", 1, 1), fill("old-b", "BUY", 1, 2), fill("old-close", "SELL", 2, 3),
        fill("new-entry", "BUY", 2, 29), fill("partial-close", "SELL", 1, 30)])


@pytest.fixture
def selection(tmp_path, monkeypatch):
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", lambda: None)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "synthetic")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", "synthetic")
    s = source()
    packets = list(s.iter_closes())
    assert len(packets) == 2 and packets[0].entry_time is None and not s.unresolved
    monkeypatch.setattr(sync, "_live_source", lambda args: (s, list(s.iter_closes())))
    path = tmp_path / "book.sqlite3"
    args = ["--source", "coinbase-cfm-dated-future", "--product-id", "BTC-CFM",
            "--portfolio-id", "portfolio", "--db", str(path), "--confirm-live-read", "--import-verified-closes"]
    return s, path, args


def test_mixed_history_imports_only_known_partial_close_and_replays_without_writes(selection):
    _, path, args = selection
    assert sync.main(args + ["--dry-run"]) == 0
    assert not path.exists()
    assert sync.main(args + ["--confirm-local-import"]) == 0
    with connect(path) as db:
        rows = list_trades(db)
        assert len(rows) == 1
        row = rows[0]
        assert row["closed_quantity"] == "1" and row["entry_price"] == "100"
        assert row["net_pnl_usd"] is None and row["position_notional_usd"] is None
        assert db.execute('SELECT count(*) FROM source_events WHERE event_id LIKE ?', ('["fills",%',)).fetchone()[0] == 5
        update_reason(db, row["id"], "My rationale", 0)
        db.execute("INSERT INTO execution_economics VALUES (?, '0', '-0.2', '100', 'coinbase_orders_ex_funding')", (row["id"],))
        db.commit()
    before = path.stat().st_mtime_ns
    assert sync.main(args + ["--confirm-local-import"]) == 0
    assert path.stat().st_mtime_ns == before
    assert not list(path.parent.glob('*.backup.sqlite3'))
    with connect(path) as db:
        saved = get_trade(db, row["id"])
        assert saved["reason"] == "My rationale" and saved["reason_revision"] == 1
        assert saved["pnl_method"] == 'coinbase_orders_ex_funding'


def test_new_fill_creates_restorable_backup_and_conflict_rolls_back(selection):
    s, path, args = selection
    assert sync.main(args + ["--confirm-local-import"]) == 0
    # Finish the remaining one-contract allocation; prior partial close is stable.
    f = dict(s._fills[-1], entry_id="final-close", trade_id="final-close", trade_time="2026-09-30T13:00:00Z", price="110")
    s._fills += (f,)
    assert sync.main(args + ["--confirm-local-import"]) == 0
    backups = list(path.parent.glob('*.backup.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT count(*) FROM trades').fetchone()[0] == 1
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    with connect(path) as db:
        assert len(list_trades(db)) == 2
    # Immutable evidence changes must fail without altering published records.
    s._fills = (dict(s._fills[0], price="999"), *s._fills[1:])
    assert sync.main(args + ["--confirm-local-import"]) == 2
    with connect(path) as db:
        assert len(list_trades(db)) == 2
        assert '999' not in db.execute('SELECT raw_json FROM source_events WHERE event_id=?', ('["fills","old-a"]',)).fetchone()[0]


def test_open_only_history_is_staged_without_a_trade(selection):
    s, path, args = selection
    s._fills = (s._fills[0],)
    assert sync.main(args + ["--confirm-local-import"]) == 0
    with connect(path) as db:
        assert not list_trades(db)
        assert db.execute('SELECT count(*) FROM source_events').fetchone()[0] == 1


def test_verified_selection_cannot_be_combined_with_raw_only_staging(selection):
    _, path, args = selection
    assert sync.main(args + ["--confirm-local-import", "--stage-unresolved-events"]) == 2
    assert not path.exists()
