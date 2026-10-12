"""Bracketed positions snapshots: forward tracking, checks, expiry and refusals."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradebook import recurring_sync as runner
from tradebook.coinbase_http import CoinbaseHTTP, CoinbaseReadError
from tradebook.lifecycles import lifecycle_id, project
from tradebook.positions import capture, latest_snapshots, load_checks, parse_positions, tracking_enabled
from tradebook.store import connect, initialize
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save

T0 = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)


def position(product=PRODUCT, side="SHORT", contracts="1", expiry=None):
    item = {"product_id": product, "side": side, "number_of_contracts": contracts}
    if expiry:
        item["expiration_time"] = expiry
    return item


class FakeCoinbase:
    def __init__(self, positions=(), portfolio=ACCOUNT, can_view=True):
        self.positions, self.portfolio, self.can_view, self.calls = list(positions), portfolio, can_view, []

    def get(self, path, params=None):
        self.calls.append(path)
        if path.endswith("key_permissions"):
            return {"can_view": self.can_view, "can_trade": False, "can_transfer": False,
                    "portfolio_uuid": self.portfolio}
        if path.endswith("cfm/positions"):
            return {"positions": self.positions}
        raise AssertionError(path)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "book.sqlite3")
    initialize(connection)
    yield connection
    connection.close()


def take(db, fills, *, at=T0, positions=(), enable=False, after=None, **kwargs):
    reads = iter([list(fills), list(after if after is not None else fills)])
    clock = iter([at, at + timedelta(seconds=1)])
    return capture(FakeCoinbase(positions, **kwargs), db, account_id=ACCOUNT, selections=[(SOURCE, PRODUCT)],
                   clock=lambda: next(clock), enable=enable, read_fills=lambda products: next(reads))


def test_transport_allows_only_the_two_new_read_routes():
    calls = []
    client = CoinbaseHTTP(lambda url, headers, timeout: (calls.append(url) or 200, "{}"), lambda m, u: "t")
    assert client.get("/api/v3/brokerage/cfm/positions") == {}
    assert client.get("/api/v3/brokerage/key_permissions") == {}
    for path in ("/api/v3/brokerage/cfm/positions/BTC", "/api/v3/brokerage/cfm/balance_summary",
                 "/api/v3/brokerage/cfm/sweeps"):
        with pytest.raises(CoinbaseReadError):
            client.get(path)
    with pytest.raises(CoinbaseReadError):
        client.request("POST", "/api/v3/brokerage/cfm/positions")
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [
    {}, {"positions": {}}, {"positions": [{"side": "LONG", "number_of_contracts": "1"}]},
    {"positions": [position(contracts="-1")]}, {"positions": [position(side="UNKNOWN")]},
    {"positions": [position(), position()]}, {"positions": [position(contracts="x")]},
    {"positions": [position(expiry="2026-10-19T00:00:00")]}])
def test_ambiguous_positions_responses_are_refused(payload):
    with pytest.raises(ValueError):
        parse_positions(payload)


def test_positions_are_signed_and_keep_expiry():
    parsed = parse_positions({"positions": [position(expiry="2026-10-19T20:00:00Z"),
                                            position("ETP-20DEC30-CDE", "LONG", "2")]})
    assert parsed[PRODUCT]["quantity"] == Decimal(-1)
    assert parsed["ETP-20DEC30-CDE"]["quantity"] == Decimal(2)
    assert parsed[PRODUCT]["expiration_time"] == datetime(2026, 10, 19, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("kwargs", [{"portfolio": "other"}, {"can_view": False}])
def test_key_must_be_a_view_key_for_the_configured_portfolio(db, kwargs):
    with pytest.raises(ValueError, match="configured portfolio"):
        take(db, [], enable=True, **kwargs)
    assert not tracking_enabled(db, ACCOUNT)
    assert db.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0] == 0


def test_enabling_needs_matching_fills_reads_and_is_idempotent(db):
    with pytest.raises(ValueError, match="fills changed"):
        take(db, [], after=[fill("a", "SELL", 1, 29)], enable=True)
    assert not tracking_enabled(db, ACCOUNT)
    assert take(db, [], enable=True)["tracking_enabled"] is True
    assert take(db, [], at=T0 + timedelta(minutes=5), enable=True)["tracking_enabled"] is False
    assert tracking_enabled(db, ACCOUNT)


def test_snapshots_before_enablement_or_with_changed_fills_are_not_checks(db):
    save(db, [fill("a", "SELL", 1, 1)])
    take(db, [fill("a", "SELL", 1, 1)], at=T0 - timedelta(minutes=10), positions=[position()])
    take(db, [fill("a", "SELL", 1, 1)], enable=True, positions=[position()])
    take(db, [fill("a", "SELL", 1, 1)], at=T0 + timedelta(minutes=5),
         after=[fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 34)])
    checks, _, _ = load_checks(db, {(SOURCE, ACCOUNT, PRODUCT): [fill("a", "SELL", 1, 1)]})
    assert [(c.as_of, c.quantity) for c in checks] == [(T0, Decimal(-1))]


def test_empty_snapshot_is_current_evidence_not_proof_of_earlier_flatness(db):
    # LNP shape: the old short/cover/short history predates tracking and stays unattributed.
    history = [fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 2), fill("c", "SELL", 1, 3), fill("d", "BUY", 1, 4)]
    save(db, history)
    take(db, history, enable=True)
    result = project(db, now=T0 + timedelta(minutes=1))
    (scope,) = result.scopes
    assert scope.status == "verified" and scope.lifecycles == ()
    assert all("unresolved" in link for link in result.links.values())


def test_forward_tracking_opens_confirms_and_detects_disagreement(db):
    take(db, [], enable=True)
    opened = [fill("s", "SELL", 1, 40)]
    save(db, opened)
    view = project(db, now=T0 + timedelta(minutes=11))
    (lc,) = view.lifecycles.values()
    assert (lc.lifecycle_id, lc.status, lc.side) == (lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s"), "open", "short")
    assert view.scopes[0].last_check_at == T0  # opened after the last check: pending a check
    take(db, opened, at=T0 + timedelta(minutes=15), positions=[position()])
    assert project(db, now=T0 + timedelta(minutes=16)).scopes[0].last_check_at == T0 + timedelta(minutes=15)
    # Coinbase says flat while fills say short (e.g. a settlement or event missing from fills).
    take(db, opened, at=T0 + timedelta(minutes=20))
    later = project(db, now=T0 + timedelta(minutes=21))
    assert later.lifecycles[lc.lifecycle_id].status == "unresolved"
    assert "fills replay to -1" in later.lifecycles[lc.lifecycle_id].unresolved_reason


def test_a_fill_near_the_snapshot_skips_that_check_for_its_product(db):
    take(db, [], enable=True)
    near = [fill("s", "SELL", 1, 40)]
    save(db, near)
    at = datetime.fromisoformat("2026-10-01T12:40:05+00:00")
    take(db, near, at=at)  # flat reported 5 seconds after the fill: order unproven, not a contradiction
    checks, _, _ = load_checks(db, {(SOURCE, ACCOUNT, PRODUCT): near})
    assert [c.as_of for c in checks] == [T0]
    assert project(db, now=at).lifecycles[lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")].status == "open"


def test_open_position_past_contract_expiry_is_unresolved(db):
    take(db, [], enable=True)
    save(db, [fill("s", "SELL", 1, 40)])
    take(db, [fill("s", "SELL", 1, 40)], at=T0 + timedelta(minutes=15),
         positions=[position(expiry="2026-10-01T13:00:00Z")])
    before = project(db, now=T0 + timedelta(minutes=20))
    after = project(db, now=T0 + timedelta(minutes=31))
    lc = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")
    assert before.lifecycles[lc].status == "open"
    assert after.lifecycles[lc].status == "unresolved" and "expired" in after.lifecycles[lc].unresolved_reason


def test_position_in_a_product_without_synced_fills_is_reported(db):
    take(db, [], enable=True, positions=[position("ETP-20DEC30-CDE", "LONG", "3")])
    extra = [s for s in project(db, now=T0).scopes if s.product_id == "ETP-20DEC30-CDE"]
    assert extra and extra[0].status == "unresolved" and "not synced" in extra[0].reason


def test_latest_snapshot_time_only_counts_usable_snapshots(db):
    assert latest_snapshots(db) == {}
    take(db, [], enable=True)
    take(db, [], at=T0 + timedelta(minutes=5), after=[fill("x", "BUY", 1, 35)])
    assert latest_snapshots(db) == {ACCOUNT: T0 + timedelta(seconds=1)}


def test_recurring_sync_snapshots_only_tracked_portfolios(tmp_path, monkeypatch):
    path = tmp_path / "book.sqlite3"
    config = tmp_path / "sync.json"
    config.write_text(json.dumps({"db": str(path), "max_fills": 1000, "sources": [
        {"source": "coinbase-cfm-dated-future", "product_id": PRODUCT, "portfolio_id": ACCOUNT}]}))
    monkeypatch.setattr(runner, "sync_main", lambda args: 0)
    monkeypatch.setattr(runner, "order_sync_main", lambda args: 0)
    calls = []
    monkeypatch.setattr(runner, "positions_main", lambda args: calls.append(args) or 0)
    db = connect(path); initialize(db); db.close()
    assert runner.main(["--config", str(config), "--confirm-live-read", "--confirm-local-import"]) == 0
    assert calls == []  # tracking never enabled: no positions read at all
    db = connect(path)
    take(db, [], enable=True)
    db.close()
    assert runner.main(["--config", str(config), "--confirm-live-read", "--confirm-local-import"]) == 0
    assert calls and f"{SOURCE}:{PRODUCT}" in calls[0] and "--enable-tracking" not in calls[0]
    assert runner.read_sync_status(path)[f"{ACCOUNT}|positions"]["last_attempt_ok"] is True
    assert runner.main(["--config", str(config), "--confirm-live-read", "--dry-run"]) == 0
    assert len(calls) == 1
