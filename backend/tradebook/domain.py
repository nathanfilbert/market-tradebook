# backend/tradebook/domain.py
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceEvent(StrictModel):
    event_id: str = Field(min_length=1)
    occurred_at: datetime
    payload: dict

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_utc(value)


class ClosedTradePacket(StrictModel):
    source_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    close_id: str = Field(min_length=1)
    position_id: str | None = None
    product_type: Literal["spot", "perpetual", "dated_future"]
    market: str = Field(min_length=1)
    position_side: Literal["long", "short"]
    entry_time: datetime | None = None
    close_time: datetime
    closed_quantity: Decimal = Field(gt=0)
    quantity_unit: str = Field(min_length=1)
    entry_price: Decimal | None = Field(default=None, gt=0)
    exit_price: Decimal | None = Field(default=None, gt=0)
    contract_multiplier: Decimal | None = Field(default=None, gt=0)
    price_currency: str = Field(min_length=1)
    fee_usd: Decimal | None = Field(default=None, ge=0)
    fee_currency_assumed: bool = False
    funding_usd: Decimal | None = None
    reported_gross_usd: Decimal | None = None
    reported_net_usd: Decimal | None = None
    basis_status: Literal["known_quote_basis", "unknown_transfer_basis", "cross_currency_unavailable"] | None = None
    basis_currency: str | None = None
    source_events: list[SourceEvent] = Field(min_length=1)

    @field_validator("entry_time", "close_time")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return require_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_times(self):
        if self.entry_time is not None and self.entry_time > self.close_time:
            raise ValueError("entry after close")
        ids = [event.event_id for event in self.source_events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source event ID")
        return self
