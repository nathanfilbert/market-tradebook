# backend/tests/test_api.py
from fastapi.testclient import TestClient
from tradebook.api import create_app
from tradebook.domain import ClosedTradePacket
from tradebook.store import connect, initialize, upsert_packet
from test_domain import packet


def client_with_trade(tmp_path):
    path = tmp_path / "book.sqlite3"
    db = connect(path); initialize(db)
    trade_id = upsert_packet(db, ClosedTradePacket.model_validate(packet()))
    db.close()
    return TestClient(create_app(path)), trade_id


def test_list_detail_and_health(tmp_path):
    client, trade_id = client_with_trade(tmp_path)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert len(client.get("/api/trades").json()["items"]) == 1
    assert client.get(f"/api/trades/{trade_id}").json()["market"] == "BTC-USD"
    assert client.get("/api/trades/missing").status_code == 404


def test_only_reason_can_be_patched(tmp_path):
    client, trade_id = client_with_trade(tmp_path)
    path = f"/api/trades/{trade_id}/reason"
    assert client.patch(path, json={"reason": "Failed breakout", "expected_revision": 0}).json()["reason"] == "Failed breakout"
    assert client.patch(path, json={"reason": "stale", "expected_revision": 0}).status_code == 409
    assert client.patch(path, json={"reason": "x", "market": "FAKE", "expected_revision": 1}).status_code == 422
    assert client.patch("/api/trades/missing/reason", json={"reason": "x", "expected_revision": 0}).status_code == 404


def test_pagination_and_limit_validation(tmp_path):
    client, _ = client_with_trade(tmp_path)
    db = connect(tmp_path / "book.sqlite3")
    other = packet(close_id="close-2")
    other["source_events"][1]["event_id"] = "exit-2"
    upsert_packet(db, ClosedTradePacket.model_validate(other)); db.close()
    first = client.get("/api/trades?offset=0&limit=1").json()
    second = client.get("/api/trades?offset=1&limit=1").json()
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["has_more"] is True and second["has_more"] is False
    assert client.get("/api/trades?limit=101").status_code == 422

def test_public_trade_does_not_expose_raw_packet_or_account_payload(tmp_path):
    client, trade_id = client_with_trade(tmp_path)
    for value in (client.get("/api/trades").json()["items"][0], client.get(f"/api/trades/{trade_id}").json()):
        assert "packet_json" not in value
        assert "packet_hash" not in value
    assert "source_events" not in client.get(f"/api/trades/{trade_id}").text
