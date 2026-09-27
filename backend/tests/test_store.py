# backend/tests/test_store.py
import sqlite3
from tradebook.store import connect, initialize, list_trades, get_trade


def test_schema_enforces_source_uniqueness(tmp_path):
    db = connect(tmp_path / "book.sqlite3")
    initialize(db)
    db.execute("INSERT INTO source_events (source_key, account_id, event_id, raw_json) VALUES ('mock', 'a', 'e1', '{}')")
    with __import__("pytest").raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO source_events (source_key, account_id, event_id, raw_json) VALUES ('mock', 'a', 'e1', '{}')")
    assert list_trades(db) == []
    assert get_trade(db, "missing") is None

from tradebook.store import upsert_packet
from tradebook.domain import ClosedTradePacket
from test_domain import packet


def test_replay_is_idempotent_and_source_events_remain_immutable(tmp_path):
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    p = ClosedTradePacket.model_validate(packet())
    first = upsert_packet(db, p)
    assert upsert_packet(db, p) == first
    assert len(list_trades(db)) == 1
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    altered = ClosedTradePacket.model_validate(packet(source_events=[
        dict(event_id="entry-1", occurred_at="2026-09-10T13:00:00Z", payload={"action": "changed"}),
        dict(event_id="exit-1", occurred_at="2026-09-10T14:32:00Z", payload={"action": "sell"})]))
    with __import__("pytest").raises(ValueError, match="source event changed"):
        upsert_packet(db, altered)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2


def test_partial_closes_share_entry_event_without_duplicate_rows(tmp_path):
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    upsert_packet(db, ClosedTradePacket.model_validate(packet()))
    second = packet(close_id="close-2", closed_quantity="0.05")
    second["source_events"][1]["event_id"] = "exit-2"
    upsert_packet(db, ClosedTradePacket.model_validate(second))
    assert len(list_trades(db)) == 2
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 3

from tradebook.store import update_reason


def test_reason_update_preserves_exchange_data_and_replay(tmp_path):
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    p = ClosedTradePacket.model_validate(packet())
    trade_id = upsert_packet(db, p)
    old = get_trade(db, trade_id)
    assert update_reason(db, trade_id, "Breakout failed", expected_revision=0)["reason_revision"] == 1
    with __import__("pytest").raises(ValueError, match="revision conflict"):
        update_reason(db, trade_id, "stale", expected_revision=0)
    upsert_packet(db, p)
    new = get_trade(db, trade_id)
    assert new["reason"] == "Breakout failed"
    assert new["gross_pnl_usd"] == old["gross_pnl_usd"]
    assert db.execute("SELECT count(*) FROM reason_history").fetchone()[0] == 1

def test_backup_restores_reason_and_source_rows(tmp_path):
    from tradebook.mock import MockSource
    from tradebook.sync import sync
    from tradebook.store import backup
    db = connect(tmp_path / "live.sqlite3"); initialize(db)
    sync(db, MockSource()); sync(db, MockSource())
    trade_id = list_trades(db)[0]["id"]
    update_reason(db, trade_id, "My thesis", 0)
    backup(db, tmp_path / "copy.sqlite3")
    restored = connect(tmp_path / "copy.sqlite3")
    assert len(list_trades(restored)) == 4
    assert get_trade(restored, trade_id)["reason"] == "My thesis"
    assert restored.execute("SELECT count(*) FROM source_events").fetchone()[0] == 7
    restored.close(); db.close()


def test_incomplete_source_values_remain_null_after_persistence(tmp_path):
    db = connect(tmp_path / "book.sqlite3"); initialize(db)
    p = ClosedTradePacket.model_validate(packet(entry_time=None, entry_price=None,
                                                contract_multiplier=None, funding_usd=None))
    trade_id = upsert_packet(db, p)
    row = get_trade(db, trade_id)
    assert row is not None
    assert row["entry_time"] is None
    assert row["entry_price"] is None
    assert row["position_notional_usd"] is None
    assert row["net_pnl_usd"] is None
    db.close()
