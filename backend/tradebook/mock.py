# backend/tradebook/mock.py
from collections.abc import Iterable
from .domain import ClosedTradePacket


class MockSource:
    source_key = "mock.tradebook"

    def iter_closes(self) -> Iterable[ClosedTradePacket]:
        entry = dict(event_id="entry-1", occurred_at="2026-09-10T13:00:00Z",
                     payload={"fictional": True, "kind": "entry"})
        for suffix, time, qty, exit_price, fee in (
            ("1", "2026-09-10T14:32:00Z", "0.10", "62000", "12"),
            ("2", "2026-09-10T15:10:00Z", "0.05", "59000", "6"),
        ):
            yield ClosedTradePacket.model_validate(dict(
                source_key=self.source_key, account_id="fictional-demo", close_id=f"close-{suffix}",
                position_id="position-1", product_type="spot", market="BTC-USD",
                position_side="long", entry_time="2026-09-10T13:00:00Z",
                close_time=time, closed_quantity=qty, quantity_unit="BTC",
                entry_price="60000", exit_price=exit_price, contract_multiplier="1",
                price_currency="USD", fee_usd=fee, funding_usd="0",
                source_events=[entry, dict(event_id=f"exit-{suffix}", occurred_at=time,
                    payload={"fictional": True, "kind": "exit"})]))
        for kind, side, idx, qty, entry_price, exit_price, multiplier, fee, funding in (
            ("perpetual", "short", "perp", "2", "70000", "68000", "0.01", "5", "-2"),
            ("dated_future", "long", "future", "1", "100", "102", "10", "2", "0"),
        ):
            yield ClosedTradePacket.model_validate(dict(
                source_key=self.source_key, account_id="fictional-demo", close_id=f"close-{idx}",
                position_id=f"position-{idx}", product_type=kind, market=f"PLACEHOLDER-{idx}",
                position_side=side, entry_time="2026-09-11T13:00:00Z",
                close_time="2026-09-11T14:00:00Z", closed_quantity=qty, quantity_unit="contracts",
                entry_price=entry_price, exit_price=exit_price, contract_multiplier=multiplier,
                price_currency="USD", fee_usd=fee, funding_usd=funding,
                source_events=[
                    dict(event_id=f"entry-{idx}", occurred_at="2026-09-11T13:00:00Z",
                         payload={"fictional": True, "kind": "entry"}),
                    dict(event_id=f"exit-{idx}", occurred_at="2026-09-11T14:00:00Z",
                         payload={"fictional": True, "kind": "exit"})]))
