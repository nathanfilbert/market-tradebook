"""The deployed notes work and the open-trade lifecycles side by side: neither touches the other's data."""
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from tradebook.api import create_app
from tradebook.lifecycles import lifecycle_id
from tradebook.store import connect, initialize
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save
from test_positions import T0, take

LC = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")
FILLS = [fill("s", "SELL", 1, 40), fill("c", "BUY", 1, 45)]


def book(tmp_path):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    take(db, [], enable=True)
    save(db, FILLS)
    db.close()
    return path, TestClient(create_app(path, clock=lambda: T0 + timedelta(minutes=50)))


def rows(path, sql):
    with connect(path) as db:
        return [tuple(r) for r in db.execute(sql)]


def test_notes_routes_history_and_conflicts_still_work(tmp_path):
    path, api = book(tmp_path)
    (trade,) = api.get("/api/trades").json()["items"]
    url = f"/api/trades/{trade['id']}/notes"
    saved = api.patch(url, json={"notes": "first", "expected_revision": 0}).json()
    assert (saved["notes"], saved["notes_revision"]) == ("first", 1)
    assert api.patch(url, json={"notes": "stale", "expected_revision": 0}).status_code == 409
    assert api.patch(url, json={"notes": None, "expected_revision": 1}).json()["notes"] is None
    assert api.patch("/api/trades/missing/notes", json={"notes": "x", "expected_revision": 0}).status_code == 404
    assert rows(path, "SELECT revision, notes FROM notes_history ORDER BY revision") == [(1, "first"), (2, None)]
    detail = api.get(f"/api/trades/{trade['id']}").json()
    assert detail["notes_revision"] == 2 and detail["lifecycle"]["lifecycle_id"] == LC


def test_reasons_notes_and_lifecycle_reasons_stay_separate(tmp_path):
    path, api = book(tmp_path)
    (trade,) = api.get("/api/trades").json()["items"]
    api.patch(f"/api/trades/{trade['id']}/notes", json={"notes": "manual note", "expected_revision": 0})
    api.patch(f"/api/trades/{trade['id']}/reason", json={"reason": "own reason", "expected_revision": 0})
    before = rows(path, "SELECT reason, reason_revision, notes, notes_revision FROM trades")
    history = rows(path, "SELECT * FROM notes_history") + rows(path, "SELECT * FROM reason_history")
    lc = api.patch(f"/api/lifecycles/{LC}/reason", json={"reason": "written while open", "expected_revision": 0}).json()
    assert lc["entry_reason"]["reason"] == "written while open"
    assert rows(path, "SELECT reason, reason_revision, notes, notes_revision FROM trades") == before
    assert rows(path, "SELECT * FROM notes_history") + rows(path, "SELECT * FROM reason_history") == history
    api.patch(f"/api/trades/{trade['id']}/notes", json={"notes": "second", "expected_revision": 1})
    assert rows(path, "SELECT revision, reason FROM lifecycle_reasons") == [(1, "written while open")]
    shown = api.get(f"/api/trades/{trade['id']}").json()
    assert (shown["reason"], shown["notes"], shown["lifecycle"]["entry_reason"]["reason"]) == (
        "own reason", "second", "written while open")


def test_sync_replay_preserves_notes_and_lifecycle_reasons(tmp_path):
    path, api = book(tmp_path)
    (trade,) = api.get("/api/trades").json()["items"]
    api.patch(f"/api/trades/{trade['id']}/notes", json={"notes": "keep", "expected_revision": 0})
    api.patch(f"/api/lifecycles/{LC}/reason", json={"reason": "keep too", "expected_revision": 0})
    with connect(path) as db:
        save(db, FILLS)                      # the same verified batch again, as each timer run does
        take(db, FILLS, at=T0 + timedelta(minutes=20))
    shown = api.get(f"/api/trades/{trade['id']}").json()
    assert (shown["notes"], shown["notes_revision"]) == ("keep", 1)
    assert shown["lifecycle"]["entry_reason"]["reason"] == "keep too"


def test_initialize_on_a_notes_database_only_adds_tables(tmp_path):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    save(db, FILLS)
    db.execute("UPDATE trades SET notes='deployed note', notes_revision=3, reason='r', reason_revision=2")
    for table in ("position_snapshots", "position_tracking", "lifecycle_reason_history", "lifecycle_reasons"):
        db.execute(f"DROP TABLE {table}")    # the deployed schema, before this feature
    db.commit()
    trades_before = [tuple(r) for r in db.execute("SELECT * FROM trades")]
    initialize(db)
    assert [tuple(r) for r in db.execute("SELECT * FROM trades")] == trades_before
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"notes_history", "position_snapshots", "position_tracking", "lifecycle_reasons",
            "lifecycle_reason_history"} <= tables
    db.close()
