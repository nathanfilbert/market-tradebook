import pytest

from tradebook.coinbase_discovery import CoinbaseDiscovery, DiscoveryError
from tradebook.coinbase_http import CoinbaseHTTP, CoinbaseReadError

PORTFOLIOS = "/api/v3/brokerage/portfolios"


def client_for(payload):
    calls = []
    http = CoinbaseHTTP(
        lambda url, headers, timeout: calls.append(url) or (200, payload),
        lambda method, uri: "fake-token",
    )
    return CoinbaseDiscovery(http), calls


def test_discovers_exact_active_portfolio_membership():
    discovery, calls = client_for('{"portfolios":[{"uuid":"p-1","type":"CONSUMER","deleted":false}]}')
    assert discovery.require_portfolio("p-1") == {"uuid": "p-1", "type": "CONSUMER", "deleted": False}
    assert calls == ["https://api.coinbase.com" + PORTFOLIOS]


def test_rejects_missing_deleted_and_intx_portfolios():
    for payload, portfolio_id in [
        ('{"portfolios":[]}', "p-1"),
        ('{"portfolios":[{"uuid":"p-1","type":"CONSUMER","deleted":true}]}', "p-1"),
        ('{"portfolios":[{"uuid":"p-1","type":"INTX","deleted":false}]}', "p-1"),
    ]:
        discovery, _ = client_for(payload)
        with pytest.raises(DiscoveryError):
            discovery.require_portfolio(portfolio_id)


def test_refuses_unverified_or_mismatched_requested_account_type():
    discovery, _ = client_for('{"portfolios":[{"uuid":"p-1","type":"CONSUMER","deleted":false}]}')
    with pytest.raises(DiscoveryError):
        discovery.require_portfolio("p-1", expected_type="CFM")
    with pytest.raises(DiscoveryError):
        discovery.require_portfolio("p-1", expected_type="SPOT")
    assert discovery.require_portfolio("p-1", expected_type="CONSUMER")["type"] == "CONSUMER"


def test_rejects_malformed_or_unknown_portfolio_identity():
    for payload in ['{}', '{"portfolios":[{"uuid":"p-1","type":"UNKNOWN","deleted":false}]}', '{"portfolios":[{"uuid":"p-1","deleted":false}]}']:
        discovery, _ = client_for(payload)
        with pytest.raises(DiscoveryError):
            discovery.require_portfolio("p-1")


def test_http_allows_documented_portfolio_read_route_only():
    calls = []
    http = CoinbaseHTTP(lambda *args: calls.append(args) or (200, '{"portfolios":[]}'), lambda *args: "t")
    assert http.get(PORTFOLIOS) == {"portfolios": []}
    with pytest.raises(CoinbaseReadError):
        http.get("/api/v3/brokerage/accounts")
    assert len(calls) == 1
