# backend/tradebook/api.py
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .store import connect, initialize, get_trade, list_trades, update_reason
from .sync import DEFAULT_DB


class ReasonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(max_length=4000)
    expected_revision: int = Field(ge=0)


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in {"packet_json", "packet_hash"}}

def create_app(db_path: str | Path) -> FastAPI:
    app = FastAPI(title="Market Tradebook")
    path = Path(db_path)

    def database():
        db = connect(path); initialize(db)
        return db

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/trades")
    def trades(offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
        db = database()
        try:
            rows = list_trades(db, limit + 1, offset)
            return {"items": [public(r) for r in rows[:limit]], "has_more": len(rows) > limit}
        finally:
            db.close()

    @app.get("/api/trades/{trade_id}")
    def trade(trade_id: str):
        db = database()
        try:
            value = get_trade(db, trade_id)
            if value is None:
                raise HTTPException(404, "trade not found")
            return public(value)
        finally:
            db.close()

    @app.patch("/api/trades/{trade_id}/reason")
    def reason(trade_id: str, body: ReasonUpdate):
        db = database()
        try:
            try:
                value = update_reason(db, trade_id, body.reason, body.expected_revision)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if value is None:
                raise HTTPException(404, "trade not found")
            return public(value)
        finally:
            db.close()

    return app


app = create_app(os.environ.get("TRADEBOOK_DB", str(DEFAULT_DB)))
