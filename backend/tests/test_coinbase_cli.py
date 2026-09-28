import pytest
from tradebook.sync import build_parser


def test_parser_requires_explicit_live_selection():
    args = build_parser().parse_args(["--source", "coinbase-spot", "--product-id", "BTC-USD", "--portfolio-id", "p1", "--db", "/tmp/a.db", "--confirm-live-read"])
    assert args.source == "coinbase-spot"
    assert args.product_id == "BTC-USD"
    assert args.portfolio_id == "p1"
    assert args.confirm_live_read


def test_parser_preserves_mock_choice():
    args = build_parser().parse_args(["--source", "mock"])
    assert args.source == "mock"


def test_live_cli_rejects_missing_confirm(monkeypatch):
    from tradebook.sync import validate_live_args
    args = build_parser().parse_args(["--source", "coinbase-spot", "--product-id", "BTC-USD", "--portfolio-id", "p1", "--db", "/tmp/a.db"])
    with pytest.raises(ValueError, match="confirm"):
        validate_live_args(args)

def test_live_source_requires_authenticated_portfolio_membership_before_product_read(monkeypatch):
    from tradebook.sync import _live_source
    from tradebook import coinbase_auth, coinbase_http, coinbase_discovery
    args = build_parser().parse_args(["--source", "coinbase-spot", "--product-id", "BTC-USD",
                                      "--portfolio-id", "p1", "--db", "unused.sqlite3", "--confirm-live-read"])
    monkeypatch.setattr(coinbase_auth, "create_coinbase_authenticator", lambda: lambda *_: "fake")
    calls = []
    class FakeHTTP:
        def __init__(self, **kwargs): pass
        def get(self, path, params=None):
            calls.append(path)
            if path.endswith("/portfolios"):
                return {"portfolios": []}
            raise AssertionError("product read before verified account")
    monkeypatch.setattr(coinbase_http, "CoinbaseHTTP", FakeHTTP)
    with pytest.raises(coinbase_discovery.DiscoveryError):
        _live_source(args)
    assert calls == ["/api/v3/brokerage/portfolios"]

def test_dry_run_loads_project_dotenv_before_validating_credentials(tmp_path, monkeypatch):
    import os
    from tradebook import coinbase_auth, sync
    monkeypatch.delenv("COINBASE_API_KEY_NAME", raising=False)
    monkeypatch.delenv("COINBASE_API_KEY_SECRET", raising=False)
    def load_env():
        os.environ["COINBASE_API_KEY_NAME"] = "organizations/test/apiKeys/test"
        os.environ["COINBASE_API_KEY_SECRET"] = "synthetic"
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", load_env)
    monkeypatch.setattr(sync, "_live_source", lambda args: (None, []))
    db = tmp_path / "dry-run.sqlite3"
    assert sync.main(["--source", "coinbase-spot", "--product-id", "BTC-USD",
                      "--portfolio-id", "p1", "--db", str(db), "--confirm-live-read", "--dry-run"]) == 0
    assert not db.exists()


