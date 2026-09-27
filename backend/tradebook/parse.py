# backend/tradebook/parse.py
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from .domain import ClosedTradePacket


def text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def project(p: ClosedTradePacket) -> dict:
    usd = p.price_currency.upper() == "USD"
    supported = usd and not (p.product_type == "spot" and p.position_side == "short")
    factor = p.closed_quantity * p.contract_multiplier if p.contract_multiplier is not None else None
    gross = ((p.exit_price - p.entry_price) * factor * (1 if p.position_side == "long" else -1)
             if supported and factor is not None and p.entry_price is not None and p.exit_price is not None else None)
    notional = (p.entry_price * factor if supported and p.entry_time is not None
                and p.entry_price is not None and factor is not None else None)
    net = gross - p.fee_usd + p.funding_usd if gross is not None and p.fee_usd is not None and p.funding_usd is not None else None
    comparisons = [(calculated, reported) for calculated, reported in (
        (gross, p.reported_gross_usd), (net, p.reported_net_usd)) if reported is not None]
    reconciliation = ("not_available" if not comparisons else
                      "not_comparable" if any(calculated is None for calculated, _ in comparisons) else
                      "mismatch" if any(calculated != reported for calculated, reported in comparisons) else "match")
    key = f"{p.source_key}:{p.account_id}:{p.close_id}"
    return dict(id=str(uuid5(NAMESPACE_URL, key)), source_key=p.source_key,
        account_id=p.account_id, close_id=p.close_id, position_id=p.position_id,
        product_type=p.product_type, market=p.market, position_side=p.position_side,
        entry_time=p.entry_time.isoformat() if p.entry_time is not None else None,
        close_time=p.close_time.isoformat(),
        closed_quantity=text(p.closed_quantity),
        quantity_unit=p.quantity_unit, entry_price=text(p.entry_price),
        exit_price=text(p.exit_price), contract_multiplier=text(p.contract_multiplier),
        price_currency=p.price_currency, position_notional_usd=text(notional),
        gross_pnl_usd=text(gross), fee_usd=text(p.fee_usd),
        funding_usd=text(p.funding_usd), net_pnl_usd=text(net),
        reported_gross_usd=text(p.reported_gross_usd),
        reported_net_usd=text(p.reported_net_usd), reconciliation_status=reconciliation)
