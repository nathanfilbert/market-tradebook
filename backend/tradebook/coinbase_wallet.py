"""Bounded, read-only Coinbase v2 wallet event transport; no accounting inference."""
from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlsplit

from .coinbase_http import BASE_URL, CoinbaseHTTP, CoinbaseReadError


class WalletReadError(RuntimeError):
    """Sanitized wallet pagination or response policy failure."""


class CoinbaseWalletReader:
    def __init__(self, requester=None, authenticator=None, *, max_pages: int = 20,
                 max_records: int = 10000, timeout: float = 15.0):
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self._http = CoinbaseHTTP(requester, authenticator, timeout=timeout)
        self.max_pages = max_pages
        self.max_records = max_records

    @staticmethod
    def _safe_uri(uri: str) -> str:
        if not isinstance(uri, str) or not uri:
            raise WalletReadError("Invalid Coinbase pagination URI")
        parsed = urlsplit(uri)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "https" or parsed.netloc != "api.coinbase.com" or parsed.username or parsed.password:
                raise WalletReadError("Coinbase pagination host is not allowed")
            uri = parsed.path + (("?" + parsed.query) if parsed.query else "")
        if not uri.startswith("/v2/accounts") or uri.startswith("//") or "#" in uri:
            raise WalletReadError("Invalid Coinbase pagination URI")
        path = uri.split("?", 1)[0]
        if not CoinbaseHTTP._allowed(path):
            raise WalletReadError("Coinbase pagination route is not allowed")
        return uri

    def _request(self, uri: str):
        safe = self._safe_uri(uri)
        path, _, query = safe.partition("?")
        try:
            return self._http.get(path, parse_qsl(query, keep_blank_values=True))
        except CoinbaseReadError:
            raise WalletReadError("Coinbase wallet read failed") from None

    def _collect(self, initial_uri: str, first_response=None, data_key: str = "data"):
        uri = self._safe_uri(initial_uri)
        results, seen = [], set()
        response = first_response
        for _ in range(self.max_pages):
            if uri in seen:
                raise WalletReadError("Repeated Coinbase pagination URI")
            seen.add(uri)
            if response is None:
                response = self._request(uri)
            if not isinstance(response, dict) or not isinstance(response.get(data_key), list):
                raise WalletReadError("Malformed Coinbase wallet response")
            results.extend(response[data_key])
            if len(results) > self.max_records:
                raise WalletReadError("Coinbase wallet history limit exceeded")
            pagination = response.get("pagination", {})
            if not isinstance(pagination, dict):
                raise WalletReadError("Malformed Coinbase pagination")
            next_uri = pagination.get("next_uri")
            if next_uri in (None, ""):
                return results
            uri = self._safe_uri(next_uri)
            response = None
        raise WalletReadError("Coinbase pagination page limit exceeded")

    def accounts(self):
        return self._collect("/v2/accounts")

    def transactions(self, account_id: str):
        if not isinstance(account_id, str) or not account_id or account_id in (".", "..") or ".." in account_id.split("/") or ("%" in account_id and quote(account_id, safe="") != account_id):
            raise WalletReadError("Invalid Coinbase account ID")
        encoded_id = quote(account_id, safe="")
        return self._collect(f"/v2/accounts/{encoded_id}/transactions")
