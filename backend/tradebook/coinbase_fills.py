"""Validated pagination for Coinbase Advanced Trade historical fills."""
from __future__ import annotations

from collections.abc import Mapping

from .coinbase_http import FILLS_ROUTE


class CoinbaseFillError(RuntimeError):
    """Historical fills response cannot be safely consumed."""


def iter_fills(http, *, limit: int = 100, product_ids=None, max_records: int = 10000):
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 250:
        raise ValueError("limit must be an integer from 1 to 250")
    params = {"limit": limit}
    if product_ids is not None:
        params["product_ids"] = list(product_ids)
    cursor_seen = set()
    fill_by_id = {}
    cursor = None
    while True:
        request_params = dict(params)
        if cursor is not None:
            request_params["cursor"] = cursor
        page = http.get(FILLS_ROUTE, request_params)
        if not isinstance(page, Mapping) or not isinstance(page.get("fills"), list):
            raise CoinbaseFillError("Invalid Coinbase fills page shape")
        fills = page["fills"]
        for fill in fills:
            if not isinstance(fill, Mapping):
                raise CoinbaseFillError("Invalid fill record")
            entry_id = fill.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                raise CoinbaseFillError("Missing required entry_id")
            if "trade_type" not in fill or "sequence_timestamp" not in fill:
                raise CoinbaseFillError("Fill missing required identity/order fields")
            record = dict(fill)
            previous = fill_by_id.get(entry_id)
            if previous is not None and previous != record:
                raise CoinbaseFillError(f"conflicting duplicate fill entry_id: {entry_id}")
            fill_by_id[entry_id] = record
            if len(fill_by_id) > max_records:
                raise CoinbaseFillError("Coinbase history exceeds configured maximum; refusing truncation")
        # cursor is the documented continuation signal; has_next is accepted
        # only as an optional consistency check for responses that include it.
        next_cursor = page.get("cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise CoinbaseFillError("Invalid Coinbase fills cursor")
        if isinstance(next_cursor, str):
            next_cursor = next_cursor.strip() or None
        has_next = page.get("has_next")
        if has_next is not None and not isinstance(has_next, bool):
            raise CoinbaseFillError("Invalid Coinbase fills has_next")
        if has_next is True and next_cursor is None:
            raise CoinbaseFillError("Coinbase fills has_next is true without a cursor")
        if has_next is False and next_cursor is not None:
            raise CoinbaseFillError("Coinbase fills has_next is false with a continuation cursor")
        if not fills:
            if has_next is True:
                raise CoinbaseFillError("Empty Coinbase fills page has a continuation signal")
            break
        if next_cursor is None:
            if len(fills) >= limit:
                raise CoinbaseFillError("Full Coinbase fills page has no continuation cursor")
            break
        if next_cursor in cursor_seen or next_cursor == cursor:
            raise CoinbaseFillError("Repeated Coinbase fills cursor")
        cursor_seen.add(next_cursor)
        cursor = next_cursor
    return iter(sorted(fill_by_id.values(), key=lambda fill: (fill["sequence_timestamp"], fill["entry_id"])))
