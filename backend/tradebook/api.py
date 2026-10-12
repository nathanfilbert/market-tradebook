# backend/tradebook/api.py
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .lifecycle_reasons import annotation, detached, reasons_by_lifecycle, update_lifecycle_reason
from .lifecycles import project
from .positions import latest_snapshots
from .recurring_sync import read_sync_status
from .store import connect, initialize, get_trade, list_trades, update_reason, update_notes
from .sync import DEFAULT_DB

# Open state is only as fresh as the last completed reads (timer runs every ~5 minutes).
STALE_AFTER = timedelta(minutes=15)


class ReasonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(max_length=4000)
    expected_revision: int = Field(ge=0)


class NotesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str | None = Field(max_length=4000)
    expected_revision: int = Field(ge=0)


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in {"packet_json", "packet_hash"}}

def freshness(status: dict, key: str, now: datetime) -> dict:
    entry = status.get(key)
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


def display_state(lc, scope: dict) -> str:
    """confirmed: a fresh Coinbase position check agrees after the last fill;
    pending_check: fresh reads, but a fill is newer than the last check; otherwise stale."""
    if lc.status != "open":
        return lc.status
    if scope["freshness"]["state"] != "fresh" or scope["positions"]["state"] != "fresh":
        return "stale"
    last_fill = datetime.fromisoformat(lc.fills[-1]["trade_time"])
    checked = scope["last_check_at"]
    return "confirmed" if checked and datetime.fromisoformat(checked) >= last_fill else "pending_check"


def create_app(db_path: str | Path, checks=None, clock=lambda: datetime.now(timezone.utc)) -> FastAPI:
    """`checks=None` reads saved positions snapshots; tests may inject explicit checks."""
    app = FastAPI(title="Market Tradebook")
    path = Path(db_path)

    def lifecycle_view(db):
        now = clock()
        projection = project(db, checks, now=now)
        status, snapshots = read_sync_status(path), latest_snapshots(db)
        scopes = {}
        for scope in projection.scopes:
            taken = snapshots.get(scope.account_id)
            failed = (status.get(f"{scope.account_id}|positions") or {}).get("last_attempt_ok") is False
            state = ("unavailable" if taken is None else
                     "stale" if now - taken > STALE_AFTER or failed else "fresh")
            positions = {"state": state, "last_sync_at": taken.isoformat() if taken else None}
            scopes[(scope.source_key, scope.account_id, scope.product_id)] = {
                **scope.public(), "freshness": freshness(status, f"{scope.account_id}|{scope.product_id}", now),
                "positions": positions}
        return projection, scopes, reasons_by_lifecycle(db)

    def lifecycle_item(lc, scopes, reasons) -> dict:
        scope = scopes[(lc.source_key, lc.account_id, lc.product_id)]
        return {**lc.public(), "scope_status": scope["status"], "freshness": scope["freshness"],
                "positions": scope["positions"], "display": display_state(lc, scope),
                "entry_reason": annotation(reasons.get(lc.lifecycle_id))}

    def with_lifecycle(row: dict, projection, reasons) -> dict:
        link = projection.links.get(row["id"])
        if link and "lifecycle_id" in link:
            link = {**link, "entry_reason": annotation(reasons.get(link["lifecycle_id"]))}
        return {**public(row), "lifecycle": link}

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
            projection = project(db, checks, now=clock())
            reasons = reasons_by_lifecycle(db)
            return {"items": [with_lifecycle(r, projection, reasons) for r in rows[:limit]],
                    "has_more": len(rows) > limit}
        finally:
            db.close()

    @app.get("/api/trades/{trade_id}")
    def trade(trade_id: str):
        db = database()
        try:
            value = get_trade(db, trade_id)
            if value is None:
                raise HTTPException(404, "trade not found")
            return with_lifecycle(value, project(db, checks, now=clock()), reasons_by_lifecycle(db))
        finally:
            db.close()

    @app.get("/api/lifecycles")
    def lifecycles(status: str | None = Query(default=None, pattern="^(open|closed|unresolved)$")):
        db = database()
        try:
            projection, scopes, reasons = lifecycle_view(db)
            linked = {}
            for trade_id, link in sorted(projection.links.items()):
                if "lifecycle_id" in link:
                    linked.setdefault(link["lifecycle_id"], []).append(trade_id)
            items = [{**lifecycle_item(lc, scopes, reasons), "linked_trade_ids": linked.get(lc.lifecycle_id, [])} for lc in
                     sorted(projection.lifecycles.values(), key=lambda lc: (lc.opened_at, lc.lifecycle_id), reverse=True)
                     if status is None or lc.status == status]
            return {"items": items, "scopes": list(scopes.values()),
                    "detached_reasons": detached(db, projection.lifecycles)}
        finally:
            db.close()

    @app.get("/api/lifecycles/{lifecycle_id}")
    def lifecycle(lifecycle_id: str):
        db = database()
        try:
            projection, scopes, reasons = lifecycle_view(db)
            lc = projection.lifecycles.get(lifecycle_id)
            if lc is None:
                raise HTTPException(404, "lifecycle not found")
            closes = sorted(trade_id for trade_id, link in projection.links.items()
                            if link.get("lifecycle_id") == lifecycle_id)
            return {**lifecycle_item(lc, scopes, reasons), "linked_trade_ids": closes}
        finally:
            db.close()

    @app.patch("/api/lifecycles/{lifecycle_id}/reason")
    def lifecycle_reason(lifecycle_id: str, body: ReasonUpdate):
        db = database()
        try:
            projection, scopes, _ = lifecycle_view(db)
            lc = projection.lifecycles.get(lifecycle_id)
            if lc is None:
                raise HTTPException(404, "lifecycle not found")
            if lc.status == "unresolved":
                raise HTTPException(409, "lifecycle cannot be attributed: " + (lc.unresolved_reason or "unresolved"))
            try:
                update_lifecycle_reason(db, lc, body.reason, body.expected_revision)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return lifecycle_item(lc, scopes, reasons_by_lifecycle(db))
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

    @app.patch("/api/trades/{trade_id}/notes")
    def notes(trade_id: str, body: NotesUpdate):
        db = database()
        try:
            try:
                value = update_notes(db, trade_id, body.notes, body.expected_revision)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if value is None:
                raise HTTPException(404, "trade not found")
            return public(value)
        finally:
            db.close()

    return app


app = create_app(os.environ.get("TRADEBOOK_DB", str(DEFAULT_DB)))
