"""Read-only lifecycle routes, trade link field and sync freshness."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tradebook import recurring_sync as runner
from tradebook.api import create_app
from tradebook.lifecycles import lifecycle_id
from tradebook.store import connect, initialize
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, baseline, fill, save

NOW = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


def book(tmp_path, fills):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    save(db, fills)
    db.close()
    return path


def client(path, baselines=(), now=NOW):
    return TestClient(create_app(path, baselines=baselines, clock=lambda: now))


def test_default_app_reports_unavailable_instead_of_an_open_position(tmp_path):
    path = book(tmp_path, [fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 2), fill("c", "SELL", 1, 3)])
    body = client(path).get("/api/lifecycles?status=open").json()
    assert body["items"] == []
    (scope,) = body["scopes"]
    assert (scope["product_id"], scope["status"], scope["baseline"]) == (PRODUCT, "unavailable", None)
    assert scope["freshness"] == {"state": "unavailable", "last_sync_at": None}
    trade = client(path).get("/api/trades").json()["items"][0]
    assert "no proven flat" in trade["lifecycle"]["unresolved"]


def test_open_lifecycle_detail_and_closed_trade_link(tmp_path):
    path = book(tmp_path, [fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2), fill("c", "SELL", 2, 3)])
    api = client(path, baselines=(baseline(),))
    (item,) = api.get("/api/lifecycles?status=open").json()["items"]
    assert (item["side"], item["open_quantity"], item["scope_status"]) == ("short", "2", "verified")
    closed = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "a")
    detail = api.get(f"/api/lifecycles/{closed}").json()
    assert detail["status"] == "closed" and len(detail["linked_trade_ids"]) == 1
    trade = api.get(f"/api/trades/{detail['linked_trade_ids'][0]}").json()
    assert trade["lifecycle"] == {"lifecycle_id": closed, "status": "closed", "method": "exact_fill"}
    assert trade["reason"] is None  # nothing copied into the closed trade
    assert api.get("/api/lifecycles/missing").status_code == 404
    assert api.get("/api/lifecycles?status=bogus").status_code == 422


def test_freshness_reads_the_sync_status_file(tmp_path):
    path = book(tmp_path, [fill("a", "BUY", 1, 1)])
    runner.record_sync_status(path, ACCOUNT, PRODUCT, True)
    recorded = runner.read_sync_status(path)[f"{ACCOUNT}|{PRODUCT}"]["last_ok_at"]
    when = datetime.fromisoformat(recorded)
    scope = lambda now: client(path, (baseline(),), now).get("/api/lifecycles").json()["scopes"][0]["freshness"]
    assert scope(when + timedelta(minutes=5)) == {"state": "fresh", "last_sync_at": recorded}
    assert scope(when + timedelta(minutes=16))["state"] == "stale"
    runner.record_sync_status(path, ACCOUNT, PRODUCT, False)
    assert scope(when + timedelta(minutes=1))["state"] == "stale"  # the latest read failed
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


def test_lifecycle_routes_write_nothing_beyond_existing_initialization(tmp_path):
    path = book(tmp_path, [fill("a", "BUY", 1, 1)])
    tables = lambda: sorted(r[0] for r in connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'"))
    before = tables()
    client(path, (baseline(),)).get("/api/lifecycles")
    assert tables() == before
