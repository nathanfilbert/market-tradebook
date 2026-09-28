"""Pure, fail-closed asset-wide Spot FIFO from two read-only Coinbase streams.

The caller must supply complete wallet history, a current wallet balance and all
Advanced Trade fills for one asset. This function never fetches or writes data.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from .coinbase_products import ProductSpecification
from .domain import ClosedTradePacket, SourceEvent


@dataclass(frozen=True)
class SpotFIFOResult:
    packets: tuple[ClosedTradePacket, ...]
    unresolved: tuple[str, ...]


def _amount(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    amount = Decimal(str(value))
    if not amount.is_finite() or len(amount.as_tuple().digits) > 28:
        raise ValueError("invalid amount or unsupported precision")
    return amount


def _time(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must have timezone")
    return result.astimezone(timezone.utc)


def build_spot_fifo(wallet_events, fills, *, asset: str, account_id: str,
                    wallet_account_id: str, wallet_balance: Decimal,
                    quantity_increment: Decimal,
                    product_specs: Mapping[str, ProductSpecification],
                    source_key: str = "coinbase.spot") -> SpotFIFOResult:
    """Build close allocations only when wallet and trade histories reconcile."""
    try:
        if not all(isinstance(x, str) and x for x in (asset, account_id, wallet_account_id, source_key)):
            raise ValueError("missing account or asset identity")
        balance = _amount(wallet_balance)
        increment = _amount(quantity_increment)
        if increment <= 0 or not product_specs:
            raise ValueError("missing asset increment or product specifications")

        wallet_seen: set[str] = set()
        wallet_total = Decimal(0)
        wallet_orders: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
        wallet_trade_records: dict[tuple[str, str], list[dict]] = defaultdict(list)
        wallet_trade_by_id: dict[str, dict] = {}
        timeline: list[tuple[datetime, str, str, Decimal, dict, str | None, Decimal | None]] = []
        for tx in wallet_events:
            if not isinstance(tx, dict) or not isinstance(tx.get("id"), str) or not tx["id"]:
                raise ValueError("invalid wallet transaction identity")
            if tx["id"] in wallet_seen:
                raise ValueError("duplicate wallet transaction")
            wallet_seen.add(tx["id"])
            if tx.get("status") != "completed":
                raise ValueError("incomplete wallet transaction")
            amount = tx.get("amount")
            if not isinstance(amount, dict) or amount.get("currency") != asset:
                raise ValueError("wallet transaction asset mismatch")
            signed = _amount(amount.get("amount"))
            when = _time(tx.get("created_at"))
            if signed == 0:
                raise ValueError("zero wallet transaction")
            wallet_total += signed
            typ = tx.get("type")
            if typ == "advanced_trade_fill":
                detail = tx.get("advanced_trade_fill")
                if not isinstance(detail, dict):
                    raise ValueError("missing wallet fill linkage")
                order, product = detail.get("order_id"), detail.get("product_id")
                if not isinstance(order, str) or not order or not isinstance(product, str) or product not in product_specs:
                    raise ValueError("unknown wallet fill order or product")
                wallet_orders[(order, product)].append(signed)
                wallet_trade_records[(order, product)].append(tx)
                wallet_trade_by_id[tx["id"]] = tx
                continue  # Execution is applied from the Advanced Trade fill only.
            if typ in {"buy", "receive", "pro_deposit", "send", "pro_withdrawal"}:
                # The signed account amount, not a legacy movement label, is
                # the documented credit/debit direction for this wallet.
                timeline.append((when, "wallet", tx["id"], signed, tx, None, None))
            else:
                raise ValueError("unsupported wallet inventory movement")
        if wallet_total != balance:
            raise ValueError("wallet transaction history does not reconcile to balance")

        fill_seen: set[str] = set()
        used_base_evidence: set[str] = set()
        fill_orders: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
        for source_fill in fills:
            fill = dict(source_fill) if isinstance(source_fill, dict) else source_fill
            if not isinstance(fill, dict) or not isinstance(fill.get("entry_id"), str) or not fill["entry_id"]:
                raise ValueError("invalid trade fill identity")
            if fill["entry_id"] in fill_seen:
                raise ValueError("duplicate trade fill")
            fill_seen.add(fill["entry_id"])
            product = fill.get("product_id")
            if not isinstance(product, str):
                raise ValueError("missing fill product identity")
            spec = product_specs.get(product)
            if not isinstance(spec, ProductSpecification) or spec.product_type != "spot" or spec.quantity_unit != asset:
                raise ValueError("unverified Spot product or asset")
            if fill.get("retail_portfolio_id") != account_id or fill.get("trade_type") != "FILL":
                raise ValueError("unverified fill account or adjustment")
            order = fill.get("order_id")
            if not isinstance(order, str) or not order or fill.get("side") not in {"BUY", "SELL"}:
                raise ValueError("ambiguous fill order or side")
            price = _amount(fill.get("price"))
            if price <= 0:
                raise ValueError("invalid fill price")
            quote_sized = fill.get("size_in_quote")
            if quote_sized is True:
                exact = Fraction(_amount(fill.get("size"))) / Fraction(price)
                if "base_size" not in fill:
                    with localcontext() as context:
                        context.prec = 28
                        computed = Decimal(exact.numerator) / Decimal(exact.denominator)
                    if Fraction(computed) == exact:
                        fill["base_size"] = format(computed, "f")
                    else:
                        candidates = [candidate for candidate in wallet_trade_records[(order, product)]
                                      if _amount(candidate["advanced_trade_fill"].get("fill_price")) == price
                                      and (_amount(candidate["amount"]["amount"]) > 0) == (fill["side"] == "BUY")
                                      and abs(Fraction(abs(_amount(candidate["amount"]["amount"]))) - exact) <= Fraction(increment)]
                        if len(candidates) != 1 or candidates[0]["id"] in used_base_evidence:
                            raise ValueError("quote-sized fill lacks unique source-backed base quantity")
                        fill["base_size"] = str(abs(_amount(candidates[0]["amount"]["amount"])))
                        fill["base_size_source_tx"] = candidates[0]["id"]
                quantity = _amount(fill["base_size"])
                if Fraction(quantity) != exact:
                    tx_id = fill.get("base_size_source_tx")
                    tx = wallet_trade_by_id.get(tx_id) if isinstance(tx_id, str) else None
                    if tx is None or tx_id in used_base_evidence or abs(Fraction(quantity) - exact) > Fraction(increment):
                        raise ValueError("quote-sized conversion lacks unique wallet quantity evidence")
                    detail = tx["advanced_trade_fill"]
                    signed_evidence = _amount(tx["amount"]["amount"])
                    if (detail.get("order_id"), detail.get("product_id")) != (order, product) or (
                        _amount(detail.get("fill_price")) != price or abs(signed_evidence) != quantity or
                        (signed_evidence > 0) != (fill["side"] == "BUY")
                    ):
                        raise ValueError("quote-sized wallet quantity evidence conflicts with fill")
                    candidates = [candidate for candidate in wallet_trade_records[(order, product)]
                                  if _amount(candidate["advanced_trade_fill"].get("fill_price")) == price
                                  and (_amount(candidate["amount"]["amount"]) > 0) == (fill["side"] == "BUY")
                                  and abs(Fraction(abs(_amount(candidate["amount"]["amount"]))) - exact) <= Fraction(increment)]
                    if len(candidates) != 1 or candidates[0]["id"] != tx_id:
                        raise ValueError("ambiguous quote-sized wallet quantity evidence")
                    if not isinstance(tx_id, str):
                        raise ValueError("missing quote-sized wallet transaction ID")
                    used_base_evidence.add(tx_id)
            elif quote_sized is False:
                quantity = _amount(fill.get("size"))
            else:
                raise ValueError("unknown fill quantity unit")
            if quantity <= 0:
                raise ValueError("invalid fill quantity")
            signed = quantity if fill["side"] == "BUY" else -quantity
            when = _time(fill.get("trade_time"))
            fill_orders[(order, product)].append(signed)
            timeline.append((when, "fill", fill["entry_id"], signed, fill, spec.quote_currency, price))
        if wallet_orders.keys() != fill_orders.keys():
            raise ValueError("wallet trades and Advanced Trade fills do not cover the same orders")
        for key, wallet_parts in wallet_orders.items():
            fill_parts = fill_orders[key]
            if len(wallet_parts) != len(fill_parts) or abs(sum(wallet_parts) - sum(fill_parts)) > increment:
                raise ValueError("wallet trade quantities do not reconcile with fills")

        matched_wallet: set[str] = set()
        for _when, kind, _event_id, signed, raw, _quote, price in timeline:
            if kind != "fill":
                continue
            key = (raw["order_id"], raw["product_id"])
            candidates = [tx for tx in wallet_trade_records[key]
                          if _amount(tx["advanced_trade_fill"].get("fill_price")) == price
                          and (_amount(tx["amount"]["amount"]) > 0) == (signed > 0)
                          and abs(_amount(tx["amount"]["amount"]) - signed) <= increment]
            if len(candidates) != 1 or candidates[0]["id"] in matched_wallet:
                raise ValueError("per-fill wallet evidence is ambiguous or mismatched")
            matched_wallet.add(candidates[0]["id"])
            raw["matched_wallet_tx"] = candidates[0]["id"]

        timeline.sort(key=lambda event: (
            event[0], _time(event[4].get("sequence_timestamp")) if event[1] == "fill" else event[0],
            event[1], event[2]))
        for left, right in zip(timeline, timeline[1:]):
            if left[0] != right[0]:
                continue
            same_order_side = (left[1] == right[1] == "fill" and
                               left[4].get("order_id") == right[4].get("order_id") and
                               left[4].get("side") == right[4].get("side"))
            different_posting = (_time(left[4].get("sequence_timestamp")) !=
                                 _time(right[4].get("sequence_timestamp"))) if same_order_side else False
            if not (same_order_side and different_posting):
                raise ValueError("same-time inventory ordering is ambiguous")
        lots: list[dict] = []
        packets: list[ClosedTradePacket] = []
        for when, kind, event_id, signed, raw, quote, price in timeline:
            evidence_id = raw.get("matched_wallet_tx")
            evidence = wallet_trade_by_id.get(evidence_id) if isinstance(evidence_id, str) else None
            if signed > 0:
                lots.append({"qty": signed, "id": event_id, "kind": kind,
                             "time": when, "payload": raw, "quote": quote, "price": price,
                             "evidence_tx": evidence if kind == "fill" else None})
                continue
            remaining = -signed
            while remaining > 0:
                if not lots:
                    raise ValueError("outflow exceeds source-backed inventory")
                lot = lots[0]
                used = min(remaining, lot["qty"])
                if kind == "fill":
                    if not isinstance(quote, str) or price is None:
                        raise ValueError("missing exit quote or price")
                    entry_ref = (f"wallet:{wallet_account_id}:{lot['id']}" if lot["kind"] == "wallet"
                                 else f"fill:{lot['id']}")
                    exit_ref = f"fill:{event_id}"
                    basis = lot["price"] if lot["quote"] == quote else None
                    packet_id = str(uuid5(NAMESPACE_URL, f"{source_key}:{account_id}:{asset}:{entry_ref}:{exit_ref}"))
                    basis_status = ("unknown_transfer_basis" if lot["kind"] == "wallet" else
                                    "cross_currency_unavailable" if basis is None else "known_quote_basis")
                    entry_payload = dict(lot["payload"])
                    if lot["kind"] == "wallet":
                        entry_payload["_wallet_account_id"] = wallet_account_id
                    source_events = [
                        SourceEvent(event_id=entry_ref, occurred_at=lot["time"], payload=entry_payload),
                        SourceEvent(event_id=exit_ref, occurred_at=when, payload=dict(raw)),
                    ]
                    for linked in (lot.get("evidence_tx"), evidence):
                        if linked is not None:
                            source_events.append(SourceEvent(
                                event_id=f"wallet:{wallet_account_id}:{linked['id']}",
                                occurred_at=_time(linked["created_at"]), payload=dict(linked)))
                    packets.append(ClosedTradePacket(
                        source_key=source_key, account_id=account_id, close_id=packet_id,
                        position_id=entry_ref, product_type="spot", market=raw["product_id"],
                        position_side="long", entry_time=lot["time"], close_time=when,
                        closed_quantity=used, quantity_unit=asset, entry_price=basis,
                        exit_price=price, contract_multiplier=Decimal(1), price_currency=quote,
                        fee_usd=None, funding_usd=None,
                        basis_status=basis_status, basis_currency=lot["quote"],
                        source_events=source_events))
                lot["qty"] -= used
                remaining -= used
                if lot["qty"] == 0:
                    lots.pop(0)
        return SpotFIFOResult(tuple(packets), ())
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        return SpotFIFOResult((), (str(exc),))