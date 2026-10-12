# backend/tradebook/api.py
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .lifecycles import project
from .recurring_sync import read_sync_status
from .store import connect, initialize, get_trade, list_trades, update_reason
from .sync import DEFAULT_DB

# Open state is only as fresh as the last completed fills read (timer runs every ~5 minutes).
STALE_AFTER = timedelta(minutes=15)
# No evidence source proves a flat starting point yet (design r2, D3), so every scope is
# reported unavailable rather than inferred from a replay that happens to reach zero.
FLAT_BASELINES = ()


class ReasonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(max_length=4000)
    expected_revision: int = Field(ge=0)


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in {"packet_json", "packet_hash"}}

def freshness(status: dict, account_id: str, product_id: str, now: datetime) -> dict:
    entry = status.get(f"{account_id}|{product_id}")
    if not isinstance(entry, dict) or not isinstance(entry.get("last_ok_at"), str):
        return {"state": "unavailable", "last_sync_at": None}
    try:
        last = datetime.fromisoformat(entry["last_ok_at"])
    except ValueError:
        return {"state": "unavailable", "last_sync_at": None}
    if last.tzinfo is None:
        return {"state": "unavailable", "last_sync_at": None}
    stale = now - last > STALE_AFTER or entry.get("last_attempt_ok") is False
    return {"state": "stale" if stale else "fresh", "last_sync_at": last.isoformat()}


def create_app(db_path: str | Path, baselines=FLAT_BASELINES, clock=lambda: datetime.now(timezone.utc)) -> FastAPI:
    app = FastAPI(title="Market Tradebook")
    path = Path(db_path)

    def lifecycle_view(db):
        projection = project(db, baselines)
        status, now = read_sync_status(path), clock()
        scopes = {}
        for scope in projection.scopes:
            scopes[(scope.source_key, scope.account_id, scope.product_id)] = {
                **scope.public(), "freshness": freshness(status, scope.account_id, scope.product_id, now)}
        return projection, scopes

    def with_lifecycle(row: dict, projection) -> dict:
        return {**public(row), "lifecycle": projection.links.get(row["id"])}

    def database():
        db = connect(path); initialize(db)
        return db

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/trades")
    def trades(offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100),
               recent: bool = False):
        db = database()
        try:
            since = datetime.now(timezone.utc) - timedelta(days=30) if recent else None
            rows = list_trades(db, limit + 1, offset, since=since)
            projection = project(db, baselines)
            return {"items": [with_lifecycle(r, projection) for r in rows[:limit]], "has_more": len(rows) > limit}
        finally:
            db.close()

    @app.get("/api/trades/{trade_id}")
    def trade(trade_id: str):
        db = database()
        try:
            value = get_trade(db, trade_id)
            if value is None:
                raise HTTPException(404, "trade not found")
            return with_lifecycle(value, project(db, baselines))
        finally:
            db.close()

    @app.get("/api/lifecycles")
    def lifecycles(status: str | None = Query(default=None, pattern="^(open|closed|unresolved)$")):
        db = database()
        try:
            projection, scopes = lifecycle_view(db)
            items = []
            for lc in sorted(projection.lifecycles.values(), key=lambda lc: (lc.opened_at, lc.lifecycle_id), reverse=True):
                if status is None or lc.status == status:
                    scope = scopes[(lc.source_key, lc.account_id, lc.product_id)]
                    items.append({**lc.public(), "scope_status": scope["status"], "freshness": scope["freshness"]})
            return {"items": items, "scopes": list(scopes.values())}
        finally:
            db.close()

    @app.get("/api/lifecycles/{lifecycle_id}")
    def lifecycle(lifecycle_id: str):
        db = database()
        try:
            projection, scopes = lifecycle_view(db)
            lc = projection.lifecycles.get(lifecycle_id)
            if lc is None:
                raise HTTPException(404, "lifecycle not found")
            scope = scopes[(lc.source_key, lc.account_id, lc.product_id)]
            closes = sorted(trade_id for trade_id, link in projection.links.items()
                            if link.get("lifecycle_id") == lifecycle_id)
            return {**lc.public(), "scope_status": scope["status"], "freshness": scope["freshness"],
                    "linked_trade_ids": closes}
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