def test_spot_live_source_dry_run_uses_asset_wide_wallet_inventory(monkeypatch):
    from tradebook.sync import _live_source
    from tradebook import coinbase_auth, coinbase_http, coinbase_wallet
    args = build_parser().parse_args(["--source", "coinbase-spot", "--product-id", "BTC-USD",
                                      "--portfolio-id", "portfolio", "--db", "unused.sqlite3",
                                      "--confirm-live-read", "--dry-run"])
    monkeypatch.setattr(coinbase_auth, "create_coinbase_authenticator", lambda: lambda *_: "fake")
    requests = []
    class FakeHTTP:
        def __init__(self, **kwargs): pass
        def get(self, path, params=None):
            requests.append((path, params))
            if path.endswith("/portfolios"):
                return {"portfolios": [{"uuid": "portfolio", "type": "DEFAULT", "deleted": False}]}
            if path.endswith("/products/BTC-USD") or path.endswith("/products/BTC-USDC"):
                quote = path.rsplit("-", 1)[-1]
                return {"product_id": f"BTC-{quote}", "product_type": "SPOT",
                        "base_currency_id": "BTC", "quote_currency_id": quote,
                        "base_increment": "0.00000001"}
            if path.endswith("/orders/historical/fills"):
                assert params is not None and "product_ids" not in params
                return {"fills": [
                    {"entry_id": "buy", "order_id": "order-buy", "trade_type": "FILL", "side": "BUY",
                     "size": "1", "price": "10", "trade_time": "2025-01-01T00:00:00Z",
                     "sequence_timestamp": "2025-01-01T00:00:00Z", "product_id": "BTC-USDC",
                     "retail_portfolio_id": "portfolio", "size_in_quote": False},
                    {"entry_id": "sell", "order_id": "order-sell", "trade_type": "FILL", "side": "SELL",
                     "size": "0.5", "price": "12", "trade_time": "2025-01-02T00:00:00Z",
                     "sequence_timestamp": "2025-01-02T00:00:00Z", "product_id": "BTC-USD",
                     "retail_portfolio_id": "portfolio", "size_in_quote": False}], "cursor": None}
            raise AssertionError("unexpected GET route")
    class FakeWallet:
        def __init__(self, **kwargs): pass
        def accounts(self):
            return [{"id": "wallet-btc", "currency": {"code": "BTC"},
                     "balance": {"amount": "0.5", "currency": "BTC"}}]
        def transactions(self, account_id):
            assert account_id == "wallet-btc"
            return [{"id": "t-buy", "type": "advanced_trade_fill", "status": "completed",
                     "created_at": "2025-01-01T00:00:00Z", "amount": {"amount": "1", "currency": "BTC"},
                     "advanced_trade_fill": {"order_id": "order-buy", "product_id": "BTC-USDC", "fill_price": "10"}},
                    {"id": "t-sell", "type": "advanced_trade_fill", "status": "completed",
                     "created_at": "2025-01-02T00:00:00Z", "amount": {"amount": "-0.5", "currency": "BTC"},
                     "advanced_trade_fill": {"order_id": "order-sell", "product_id": "BTC-USD", "fill_price": "12"}}]
    monkeypatch.setattr(coinbase_http, "CoinbaseHTTP", FakeHTTP)
    monkeypatch.setattr(coinbase_wallet, "CoinbaseWalletReader", FakeWallet)
    source, packets = _live_source(args)
    assert len(packets) == 1 and packets[0].entry_price is None
    assert [event.event_id for event in packets[0].source_events] == [
        "fill:buy", "fill:sell", "wallet:wallet-btc:t-buy", "wallet:wallet-btc:t-sell"]
    assert len(source._wallet_events) == 2


def test_spot_source_refuses_wallet_change_during_collection(monkeypatch):
    from tradebook.sync import _live_source
    from tradebook import coinbase_auth, coinbase_http, coinbase_wallet
    monkeypatch.setattr(coinbase_auth, "create_coinbase_authenticator", lambda: lambda *_: "fake")
    args = build_parser().parse_args(["--source", "coinbase-spot", "--product-id", "BTC-USD",
                                      "--portfolio-id", "portfolio", "--db", "unused.sqlite3",
                                      "--confirm-live-read", "--dry-run"])
    class FakeHTTP:
        def __init__(self, **kwargs): pass
        def get(self, path, params=None):
            if path.endswith("/portfolios"):
                return {"portfolios": [{"uuid": "portfolio", "type": "DEFAULT", "deleted": False}]}
            if path.endswith("/products/BTC-USD"):
                return {"product_id": "BTC-USD", "product_type": "SPOT",
                        "base_currency_id": "BTC", "quote_currency_id": "USD", "base_increment": "0.00000001"}
            if path.endswith("/orders/historical/fills"):
                return {"fills": [], "cursor": None}
            raise AssertionError("unexpected read")
    class ChangingWallet:
        calls = 0
        def __init__(self, **kwargs): pass
        def accounts(self):
            ChangingWallet.calls += 1
            return [{"id": "wallet", "currency": {"code": "BTC"},
                     "balance": {"amount": str(ChangingWallet.calls - 1), "currency": "BTC"}}]
        def transactions(self, account_id): return []
    monkeypatch.setattr(coinbase_http, "CoinbaseHTTP", FakeHTTP)
    monkeypatch.setattr(coinbase_wallet, "CoinbaseWalletReader", ChangingWallet)
    with pytest.raises(ValueError, match="changed during collection"):
        _live_source(args)


