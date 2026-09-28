"""Authenticated, read-only portfolio discovery from documented Coinbase fields."""
from __future__ import annotations

from .coinbase_http import PORTFOLIOS_ROUTE


class DiscoveryError(RuntimeError):
    """Portfolio identity could not be verified from the authenticated response."""


_DOCUMENTED_TYPES = {"DEFAULT", "CONSUMER"}


class CoinbaseDiscovery:
    def __init__(self, http):
        self._http = http

    def require_portfolio(self, portfolio_id: str, *, expected_type: str | None = None) -> dict:
        try:
            response = self._http.get(PORTFOLIOS_ROUTE)
            portfolios = response.get("portfolios") if isinstance(response, dict) else None
            if not isinstance(portfolios, list):
                raise ValueError
            matches = [item for item in portfolios if isinstance(item, dict) and item.get("uuid") == portfolio_id]
            if len(matches) != 1:
                raise ValueError
            portfolio = matches[0]
            if portfolio.get("deleted") is not False or portfolio.get("type") not in _DOCUMENTED_TYPES:
                raise ValueError
            # CFM is not a published portfolio type; never infer family from a CLI label.
            if expected_type is not None and expected_type != portfolio["type"]:
                raise ValueError
            return portfolio
        except Exception:
            raise DiscoveryError("Coinbase portfolio identity could not be verified") from None
