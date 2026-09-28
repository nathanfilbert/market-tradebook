"""Coinbase CFM order-derived execution P/L; excludes funding and settlement cash flows."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from urllib.parse import quote

from .domain import ClosedTradePacket
from .store import canonical_json


@dataclass(frozen=True)
class Execution:
    gross_usd: Decimal
    net_after_fees_usd: Decimal
    entry_notional_usd: Decimal
    order_ids: tuple[str, str]


def _decimal(value, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise ValueError("invalid Coinbase order amount")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid Coinbase order amount") from None
    if not amount.is_finite() or (amount <= 0 if positive else amount < 0):
        raise ValueError("invalid Coinbase order amount")
    return amount


def calculate_execution(packet: ClosedTradePacket, orders: list[dict]) -> Execution:
    """Use complete one-fill order values; never substitute margin or leverage."""
    if (packet.product_type not in {"dated_future", "perpetual"} or packet.price_currency != "USD"
            or packet.entry_time is None or packet.entry_price is None or packet.exit_price is None
            or packet.fee_usd is None or not packet.fee_currency_assumed
            or len(packet.source_events) != 2 or len(orders) != 2):
        raise ValueError("CFM close lacks complete two-order execution evidence")
    entry_side = "BUY" if packet.position_side == "long" else "SELL"
    sides = (entry_side, "SELL" if entry_side == "BUY" else "BUY")
    values: list[Decimal] = []
    fees: list[Decimal] = []
    after_fees: list[Decimal] = []
    ids: list[str] = []
    for event, order, side, price in zip(packet.source_events, orders, sides, (packet.entry_price, packet.exit_price)):
        fill = event.payload
        if not isinstance(fill, dict) or not isinstance(order, dict):
            raise ValueError("invalid order evidence")
        order_id = order.get("order_id")
        if (not isinstance(order_id, str) or not order_id
                or fill.get("order_id") != order_id or fill.get("entry_id") != event.event_id
                or fill.get("trade_type") != "FILL" or fill.get("size_in_quote") is not False
                or fill.get("product_id") != packet.market or fill.get("side") != side
                or order.get("product_id") != packet.market or order.get("retail_portfolio_id") != packet.account_id
                or order.get("product_type") != "FUTURE" or order.get("side") != side
                or order.get("status") != "FILLED" or order.get("number_of_fills") != "1"):
            raise ValueError("order identity, account, or fill completion mismatch")
        if (_decimal(fill.get("size"), positive=True) != packet.closed_quantity
                or _decimal(order.get("filled_size"), positive=True) != packet.closed_quantity
                or _decimal(fill.get("price"), positive=True) != price
                or _decimal(order.get("average_filled_price"), positive=True) != price):
            raise ValueError("order quantity or price mismatch")
        value = _decimal(order.get("filled_value"), positive=True)
        fee = _decimal(order.get("total_fees"))
        after = _decimal(order.get("total_value_after_fees"))
        if fee != _decimal(fill.get("commission")) or after != value + (fee if side == "BUY" else -fee):
            raise ValueError("order fee or after-fees value mismatch")
        values.append(value); fees.append(fee); after_fees.append(after); ids.append(order_id)
    if ids[0] == ids[1] or sum(fees) != packet.fee_usd:
        raise ValueError("duplicate order or unallocated fees")
    # Both orders must imply the same USD value per price movement per contract.
    if values[0] * packet.exit_price != values[1] * packet.entry_price:
        raise ValueError("order value cannot be reconciled to a constant contract size")
    gross = values[1] - values[0] if packet.position_side == "long" else values[0] - values[1]
    net = after_fees[1] - after_fees[0] if packet.position_side == "long" else after_fees[0] - after_fees[1]
    return Execution(gross, net, values[0], (ids[0], ids[1]))


def persist_execution(db: sqlite3.Connection, trade_id: str, packet: ClosedTradePacket,
                      orders: list[dict], result: Execution) -> None:
    if result != calculate_execution(packet, orders):
        raise ValueError("execution does not match order evidence")
    packet_hash = hashlib.sha256(canonical_json(packet.model_dump(mode="json")).encode()).hexdigest()
    stored = db.execute("SELECT packet_hash FROM trades WHERE id=? AND source_key=? AND account_id=?",
                        (trade_id, packet.source_key, packet.account_id)).fetchone()
    if not stored or stored["packet_hash"] != packet_hash:
        raise ValueError("saved trade does not match execution evidence")
    db.execute("SAVEPOINT order_execution")
    try:
        for order in orders:
            event_id = json.dumps(["orders", order["order_id"]], separators=(",", ":"))
            raw = canonical_json(order)
            old = db.execute("SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                             (packet.source_key, packet.account_id, event_id)).fetchone()
            if old and old["raw_json"] != raw:
                raise ValueError("Coinbase order evidence changed")
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                       (packet.source_key, packet.account_id, event_id, raw))
        db.execute("INSERT INTO execution_economics VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(trade_id) DO UPDATE SET gross_execution_usd=excluded.gross_execution_usd, "
                   "net_after_fees_usd=excluded.net_after_fees_usd, entry_notional_usd=excluded.entry_notional_usd, "
                   "pnl_method=excluded.pnl_method",
                   (trade_id, str(result.gross_usd), str(result.net_after_fees_usd),
                    str(result.entry_notional_usd), "coinbase_orders_ex_funding"))
        db.execute("RELEASE SAVEPOINT order_execution")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT order_execution")
        db.execute("RELEASE SAVEPOINT order_execution")
        raise


def collect_saved_executions(db: sqlite3.Connection, markets: list[str], http):
    """Read only selected saved closes and their exact historical orders."""
    if not markets or len(set(markets)) != len(markets):
        raise ValueError("select distinct markets")
    collected = []
    for market in markets:
        if not isinstance(market, str) or not market:
            raise ValueError("invalid market")
        rows = db.execute("SELECT id, packet_json FROM trades WHERE market=? AND source_key LIKE 'coinbase.cfm.%'",
                          (market,)).fetchall()
        if len(rows) != 1:
            raise ValueError("selected market must have exactly one saved CFM close")
        row = rows[0]
        packet = ClosedTradePacket.model_validate_json(row["packet_json"])
        if len(packet.source_events) != 2:
            raise ValueError("requires exactly two source fills")
        orders = []
        for event in packet.source_events:
            order_id = event.payload.get("order_id")
            if (not isinstance(order_id, str) or not order_id or order_id in {".", ".."}
                    or quote(order_id, safe="") != order_id):
                raise ValueError("invalid source order ID")
            response = http.get("/api/v3/brokerage/orders/historical/" + order_id)
            if not isinstance(response, dict) or not isinstance(response.get("order"), dict):
                raise ValueError("invalid historical order response")
            orders.append(response["order"])
        collected.append((row["id"], packet, orders, calculate_execution(packet, orders)))
    return collected
