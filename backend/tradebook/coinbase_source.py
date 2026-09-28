"""Fail-closed TradeSource adapters over injected Coinbase fill records."""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

from .coinbase_closes import assemble_closes
from .coinbase_products import ProductSpecification
from .source import TradeSource


_REQUIRED = ("entry_id", "trade_id", "trade_time", "trade_type", "price", "size", "commission", "product_id", "sequence_timestamp", "size_in_quote", "side", "retail_portfolio_id")


class _CoinbaseSource:
    source_key: str
    expected_type: str

    def __init__(self, account_id: str, product: ProductSpecification, fills: Iterable[dict], *, source_key: str | None = None):
        self.account_id = account_id
        self.product = product
        if source_key is not None and source_key != self.source_key:
            raise ValueError("source key mismatch")
        self._fills = tuple(dict(fill) if isinstance(fill, dict) else fill for fill in fills)
        self.unresolved: tuple[str, ...] = ()

    def iter_closes(self):
        reasons: list[str] = []
        seen: set[str] = set()
        valid: list[dict] = []
        expected_family = "spot" if self.expected_type == "spot" else "cfm"
        if not self.account_id or self.product.account_family != expected_family or self.product.product_type != self.expected_type:
            self.unresolved = ("account or product specification mismatch",)
            return iter(())
        for fill in self._fills:
            if not isinstance(fill, dict):
                reasons.append("invalid fill record")
                continue
            missing = [field for field in _REQUIRED if field not in fill]
            if missing:
                reasons.append("missing documented fields: " + ", ".join(missing))
                continue
            entry_id = fill.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
                reasons.append("missing or duplicate entry_id")
                continue
            seen.add(entry_id)
            if fill.get("product_id") != self.product.product_id:
                reasons.append("product mismatch")
                continue
            if fill.get("retail_portfolio_id") != self.account_id:
                reasons.append("account mismatch")
                continue
            if fill.get("size_in_quote") not in (True, False):
                reasons.append(f"{entry_id}: size_in_quote ambiguity")
                continue
            if fill["size_in_quote"] is True and self.expected_type != "spot":
                reasons.append(f"{entry_id}: unsupported derivative quote-sized fill")
                continue
            if fill.get("trade_type") != "FILL":
                reasons.append(f"{entry_id}: adjustment or non-fill event")
                continue
            # Public schema exposes commission but no documented currency.
            normalized = dict(fill)
            if fill["size_in_quote"] is True:
                try:
                    quote_size, price = Decimal(str(fill["size"])), Decimal(str(fill["price"]))
                    if not quote_size.is_finite() or not price.is_finite() or quote_size <= 0 or price <= 0:
                        raise ValueError
                    exact = Fraction(quote_size) / Fraction(price)
                    with localcontext() as context:
                        context.prec = 28
                        base_size = Decimal(exact.numerator) / Decimal(exact.denominator)
                    if Fraction(base_size) != exact:
                        reasons.append(f"{entry_id}: non-exact quote-sized fill")
                        continue
                    normalized["base_size"] = format(base_size, "f")
                except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
                    reasons.append(f"{entry_id}: invalid quote-sized fill")
                    continue
            normalized["fee"] = None
            normalized.pop("fee_currency", None)
            valid.append(normalized)
        assembly = assemble_closes(valid, product=self.product, source_key=self.source_key, account_id=self.account_id)
        self.unresolved = tuple(reasons) + assembly.unresolved
        return iter(assembly.closes)


class CoinbaseSpotSource(_CoinbaseSource):
    source_key = "coinbase.spot"
    expected_type = "spot"


class CoinbaseCFMPerpetualSource(_CoinbaseSource):
    source_key = "coinbase.cfm.us_perpetual"
    expected_type = "perpetual"


class CoinbaseCFMDatedFutureSource(_CoinbaseSource):
    source_key = "coinbase.cfm.dated_future"
    expected_type = "dated_future"


class CoinbaseAssetSpotSource:
    """Already reconciled asset-wide Spot packets, retaining both raw streams."""
    source_key = "coinbase.spot"

    def __init__(self, account_id: str, fills: Iterable[dict], wallet_account_id: str,
                 wallet_events: Iterable[dict], result):
        self.account_id = account_id
        self.wallet_account_id = wallet_account_id
        self._fills = tuple(fills)
        self._wallet_events = tuple(wallet_events)
        self.unresolved = result.unresolved
        self._packets = result.packets

    def iter_closes(self):
        return iter(self._packets)
