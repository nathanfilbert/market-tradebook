"""The real `python -m tradebook.positions` entry point against an already-populated database.

Only the network is faked: the signer, the GET transport's requester and the dotenv loader are replaced, so
argument parsing, the database connection main() opens, staging fills that are already saved, the commit and
the rollback all run as in production. Nothing reaches Coinbase.
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from tradebook import coinbase_auth, coinbase_http, positions
from tradebook import recurring_sync as runner
from tradebook.store import connect, initialize, update_reason
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save

FILLS = [fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 2)]


class FakeCoinbase:
    """Answers the GET-only transport like Coinbase: permissions, product, fills pages and positions."""

    def __init__(self, fills=FILLS, positions=(), fills_after=None, positions_status=200):
        self.fills, self.positions, self.fills_after = list(fills), list(positions), fills_after
        self.positions_status, self.calls, self.fill_reads = positions_status, [], 0

    def __call__(self, url, headers, timeout):
        assert headers["Authorization"] == "Bearer signed"
        parsed = urlparse(url)
        path = parsed.path
        self.calls.append(path)
        if path == "/api/v3/brokerage/key_permissions":
            return 200, json.dumps({"can_view": True, "can_trade": False, "can_transfer": False,
                                    "portfolio_uuid": ACCOUNT})
        if path == f"/api/v3/brokerage/products/{PRODUCT}":
            return 200, json.dumps({"product_id": PRODUCT, "product_type": "FUTURE", "quote_currency_id": "USD",
                                    "future_product_details": {"contract_expiry_type": "EXPIRING",
                                                               "risk_managed_by": "MANAGED_BY_FCM"}})
        if path == "/api/v3/brokerage/orders/historical/fills":
            assert parse_qs(parsed.query)["product_ids"] == [PRODUCT]
            self.fill_reads += 1
            rows = self.fills_after if self.fill_reads % 2 == 0 and self.fills_after is not None else self.fills
            return 200, json.dumps({"fills": rows, "cursor": ""})
        if path == "/api/v3/brokerage/cfm/positions":
            return self.positions_status, json.dumps({"positions": self.positions})
        raise AssertionError(f"unexpected route {path}")


@pytest.fixture
def book(tmp_path, monkeypatch):
    """A database like production's: closes imported, raw fills saved, a reason and notes on the trade."""
    path = tmp_path / "coinbase.sqlite3"
    db = connect(path); initialize(db)
    save(db, FILLS)
    (trade_id,) = [r[0] for r in db.execute("SELECT id FROM trades")]
    update_reason(db, trade_id, "own reason", 0)
    columns = {c[1] for c in db.execute("PRAGMA table_info(trades)")}
    if "notes" in columns:
        db.execute("UPDATE trades SET notes='manual note'")
    db.commit()
    db.close()
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", lambda path=None: None)
    monkeypatch.setattr(coinbase_auth, "create_coinbase_authenticator", lambda generator=None: lambda method, uri: "signed")
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "synthetic")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", "synthetic")
    return path


def serve(monkeypatch, fake):
    monkeypatch.setattr(coinbase_http, "_default_requester", fake)
    return fake


def argv(path, *extra):
    return ["--db", str(path), "--portfolio-id", ACCOUNT, "--product", f"{SOURCE}:{PRODUCT}",
            "--confirm-live-read", "--confirm-local-import", *extra]


def state(path):
    with connect(path) as db:
        return {"snapshots": db.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0],
                "tracking": db.execute("SELECT COUNT(*) FROM position_tracking").fetchone()[0],
                "events": sorted(tuple(r) for r in db.execute("SELECT * FROM source_events")),
                "trades": sorted(tuple(r) for r in db.execute("SELECT * FROM trades")),
                "reasons": sorted(tuple(r) for r in db.execute("SELECT * FROM reason_history"))}


def test_enable_then_recurring_capture_over_already_saved_fills(book, monkeypatch, capsys):
    before = state(book)
    fake = serve(monkeypatch, FakeCoinbase())
    assert positions.main(argv(book, "--enable-tracking")) == 0, capsys.readouterr().out
    assert "tracking enabled now: True" in capsys.readouterr().out
    assert fake.calls.count("/api/v3/brokerage/cfm/positions") == 1 and fake.fill_reads == 2
    enabled = state(book)
    assert (enabled["snapshots"], enabled["tracking"]) == (1, 1)
    # The saved fills were matched, not rewritten; trades, reasons and notes are exactly as they were.
    assert {k: enabled[k] for k in ("events", "trades", "reasons")} == {k: before[k] for k in ("events", "trades", "reasons")}
    # The next timer run: same command without --enable-tracking, same database.
    assert positions.main(argv(book)) == 0
    assert "tracking enabled now: False" in capsys.readouterr().out
    later = state(book)
    assert (later["snapshots"], later["tracking"]) == (2, 1) and later["events"] == before["events"]


def test_failures_roll_back_and_leave_the_database_as_it_was(book, monkeypatch, capsys):
    before = state(book)
    # Fills change between the two reads: the snapshot row is written, then enabling refuses; all rolled back.
    serve(monkeypatch, FakeCoinbase(fills_after=FILLS + [fill("c", "SELL", 1, 3)]))
    assert positions.main(argv(book, "--enable-tracking")) == 2
    assert "refused: ValueError" in capsys.readouterr().out
    assert state(book) == before
    # A saved fill now reads differently (source drift) on a recurring run: refused before commit.
    serve(monkeypatch, FakeCoinbase(fills=[fill("a", "SELL", 2, 1), FILLS[1]]))
    assert positions.main(argv(book)) == 2
    assert state(book) == before
    # Coinbase errors on the positions read: nothing is written.
    serve(monkeypatch, FakeCoinbase(positions_status=500))
    assert positions.main(argv(book, "--enable-tracking")) == 2
    assert state(book) == before


@pytest.mark.parametrize("extra", [["--product", "no-colon-here"], ["--product", f"coinbase.spot:{PRODUCT}"]])
def test_bad_arguments_are_refused_without_writes(book, monkeypatch, extra):
    before = state(book)
    serve(monkeypatch, FakeCoinbase())
    assert positions.main([*argv(book)[:4], *extra, "--confirm-live-read", "--confirm-local-import", "--enable-tracking"]) == 2
    assert positions.main(["--db", str(book), "--portfolio-id", ACCOUNT, "--product", f"{SOURCE}:{PRODUCT}"]) == 2
    assert state(book) == before


def test_recurring_sync_runs_the_real_positions_entry_point(book, tmp_path, monkeypatch):
    serve(monkeypatch, FakeCoinbase())
    assert positions.main(argv(book, "--enable-tracking")) == 0
    config = tmp_path / "sync.json"
    config.write_text(json.dumps({"db": str(book), "max_fills": 1000, "sources": [
        {"source": "coinbase-cfm-dated-future", "product_id": PRODUCT, "portfolio_id": ACCOUNT}]}))
    monkeypatch.setattr(runner, "sync_main", lambda args: 0)
    monkeypatch.setattr(runner, "order_sync_main", lambda args: 0)
    assert runner.main(["--config", str(config), "--confirm-live-read", "--confirm-local-import"]) == 0
    assert state(book)["snapshots"] == 2
    assert runner.read_sync_status(book)[f"{ACCOUNT}|positions"]["last_attempt_ok"] is True
