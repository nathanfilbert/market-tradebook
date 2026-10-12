"""Coinbase CFM order-derived execution P/L; excludes funding and settlement cash flows."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, Inexact, InvalidOperation, localcontext
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


def _share(amount: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    """Exact amount * part / whole; refuse rather than round."""
    with localcontext() as context:
        context.traps[Inexact] = True
        try:
            return amount * part / whole
        except Inexact:
            raise ValueError("partial entry cannot be allocated exactly") from None


def calculate_execution(packet: ClosedTradePacket, orders: list[dict]) -> Execution:
    """Use complete one-fill order values; never substitute margin or leverage.

    A close may use part of its entry fill (one entry filled 4, closed 2 + 2). The
    entry order's value and fee are then allocated by closed/entry quantity, and only
    when that share is exact. The exit fill and order must equal the close exactly.
    """
    if (packet.product_type not in {"dated_future", "perpetual"} or packet.price_currency != "USD"
            or packet.entry_time is None or packet.entry_price is None or packet.exit_price is None
            or packet.fee_usd is None or not packet.fee_currency_assumed
            or len(packet.source_events) != 2 or len(orders) != 2):
        raise ValueError("CFM close lacks complete two-order execution evidence")
    entry_side = "BUY" if packet.position_side == "long" else "SELL"
    sides = (entry_side, "SELL" if entry_side == "BUY" else "BUY")
    sizes: list[Decimal] = []
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
        size = _decimal(fill.get("size"), positive=True)
        if (_decimal(order.get("filled_size"), positive=True) != size
                or _decimal(fill.get("price"), positive=True) != price
                or _decimal(order.get("average_filled_price"), positive=True) != price):
            raise ValueError("order quantity or price mismatch")
        value = _decimal(order.get("filled_value"), positive=True)
        fee = _decimal(order.get("total_fees"))
        after = _decimal(order.get("total_value_after_fees"))
        if fee != _decimal(fill.get("commission")) or after != value + (fee if side == "BUY" else -fee):
            raise ValueError("order fee or after-fees value mismatch")
        sizes.append(size); values.append(value); fees.append(fee); after_fees.append(after); ids.append(order_id)
    closed = packet.closed_quantity
    # Exit must be wholly this close (no reversal or split exit); entry may be shared.
    if sizes[1] != closed or not closed <= sizes[0]:
        raise ValueError("order quantity or price mismatch")
    if closed == sizes[0]:
        entry_value, entry_fee = values[0], fees[0]
    else:
        entry_value, entry_fee = _share(values[0], closed, sizes[0]), _share(fees[0], closed, sizes[0])
    entry_after = entry_value + (entry_fee if entry_side == "BUY" else -entry_fee)
    if ids[0] == ids[1] or entry_fee + fees[1] != packet.fee_usd:
        raise ValueError("duplicate order or unallocated fees")
    # Both orders must imply the same USD value per price movement per contract.
    if values[0] * sizes[1] * packet.exit_price != values[1] * sizes[0] * packet.entry_price:
        raise ValueError("order value cannot be reconciled to a constant contract size")
    gross = values[1] - entry_value if packet.position_side == "long" else entry_value - values[1]
    net = after_fees[1] - entry_after if packet.position_side == "long" else entry_after - after_fees[1]
    return Execution(gross, net, entry_value, (ids[0], ids[1]))


def check_entry_allocation(db: sqlite3.Connection, trade_id: str, packet: ClosedTradePacket,
                           orders: list[dict], result: Execution) -> None:
    """Refuse when saved closes sharing this entry fill would use more than that fill.

    Checks every saved close of the market and account, enriched or not, so that two
    partial closes can never each claim the whole entry. The exit fill must belong to
    this close alone.
    """
    entry, exit_fill = packet.source_events
    entry_size = _decimal(entry.payload.get("size"), positive=True)
    entry_fee = _decimal(orders[0].get("total_fees"))
    quantity = Decimal(0)
    fees = Decimal(0)
    notional = result.entry_notional_usd
    found = False
    rows = db.execute(
        "SELECT t.id, t.packet_json, e.entry_notional_usd FROM trades t "
        "LEFT JOIN execution_economics e ON e.trade_id=t.id WHERE t.account_id=? AND t.market=?",
        (packet.account_id, packet.market)).fetchall()
    for row in rows:
        other = ClosedTradePacket.model_validate_json(row[1])
        event_ids = [event.event_id for event in other.source_events]
        if row[0] == trade_id:
            if other != packet:
                raise ValueError("saved trade does not match execution evidence")
            found = True
        elif exit_fill.event_id in event_ids:
            raise ValueError("closing fill is shared with another close")
        if entry.event_id not in event_ids:
            continue
        if (len(other.source_events) != 2 or other.source_events[0] != entry
                or other.source_key != packet.source_key or other.position_side != packet.position_side
                or other.entry_price != packet.entry_price or other.entry_time != packet.entry_time
                or other.fee_usd is None):
            raise ValueError("entry fill is used inconsistently")
        other_exit = other.source_events[1].payload
        quantity += other.closed_quantity
        fees += other.fee_usd - _share(_decimal(other_exit.get("commission")), other.closed_quantity,
                                       _decimal(other_exit.get("size"), positive=True))
        if row[0] != trade_id and row[2] is not None:
            notional += Decimal(row[2])
    if (not found or quantity > entry_size or fees > entry_fee
            or (quantity == entry_size and fees != entry_fee)
            or notional > _decimal(orders[0].get("filled_value"), positive=True)):
        raise ValueError("entry fill allocation exceeds its order")


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
        # Recheck inside the write so a stale collection cannot oversubscribe an entry.
        check_entry_allocation(db, trade_id, packet, orders, result)
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


def _collect_execution(db, trade_id: str, packet: ClosedTradePacket, http, cache: dict):
    """Read exact source order IDs and refuse conflicts with immutable saved evidence."""
    orders = []
    if len(packet.source_events) != 2:
        raise ValueError("requires exactly two source fills")
    for event in packet.source_events:
        order_id = event.payload.get("order_id")
        if (not isinstance(order_id, str) or not order_id or order_id in {".", ".."}
                or quote(order_id, safe="") != order_id):
            raise ValueError("invalid source order ID")
        if order_id not in cache:
            response = http.get("/api/v3/brokerage/orders/historical/" + order_id)
            if not isinstance(response, dict) or not isinstance(response.get("order"), dict):
                raise ValueError("invalid historical order response")
            cache[order_id] = response["order"]
        order = cache[order_id]
        old = db.execute("SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                         (packet.source_key, packet.account_id,
                          json.dumps(["orders", order_id], separators=(",", ":")))).fetchone()
        if old and old[0] != canonical_json(order):
            raise ValueError("Coinbase order evidence changed")
        orders.append(order)
    result = calculate_execution(packet, orders)
    check_entry_allocation(db, trade_id, packet, orders, result)
    return orders, result


def collect_saved_executions(db: sqlite3.Connection, markets: list[str], http):
    """Strict manual collection: every saved close in selected markets must reconcile."""
    if not markets or len(set(markets)) != len(markets):
        raise ValueError("select distinct markets")
    collected = []
    cache = {}
    for market in markets:
        if not isinstance(market, str) or not market:
            raise ValueError("invalid market")
        rows = db.execute("SELECT id, packet_json FROM trades WHERE market=? AND source_key LIKE 'coinbase.cfm.%' ORDER BY close_time DESC, id",
                          (market,)).fetchall()
        if not rows:
            raise ValueError("selected market has no saved CFM closes")
        for row in rows:
            packet = ClosedTradePacket.model_validate_json(row["packet_json"])
            orders, result = _collect_execution(db, row["id"], packet, http, cache)
            collected.append((row["id"], packet, orders, result))
    return collected


def collect_pending_executions(db: sqlite3.Connection, markets: list[str], account_id: str,
                               http, *, max_closes: int = 100):
    """Bounded automatic enrichment, isolating unavailable/mismatched closes.

    Already enriched rows need no new order reads. Source replay clears enrichment
    when its packet changes. A close using part of its entry fill gets only its exact
    share of the entry order; split or reversing exits stay unsupported.
    """
    if not account_id or not markets or len(set(markets)) != len(markets) or not 1 <= max_closes <= 1000:
        raise ValueError("explicit account, distinct markets and bounded closes required")
    collected, skipped, failed, deferred, attempted = [], 0, 0, 0, 0
    cache = {}
    for market in markets:
        rows = db.execute(
            "SELECT t.id,t.packet_json FROM trades t LEFT JOIN execution_economics e ON e.trade_id=t.id "
            "WHERE t.market=? AND t.account_id=? AND t.source_key LIKE 'coinbase.cfm.%' AND e.trade_id IS NULL "
            "ORDER BY t.close_time DESC,t.id", (market, account_id)).fetchall()
        for row in rows:
            try:
                packet = ClosedTradePacket.model_validate_json(row["packet_json"])
                if (packet.price_currency != "USD" or packet.entry_time is None or packet.entry_price is None
                        or packet.exit_price is None or packet.fee_usd is None or not packet.fee_currency_assumed
                        or len(packet.source_events) != 2
                        or _decimal(packet.source_events[1].payload.get("size"), positive=True) != packet.closed_quantity
                        or _decimal(packet.source_events[0].payload.get("size"), positive=True) < packet.closed_quantity):
                    skipped += 1
                    continue
                if attempted >= max_closes:
                    deferred += 1
                    continue
                attempted += 1
                orders, result = _collect_execution(db, row["id"], packet, http, cache)
                collected.append((row["id"], packet, orders, result))
            except Exception:
                # Never expose dependency exception text, credentials or raw payloads.
                failed += 1
    return collected, skipped, failed, deferred
