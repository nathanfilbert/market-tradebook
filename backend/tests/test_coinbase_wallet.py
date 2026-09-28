import pytest

from tradebook.coinbase_wallet import CoinbaseWalletReader, WalletReadError


def fixture_account(account_id="acct/1"):
    return {"id": account_id, "name": "BTC Wallet", "currency": {"code": "BTC"}, "balance": {"amount": "1", "currency": "BTC"}}


def test_reads_accounts_and_transactions_preserving_raw_event_fields():
    calls = []
    pages = {
        "/v2/accounts": {"data": [fixture_account()], "pagination": {"next_uri": "/v2/accounts?cursor=two"}},
        "/v2/accounts?cursor=two": {"data": [], "pagination": {"next_uri": None}},
        "/v2/accounts/acct%2F1/transactions": {"data": [{"id": "tx-1", "type": "advanced_trade_fill", "amount": {"amount": "-0.1", "currency": "BTC"}}], "pagination": {"next_uri": None}},
    }
    def requester(url, headers, timeout):
        uri = url.removeprefix("https://api.coinbase.com")
        calls.append(uri)
        return 200, __import__("json").dumps(pages[uri])
    reader = CoinbaseWalletReader(requester, lambda method, uri: "token")
    accounts = reader.accounts()
    events = reader.transactions("acct/1")
    assert accounts == [fixture_account()]
    assert events == [{"id": "tx-1", "type": "advanced_trade_fill", "amount": {"amount": "-0.1", "currency": "BTC"}}]
    assert calls == ["/v2/accounts", "/v2/accounts?cursor=two", "/v2/accounts/acct%2F1/transactions"]


def test_rejects_off_host_and_repeated_pagination_uri():
    reader = CoinbaseWalletReader(lambda *args: None, lambda *args: "token")
    with pytest.raises(WalletReadError):
        reader._collect("/v2/accounts", {"data": [], "pagination": {"next_uri": "https://evil.example/v2/accounts"}}, "data")
    responses = iter([
        {"data": [], "pagination": {"next_uri": "/v2/accounts?cursor=x"}},
        {"data": [], "pagination": {"next_uri": "/v2/accounts?cursor=x"}},
    ])
    reader._request = lambda uri: next(responses)
    with pytest.raises(WalletReadError):
        reader._collect("/v2/accounts", None, "data")


def test_bounds_pagination_and_rejects_malformed_account_ids():
    reader = CoinbaseWalletReader(lambda *args: None, lambda *args: "token", max_pages=1)
    reader._request = lambda uri: {"data": [], "pagination": {"next_uri": "/v2/accounts?cursor=more"}}
    with pytest.raises(WalletReadError):
        reader.accounts()
    for account_id in ("", "../x", "a/b", "%2f", "?x=1"):
        with pytest.raises(WalletReadError):
            reader.transactions(account_id)

def test_encoded_pagination_cursor_is_not_double_encoded():
    calls = []
    def requester(url, headers, timeout):
        calls.append(url)
        response = ({"data": [], "pagination": {"next_uri": "/v2/accounts?cursor=a%2Fb"}}
                    if len(calls) == 1 else {"data": [], "pagination": {"next_uri": None}})
        return 200, __import__("json").dumps(response)
    reader = CoinbaseWalletReader(requester, lambda method, uri: "token")
    assert reader.accounts() == []
    assert calls[1].endswith("/v2/accounts?cursor=a%2Fb")

def test_wallet_history_cap_refuses_truncation():
    reader = CoinbaseWalletReader(lambda *args: (200, '{"data":[{"id":"1"},{"id":"2"},{"id":"3"}],"pagination":{"next_uri":null}}'),
                                  lambda method, uri: "token", max_records=2)
    with pytest.raises(WalletReadError, match="limit"):
        reader.accounts()
