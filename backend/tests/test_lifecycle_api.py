"""Lifecycle routes, entry reasons, trade link annotations and freshness."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tradebook import recurring_sync as runner
from tradebook.api import create_app
from tradebook.domain import ClosedTradePacket
from tradebook.lifecycles import lifecycle_id
from tradebook.store import connect, initialize, upsert_packet
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save
from test_positions import T0, position, take

LC_S = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")


def book(tmp_path, fills=(), *, tracked=True):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    if tracked:
        take(db, [], enable=True)
    if fills:
        save(db, list(fills))
    db.close()
    return path


def snapshot(path, minutes, fills, positions=()):
    db = connect(path)
    take(db, fills, at=T0 + timedelta(minutes=minutes), positions=positions)
    db.close()


def synced(path, minutes):
    """Pretend the fills and positions reads both completed at T0 + minutes."""
    status = {"version": 1, "selections": {key: {"last_attempt_at": "x", "last_attempt_ok": True,
                                                  "last_ok_at": (T0 + timedelta(minutes=minutes)).isoformat()}
                                            for key in (f"{ACCOUNT}|{PRODUCT}", f"{ACCOUNT}|positions")}}
    (path.parent / "book.sqlite3.sync-status.json").write_text(json.dumps(status))


def client(path, minutes):
    return TestClient(create_app(path, clock=lambda: T0 + timedelta(minutes=minutes)))


def test_untracked_book_shows_no_open_position(tmp_path):
    path = book(tmp_path, [fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 2), fill("c", "SELL", 1, 3)], tracked=False)
    body = client(path, 1).get("/api/lifecycles?status=open").json()
    assert body["items"] == [] and body["detached_reasons"] == []
    (scope,) = body["scopes"]
    assert (scope["status"], scope["baseline"]) == ("unavailable", None)
    assert scope["freshness"]["state"] == "unavailable" and scope["positions"]["state"] == "unavailable"
    trade = client(path, 1).get("/api/trades").json()["items"][0]
    assert "no proven flat" in trade["lifecycle"]["unresolved"]


def test_display_moves_from_pending_to_confirmed_to_stale(tmp_path):
    opened = [fill("s", "SELL", 1, 40)]
    path = book(tmp_path, opened)
    snapshot(path, 5, [])
    synced(path, 11)
    item = client(path, 12).get("/api/lifecycles?status=open").json()["items"][0]
    assert (item["lifecycle_id"], item["display"]) == (LC_S, "pending_check")
    snapshot(path, 14, opened, [position()])
    synced(path, 14)
    assert client(path, 15).get(f"/api/lifecycles/{LC_S}").json()["display"] == "confirmed"
    assert client(path, 30).get(f"/api/lifecycles/{LC_S}").json()["display"] == "stale"


def test_entry_reason_revisions_conflicts_and_deliberate_clear(tmp_path):
    path = book(tmp_path, [fill("s", "SELL", 1, 40)])
    api = client(path, 11)
    url = f"/api/lifecycles/{LC_S}/reason"
    first = api.patch(url, json={"reason": "Failed breakout <b>&</b>", "expected_revision": 0}).json()
    assert first["entry_reason"]["state"] == "set" and first["entry_reason"]["revision"] == 1
    assert first["entry_reason"]["reason"] == "Failed breakout <b>&</b>"  # stored as text; the desk escapes
    assert api.patch(url, json={"reason": "stale tab", "expected_revision": 0}).status_code == 409
    cleared = api.patch(url, json={"reason": None, "expected_revision": 1}).json()["entry_reason"]
    assert (cleared["state"], cleared["reason"], cleared["revision"]) == ("cleared", None, 2)
    assert api.patch(url, json={"reason": "x", "expected_revision": 1}).status_code == 409
    assert api.patch(url, json={"reason": "x", "market": "y", "expected_revision": 2}).status_code == 422
    assert api.patch(url, json={"reason": "x" * 4001, "expected_revision": 2}).status_code == 422
    assert api.patch("/api/lifecycles/missing/reason", json={"reason": "x", "expected_revision": 0}).status_code == 404
    with connect(path) as db:
        assert [r[0] for r in db.execute("SELECT revision FROM lifecycle_reason_history ORDER BY revision")] == [1, 2]


def test_unresolved_lifecycle_takes_no_reason(tmp_path):
    path = book(tmp_path, [fill("s", "SELL", 1, 40)])
    snapshot(path, 15, [fill("s", "SELL", 1, 40)])  # Coinbase reports flat: disagreement
    response = client(path, 16).patch(f"/api/lifecycles/{LC_S}/reason", json={"reason": "x", "expected_revision": 0})
    assert response.status_code == 409 and "cannot be attributed" in response.json()["detail"]


def test_closed_trade_shows_linked_reason_without_copying_it(tmp_path):
    fills = [fill("s", "SELL", 1, 40), fill("c", "BUY", 1, 45)]
    path = book(tmp_path, fills)
    api = client(path, 50)
    api.patch(f"/api/lifecycles/{LC_S}/reason", json={"reason": "fade the pop", "expected_revision": 0})
    with connect(path) as db:
        db.execute("ALTER TABLE trades ADD COLUMN notes TEXT")
        db.execute("UPDATE trades SET notes='desk-independent note'")
    (trade,) = api.get("/api/trades").json()["items"]
    assert trade["reason"] is None and trade["reason_revision"] == 0 and trade["notes"] == "desk-independent note"
    assert trade["lifecycle"]["lifecycle_id"] == LC_S
    assert trade["lifecycle"]["entry_reason"]["reason"] == "fade the pop"
    own = api.patch(f"/api/trades/{trade['id']}/reason", json={"reason": "own", "expected_revision": 0}).json()
    assert own["reason"] == "own"
    listed = api.get("/api/lifecycles").json()["items"]
    assert [(i["lifecycle_id"], i["linked_trade_ids"]) for i in listed] == [(LC_S, [trade["id"]])]
    detail = api.get(f"/api/lifecycles/{LC_S}").json()
    assert detail["entry_reason"]["reason"] == "fade the pop" and detail["linked_trade_ids"] == [trade["id"]]


def test_reason_is_kept_and_reported_detached_when_replay_changes_identity(tmp_path):
    path = book(tmp_path, [fill("s", "SELL", 1, 40)])
    api = client(path, 41)
    api.patch(f"/api/lifecycles/{LC_S}/reason", json={"reason": "keep me", "expected_revision": 0})
    with connect(path) as db:  # an earlier fill after the flat point arrives late
        save(db, [fill("e", "SELL", 1, 35)])
    body = api.get("/api/lifecycles").json()
    assert [item["lifecycle_id"] for item in body["items"]] == [lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "e")]
    assert body["items"][0]["entry_reason"]["state"] == "none"  # never moved automatically
    (orphan,) = body["detached_reasons"]
    assert (orphan["lifecycle_id"], orphan["reason"], orphan["opening_entry_id"]) == (LC_S, "keep me", "s")


def test_linked_closes_cannot_exceed_the_closing_fill(tmp_path):
    path = book(tmp_path, [fill("s", "SELL", 1, 40), fill("c", "BUY", 1, 45)])
    with connect(path) as db:  # a second, conflicting close packet citing the same fills
        packet = json.loads(db.execute("SELECT packet_json FROM trades").fetchone()[0])
        packet["close_id"] = "duplicate-close"
        upsert_packet(db, ClosedTradePacket.model_validate(packet))
    items = client(path, 50).get("/api/trades").json()["items"]
    assert len(items) == 2
    assert all("exceed" in item["lifecycle"]["unresolved"] for item in items)


def test_freshness_from_status_file(tmp_path):
    path = book(tmp_path, [fill("a", "BUY", 1, 31)])
    runner.record_sync_status(path, ACCOUNT, PRODUCT, True)
    recorded = runner.read_sync_status(path)[f"{ACCOUNT}|{PRODUCT}"]["last_ok_at"]
    when = datetime.fromisoformat(recorded)
    scope = lambda now: TestClient(create_app(path, clock=lambda: now)).get("/api/lifecycles").json()["scopes"][0]["freshness"]
    assert scope(when + timedelta(minutes=5)) == {"state": "fresh", "last_sync_at": recorded}
    assert scope(when + timedelta(minutes=16))["state"] == "stale"
    runner.record_sync_status(path, ACCOUNT, PRODUCT, False)
    assert scope(when + timedelta(minutes=1))["state"] == "stale"
    (tmp_path / "book.sqlite3.sync-status.json").write_text("not json")
    assert scope(when)["state"] == "unavailable"


def test_recurring_sync_records_each_selection_but_not_on_dry_run(tmp_path, monkeypatch):
    config = tmp_path / "sync.json"
    db = tmp_path / "book.sqlite3"
    config.write_text(json.dumps({"db": str(db), "max_fills": 1000, "sources": [
        {"source": "coinbase-cfm-dated-future", "product_id": p, "portfolio_id": "portfolio"}
        for p in ("BTC-CFM", "OIL-CFM")]}))
    monkeypatch.setattr(runner, "sync_main", lambda args: 2 if "BTC-CFM" in args else 0)
    monkeypatch.setattr(runner, "order_sync_main", lambda args: 0)
    assert runner.main(["--config", str(config), "--confirm-live-read", "--dry-run"]) == 2
    assert runner.read_sync_status(db) == {}
    assert runner.main(["--config", str(config), "--confirm-live-read", "--confirm-local-import"]) == 2
    status = runner.read_sync_status(db)
    assert status["portfolio|OIL-CFM"]["last_attempt_ok"] is True and "last_ok_at" in status["portfolio|OIL-CFM"]
    assert status["portfolio|BTC-CFM"]["last_attempt_ok"] is False and "last_ok_at" not in status["portfolio|BTC-CFM"]
    assert oct((tmp_path / "book.sqlite3.sync-status.json").stat().st_mode & 0o777) == "0o600"


def test_reads_do_not_change_the_database(tmp_path):
    path = book(tmp_path, [fill("s", "SELL", 1, 40)])
    api = client(path, 41)
    api.get("/api/lifecycles")  # first request initializes, as every existing route does
    with connect(path) as db:
        before = db.total_changes, [tuple(r) for r in db.execute("SELECT * FROM sqlite_master")]
    api.get("/api/lifecycles"); api.get(f"/api/lifecycles/{LC_S}"); api.get("/api/trades")
    with connect(path) as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM sqlite_master")] == before[1]
        assert db.execute("SELECT COUNT(*) FROM lifecycle_reasons").fetchone()[0] == 0