def test_spot_import_requires_separate_local_write_confirmation(tmp_path, monkeypatch):
    from tradebook import coinbase_auth, sync
    from tradebook.domain import ClosedTradePacket
    from tradebook.store import connect, get_trade, update_reason
    from test_domain import packet
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", lambda: None)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "synthetic")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", "synthetic")
    trade = ClosedTradePacket.model_validate(packet(source_key="coinbase.spot", account_id="portfolio"))
    class Source:
        source_key = "coinbase.spot"
        account_id = "portfolio"
        def iter_closes(self): return iter((trade,))
    monkeypatch.setattr(sync, "_live_source", lambda args: (Source(), [trade]))
    path = tmp_path / "live.sqlite3"
    args = ["--source", "coinbase-spot", "--product-id", "BTC-USD", "--portfolio-id", "portfolio",
            "--db", str(path), "--confirm-live-read"]
    assert sync.main(args) == 2 and not path.exists()
    assert sync.main(args + ["--confirm-local-import"]) == 0
    with connect(path) as db:
        row = db.execute("SELECT id FROM trades").fetchone()
        assert row is not None
        update_reason(db, row["id"], "TEST ONLY", 0)
    assert sync.main(args + ["--confirm-local-import"]) == 0
    with connect(path) as db:
        assert get_trade(db, row["id"])["reason"] == "TEST ONLY"
    assert len(list(tmp_path.glob("live.*.backup.sqlite3"))) == 1


def test_live_import_rejects_existing_mock_database(tmp_path, monkeypatch):
    from tradebook import coinbase_auth, sync
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", lambda: None)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "synthetic")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", "synthetic")
    path = tmp_path / "mixed.sqlite3"
    assert sync.main(["--mock", "--db", str(path)]) == 0
    class Source:
        source_key = "coinbase.spot"
        account_id = "portfolio"
        def iter_closes(self): return iter(())
    monkeypatch.setattr(sync, "_live_source", lambda args: (Source(), []))
    assert sync.main(["--source", "coinbase-spot", "--product-id", "BTC-USD",
                      "--portfolio-id", "portfolio", "--db", str(path),
                      "--confirm-live-read", "--confirm-local-import"]) == 2
    from tradebook.store import connect
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 4


def test_verified_cfm_dated_close_can_be_captured_without_invented_economics(tmp_path, monkeypatch):
    from tradebook import coinbase_auth, sync
    from tradebook.domain import ClosedTradePacket
    from tradebook.store import connect
    from test_domain import packet
    monkeypatch.setattr(coinbase_auth, "load_coinbase_env", lambda: None)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "synthetic")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", "synthetic")
    trade = ClosedTradePacket.model_validate(packet(source_key="coinbase.cfm.dated_future",
                                                     account_id="portfolio", product_type="dated_future",
                                                     contract_multiplier=None, fee_usd=None, funding_usd=None))
    class Source:
        source_key = "coinbase.cfm.dated_future"
        account_id = "portfolio"
        def iter_closes(self): return iter((trade,))
    monkeypatch.setattr(sync, "_live_source", lambda args: (Source(), [trade]))
    path = tmp_path / "cfm.sqlite3"
    assert sync.main(["--source", "coinbase-cfm-dated-future", "--product-id", "EXAMPLE",
                      "--portfolio-id", "portfolio", "--db", str(path),
                      "--confirm-live-read", "--confirm-local-import"]) == 0
    with connect(path) as db:
        row = db.execute("SELECT gross_pnl_usd, net_pnl_usd, position_notional_usd FROM trades").fetchone()
        assert row is not None and tuple(row) == (None, None, None)
