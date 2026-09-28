import sqlite3

import pytest

from tradebook.store import connect, initialize, ingest_source_page


def test_page_events_and_scoped_checkpoint_commit_atomically(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    assert ingest_source_page(db, "coinbase", "spot-a", "fills", [
        {"event_id": "fill-1", "side": "BUY"},
        {"event_id": "fill-2", "side": "SELL"},
    ], "cursor-2", expected_cursor=None) == 2
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    checkpoint = db.execute(
        "SELECT cursor FROM source_checkpoints WHERE source_key=? AND account_id=? AND stream=?",
        ("coinbase", "spot-a", "fills"),
    ).fetchone()
    assert checkpoint["cursor"] == "cursor-2"


def test_page_replay_is_idempotent_and_checkpoint_is_scoped(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    page = [{"event_id": "fill-1", "side": "BUY"}]
    ingest_source_page(db, "coinbase", "spot-a", "fills", page, "cursor-1", expected_cursor=None)
    ingest_source_page(db, "coinbase", "spot-a", "fills", page, "cursor-1", expected_cursor="cursor-1", same_page_replay=True)
    ingest_source_page(db, "coinbase", "cfm-a", "fills", page, "cursor-cfm", expected_cursor=None)
    ingest_source_page(db, "coinbase", "spot-a", "orders", page, "cursor-orders", expected_cursor=None)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM source_checkpoints").fetchone()[0] == 3


def test_stale_page_is_rejected_and_same_page_retry_is_explicit(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    first = [{"event_id": "one", "side": "BUY"}]
    second = [{"event_id": "two", "side": "SELL"}]
    ingest_source_page(db, "coinbase", "a", "fills", first, "opaque-A", expected_cursor=None)
    ingest_source_page(db, "coinbase", "a", "fills", second, "opaque-B", expected_cursor="opaque-A")
    with pytest.raises(ValueError, match="stale source page"):
        ingest_source_page(db, "coinbase", "a", "fills", first, "opaque-A", expected_cursor=None)
    assert db.execute("SELECT cursor FROM source_checkpoints").fetchone()[0] == "opaque-B"
    ingest_source_page(db, "coinbase", "a", "fills", second, "opaque-B", expected_cursor="opaque-A", same_page_replay=True)


def test_identical_raw_event_ids_are_namespaced_by_stream(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    ingest_source_page(db, "coinbase", "a", "fills", [{"event_id": "same", "x": 1}], "f", expected_cursor=None)
    ingest_source_page(db, "coinbase", "a", "orders", [{"event_id": "same", "x": 2}], "o", expected_cursor=None)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2


def test_conflicting_replay_rolls_back_new_page_events_and_cursor(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    ingest_source_page(db, "coinbase", "spot-a", "fills", [
        {"event_id": "fill-1", "side": "BUY"}], "cursor-1", expected_cursor=None)
    with pytest.raises(ValueError, match="source event changed"):
        ingest_source_page(db, "coinbase", "spot-a", "fills", [
            {"event_id": "fill-2", "side": "SELL"},
            {"event_id": "fill-1", "side": "CORRECTED"}], "cursor-2", expected_cursor="cursor-1")
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
    assert db.execute("SELECT event_id FROM source_events").fetchone()[0] == '["fills","fill-1"]'
    assert db.execute("SELECT cursor FROM source_checkpoints").fetchone()[0] == "cursor-1"


def test_page_write_does_not_touch_trades_or_reason_history(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    db.execute("INSERT INTO trades (id, source_key, account_id, close_id, product_type, market, position_side, close_time, closed_quantity, quantity_unit, price_currency, reconciliation_status, packet_json, packet_hash, reason, reason_revision) VALUES ('t1','mock','a','c1','spot','BTC-USD','long','2026-01-01','1','BTC','USD','ok','{}','hash','keep me',1)")
    db.execute("INSERT INTO reason_history VALUES ('t1',1,'keep me','2026-01-01T00:00:00Z')")
    ingest_source_page(db, "coinbase", "spot-a", "fills", [
        {"event_id": "fill-1", "side": "BUY"}], "cursor-1", expected_cursor=None)
    assert db.execute("SELECT reason, reason_revision FROM trades WHERE id='t1'").fetchone()[:] == ("keep me", 1)
    assert db.execute("SELECT count(*) FROM reason_history").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_duplicate_event_ids_with_conflicting_payload_in_one_page_reject_and_rollback(tmp_path):
    db = connect(tmp_path / "events.sqlite3")
    initialize(db)
    with pytest.raises(ValueError, match="source event changed"):
        ingest_source_page(db, "coinbase", "spot-a", "fills", [
            {"event_id": "same", "side": "BUY"},
            {"event_id": "same", "side": "SELL"}], "cursor-1", expected_cursor=None)
    assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM source_checkpoints").fetchone()[0] == 0
