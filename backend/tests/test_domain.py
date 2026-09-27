# backend/tests/test_domain.py
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError
from tradebook.domain import ClosedTradePacket, SourceEvent


def packet(**changes):
    data = dict(source_key="mock.spot", account_id="demo", close_id="close-1",
        position_id="p-1", product_type="spot", market="BTC-USD",
        position_side="long", entry_time="2026-09-10T13:00:00Z",
        close_time="2026-09-10T14:32:00Z",
        closed_quantity="0.10", quantity_unit="BTC", entry_price="60000",
        exit_price="62000", contract_multiplier="1", price_currency="USD",
        fee_usd="12", funding_usd="0",
        source_events=[dict(event_id="entry-1", occurred_at="2026-09-10T13:00:00Z", payload={"action": "buy"}),
                       dict(event_id="exit-1", occurred_at="2026-09-10T14:32:00Z", payload={"action": "sell"})])
    data.update(changes)
    return data


def test_packet_accepts_a_partial_close_allocation():
    p = ClosedTradePacket.model_validate(packet())
    assert p.closed_quantity == Decimal("0.10")
    assert p.close_time == datetime(2026, 9, 10, 14, 32, tzinfo=timezone.utc)
    assert len(p.source_events) == 2


def test_offset_timestamps_normalize_to_utc():
    p = ClosedTradePacket.model_validate(packet(close_time="2026-09-10T15:32:00+01:00"))
    assert p.close_time == datetime(2026, 9, 10, 14, 32, tzinfo=timezone.utc)
    assert p.close_time.utcoffset().total_seconds() == 0


def test_rejects_duplicate_event_ids_inside_one_close_packet():
    data = packet()
    data["source_events"][1]["event_id"] = "entry-1"
    with pytest.raises(ValidationError, match="duplicate source event ID"):
        ClosedTradePacket.model_validate(data)


@pytest.mark.parametrize("changes", [
    {"closed_quantity": "-1"}, {"close_time": "2026-09-10T14:32:00"},
    {"source_events": []}, {"fee_usd": "-1"}, {"extra_trade_field": "manual"},
    {"entry_time": "2026-09-11T13:00:00Z"},
])
def test_rejects_invalid_or_unproven_packet(changes):
    with pytest.raises(ValidationError):
        ClosedTradePacket.model_validate(packet(**changes))
