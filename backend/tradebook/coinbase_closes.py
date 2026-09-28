"""Fail-closed assembly of Coinbase normalized fills into closed trade packets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

from .coinbase_products import ProductSpecification
from .domain import ClosedTradePacket, SourceEvent


@dataclass(frozen=True, slots=True)
class CloseAssembly:
    closes: tuple[ClosedTradePacket, ...]
    unresolved: tuple[str, ...]


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"invalid {field}") from None
    if not result.is_finite():
        raise ValueError(f"invalid {field}")
    return result


def _time(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing trade_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid trade_time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("trade_time must be timezone-aware")
    return parsed


def _fill_order(fill) -> tuple[datetime, str]:
    if isinstance(fill, dict):
        try:
            return _time(fill.get("trade_time")).astimezone(timezone.utc), str(fill.get("entry_id", ""))
        except ValueError:
            pass
    return datetime.max.replace(tzinfo=timezone.utc), ""


def assemble_closes(fills, *, product: ProductSpecification, source_key: str, account_id: str) -> CloseAssembly:
    """Assemble unambiguous single-lot reductions; unsupported events are quarantined."""
    unit = product.quantity_unit
    if not isinstance(unit, str) or not unit.strip() or unit.lower() in {"unknown", "n/a", "null"}:
        return CloseAssembly((), ("unknown quantity unit",))
    if not source_key or not account_id:
        return CloseAssembly((), ("missing source or account identity",))
    closes: list[ClosedTradePacket] = []
    unresolved: list[str] = []
    lots: list[dict] = []
    unknown_episode: list[SourceEvent] = []
    ordered = sorted(fills, key=_fill_order)
    ids = [fill.get("entry_id") for fill in ordered if isinstance(fill, dict)]
    if len(ids) != len(set(ids)):
        return CloseAssembly((), ("duplicate source fill ID",))
    for fill in ordered:
        try:
            if not isinstance(fill, dict):
                raise ValueError("invalid source event")
            if fill.get("product_id") != product.product_id:
                unresolved.append("product mismatch")
                continue
            if fill.get("trade_type") != "FILL":
                unresolved.append("adjustment or non-fill event")
                continue
            entry_id = fill.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                raise ValueError("missing source entry ID")
            side = fill.get("side")
            if side not in ("BUY", "SELL"):
                raise ValueError(f"{entry_id}: unknown direction")
            size = fill.get("base_size", fill.get("size")) if product.product_type == "spot" else fill.get("size")
            qty = _decimal(size, "size")
            price = _decimal(fill.get("price"), "price")
            when = _time(fill.get("trade_time"))
            if qty <= 0 or price <= 0:
                raise ValueError("size and price must be positive")
            if fill.get("position_effect") not in (None, "OPEN", "CLOSE"):
                unresolved.append(f"{entry_id}: unsupported position effect")
                continue
            matching_side = "BUY" if side == "SELL" else "SELL"
            matches = [lot for lot in lots if lot["side"] == matching_side]
            if fill.get("position_effect") == "OPEN" or (not matches and fill.get("position_effect") != "CLOSE"):
                if product.product_type == "spot" and side == "SELL":
                    unresolved.append(f"{entry_id}: missing entry")
                else:
                    lots.append({"id": entry_id, "side": side, "qty": qty, "original_qty": qty,
                                 "price": price, "time": when, "fee": fill.get("fee"),
                                 "fee_currency": fill.get("fee_currency"), "payload": dict(fill), "fee_allocated": Decimal(0)})
                    if unknown_episode:
                        unknown_episode.append(SourceEvent(event_id=entry_id, occurred_at=when, payload=dict(fill)))
                continue
            if product.product_type != "spot" and (len(matches) > 1 or unknown_episode):
                if len(matches) != len(lots) or not matches or qty > sum(lot["qty"] for lot in matches):
                    unresolved.append(f"{entry_id}: ambiguous derivative position or reversal")
                    continue
                if not unknown_episode:
                    unknown_episode = [SourceEvent(event_id=lot["id"], occurred_at=lot["time"],
                                                   payload=lot["payload"]) for lot in lots]
                events = [*unknown_episode, SourceEvent(event_id=entry_id, occurred_at=when, payload=dict(fill))]
                stable_id = str(uuid5(NAMESPACE_URL, f"{source_key}:{account_id}:{product.product_id}:close:{entry_id}"))
                closes.append(ClosedTradePacket(source_key=source_key, account_id=account_id,
                    close_id=stable_id, position_id=None, product_type=product.product_type,
                    market=product.product_id, position_side="long" if matches[0]["side"] == "BUY" else "short",
                    entry_time=None, close_time=when, closed_quantity=qty, quantity_unit=unit,
                    entry_price=None, exit_price=price, contract_multiplier=product.contract_multiplier,
                    price_currency=product.quote_currency, fee_usd=None, funding_usd=None,
                    source_events=events))
                remaining = qty
                for lot in list(matches):
                    used = min(lot["qty"], remaining)
                    lot["qty"] -= used
                    remaining -= used
                    if lot["qty"] == 0:
                        lots.remove(lot)
                    if remaining == 0:
                        break
                if not lots:
                    unknown_episode = []
                continue
            if product.product_type == "spot" and side == "SELL" and matches:
                allocations = []
                remaining_to_close = qty
                for candidate in matches:
                    allocated_qty = min(remaining_to_close, candidate["qty"])
                    if allocated_qty > 0:
                        allocations.append((candidate, allocated_qty))
                        remaining_to_close -= allocated_qty
                    if remaining_to_close == 0:
                        break
                if remaining_to_close > 0:
                    unresolved.append(f"{entry_id}: reversal remainder unresolved")
            elif len(matches) == 1 and len(lots) == 1:
                allocations = [(matches[0], min(qty, matches[0]["qty"]))]
                if qty > matches[0]["qty"]:
                    unresolved.append(f"{entry_id}: reversal remainder unresolved")
            else:
                unresolved.append(f"{entry_id}: ambiguous multiple lots" if lots else f"{entry_id}: missing entry")
                continue
            close_fee = _decimal(fill.get("fee"), "fee") if fill.get("fee") is not None else None
            for lot, close_qty in allocations:
                exit_usd = close_fee * close_qty / qty if close_fee is not None and str(fill.get("fee_currency", "")).upper() == "USD" else None
                entry_fee_usd = None
                if lot["fee"] is not None and str(lot["fee_currency"] or "").upper() == "USD":
                    entry_total = _decimal(lot["fee"], "fee")
                    entry_fee_usd = entry_total * close_qty / lot["original_qty"]
                    lot["fee_allocated"] += entry_fee_usd
                    if lot["qty"] == close_qty:
                        entry_fee_usd = entry_total - (lot["fee_allocated"] - entry_fee_usd)
                fee_usd = exit_usd + entry_fee_usd if exit_usd is not None and entry_fee_usd is not None else None
                stable_id = str(uuid5(NAMESPACE_URL, f"{source_key}:{account_id}:{product.product_id}:{lot['id']}:{entry_id}"))
                events = [SourceEvent(event_id=lot["id"], occurred_at=lot["time"], payload=lot["payload"]),
                          SourceEvent(event_id=entry_id, occurred_at=when, payload=dict(fill))]
                closes.append(ClosedTradePacket(source_key=source_key, account_id=account_id, close_id=stable_id,
                    position_id=None, product_type=product.product_type, market=product.product_id,
                    position_side="long" if lot["side"] == "BUY" else "short", entry_time=lot["time"], close_time=when,
                    closed_quantity=close_qty, quantity_unit=unit, entry_price=lot["price"], exit_price=price,
                    contract_multiplier=Decimal(1) if product.product_type == "spot" else product.contract_multiplier,
                    price_currency=product.quote_currency, fee_usd=fee_usd,
                    funding_usd=None, source_events=events))
                remaining = lot["qty"] - close_qty
                if remaining == 0:
                    lots.remove(lot)
                else:
                    lot["qty"] = remaining
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            unresolved.append(str(exc))
    return CloseAssembly(tuple(closes), tuple(unresolved))
