import pytest
import traceback

from tradebook.coinbase_http import CoinbaseHTTP, CoinbaseReadError


def test_get_decodes_json_and_authenticates_encoded_get_uri():
    calls = []
    auth = []

    def requester(url, headers, timeout):
        calls.append((url, headers, timeout))
        return 200, '{"ok":true}'

    client = CoinbaseHTTP(requester, lambda method, uri: auth.append((method, uri)) or "secret-token")
    result = client.get("/api/v3/brokerage/orders/historical/fills", {"limit": 100, "product_ids": ["BTC-USD"]})
    assert result == {"ok": True}
    assert calls[0][0].endswith("?limit=100&product_ids=BTC-USD")
    assert auth == [("GET", calls[0][0].removeprefix("https://api.coinbase.com"))]

def test_historical_order_get_allows_only_encoded_single_order_id():
    calls = []
    client = CoinbaseHTTP(lambda url, headers, timeout: (calls.append(url) or 200, '{"order":{}}'), lambda method, uri: "token")
    assert client.get("/api/v3/brokerage/orders/historical/order-1") == {"order": {}}
    for path in ("/api/v3/brokerage/orders/historical/", "/api/v3/brokerage/orders/historical/order/other",
                 "/api/v3/brokerage/orders/historical/..", "/api/v3/brokerage/orders/historical/order?x=1"):
        with pytest.raises(CoinbaseReadError):
            client.get(path)
    assert len(calls) == 1


def test_non_get_and_unallowlisted_route_rejected_before_request():
    calls = []
    client = CoinbaseHTTP(lambda *args: calls.append(args), lambda *args: "t")
    with pytest.raises(CoinbaseReadError):
        client.request("POST", "/api/v3/brokerage/orders/historical/fills")
    with pytest.raises(CoinbaseReadError):
        client.get("/api/v3/brokerage/orders")
    assert calls == []


@pytest.mark.parametrize("response", [(500, "sensitive body"), (200, "not json")])
def test_errors_are_sanitized(response):
    client = CoinbaseHTTP(lambda *args: response, lambda *args: "secret-token")
    with pytest.raises(CoinbaseReadError) as exc:
        client.get("/api/v3/brokerage/orders/historical/fills")
    assert "secret-token" not in str(exc.value)
    assert "sensitive body" not in str(exc.value)


def test_requester_failure_is_sanitized():
    def requester(*args):
        raise TimeoutError("secret-token in endpoint response")
    client = CoinbaseHTTP(requester, lambda *args: "secret-token")
    with pytest.raises(CoinbaseReadError) as exc:
        client.get("/api/v3/brokerage/orders/historical/fills")
    assert "secret-token" not in str(exc.value)

def test_requester_failure_does_not_chain_sensitive_cause():
    def requester(*args):
        raise RuntimeError("private-key-value")
    client = CoinbaseHTTP(requester, lambda *args: "token")
    with pytest.raises(CoinbaseReadError) as exc:
        client.get("/api/v3/brokerage/orders/historical/fills")
    assert "private-key-value" not in "".join(traceback.format_exception(exc.value))
