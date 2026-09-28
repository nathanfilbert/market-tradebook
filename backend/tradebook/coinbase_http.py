"""Read-only, allowlisted Coinbase Advanced Trade HTTP transport."""
from __future__ import annotations

import json
from typing import Callable, Mapping, Sequence
from urllib.error import URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.coinbase.com"
FILLS_ROUTE = "/api/v3/brokerage/orders/historical/fills"
PORTFOLIOS_ROUTE = "/api/v3/brokerage/portfolios"
_STATIC_ROUTES = frozenset({FILLS_ROUTE, "/api/v3/brokerage/products", PORTFOLIOS_ROUTE, "/v2/accounts"})
_WALLET_TRANSACTIONS_PREFIX = "/v2/accounts/"


def _wallet_route(path: str) -> bool:
    if not path.startswith(_WALLET_TRANSACTIONS_PREFIX) or not path.endswith("/transactions"):
        return False
    account_id = path[len(_WALLET_TRANSACTIONS_PREFIX):-len("/transactions")]
    decoded = unquote(account_id)
    return bool(account_id) and quote(decoded, safe="") == account_id


class CoinbaseReadError(RuntimeError):
    """Sanitized transport, response, or policy error."""


def _default_requester(url: str, headers: Mapping[str, str], timeout: float):
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except Exception:
        # Do not preserve exception text: it may include credentials or payload.
        raise CoinbaseReadError("Coinbase read request failed") from None


class CoinbaseHTTP:
    def __init__(self, requester: Callable | None = None, authenticator: Callable | None = None,
                 *, timeout: float = 15.0):
        if authenticator is None:
            raise ValueError("An authenticator must be injected")
        self._requester = requester or _default_requester
        self._authenticator = authenticator
        self._timeout = timeout

    @staticmethod
    def _allowed(path: str) -> bool:
        if path in _STATIC_ROUTES or _wallet_route(path):
            return True
        prefix = "/api/v3/brokerage/products/"
        if path.startswith(prefix):
            product_id = path[len(prefix):]
            return bool(product_id) and "/" not in product_id and quote(product_id, safe="") == product_id
        return False

    def get(self, path: str, params: Mapping | Sequence[tuple[str, str]] | None = None):
        return self.request("GET", path, params)

    def request(self, method: str, path: str, params: Mapping | Sequence[tuple[str, str]] | None = None):
        if method != "GET" or not isinstance(path, str) or not self._allowed(path):
            raise CoinbaseReadError("Coinbase route or method is not allowed")
        query = urlencode(params or {}, doseq=True)
        uri = path + (f"?{query}" if query else "")
        url = BASE_URL + uri
        try:
            token = self._authenticator("GET", uri)
            response = self._requester(url, {"Authorization": f"Bearer {token}"}, self._timeout)
            status, body = response
            if status != 200:
                raise CoinbaseReadError(f"Coinbase returned HTTP {status}")
            return json.loads(body)
        except Exception:
            # Injected dependencies can fail with sensitive exception text too.
            raise CoinbaseReadError("Coinbase read request failed or returned invalid JSON") from None
