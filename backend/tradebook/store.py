# backend/tradebook/store.py
import sqlite3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from .domain import ClosedTradePacket
from .parse import project

COLUMNS = ("id", "source_key", "account_id", "close_id", "position_id", "product_type", "market",
    "position_side", "entry_time", "close_time", "closed_quantity", "quantity_unit", "entry_price",
    "exit_price", "contract_multiplier", "price_currency", "position_notional_usd",
    "gross_pnl_usd", "fee_usd", "funding_usd", "net_pnl_usd", "reported_gross_usd",
    "reported_net_usd", "reconciliation_status", "basis_status", "basis_currency")


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(db: sqlite3.Connection) -> None:
    with db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS source_events (
          source_key TEXT NOT NULL, account_id TEXT NOT NULL, event_id TEXT NOT NULL,
          raw_json TEXT NOT NULL, PRIMARY KEY(source_key, account_id, event_id));
        CREATE TABLE IF NOT EXISTS source_checkpoints (
          source_key TEXT NOT NULL, account_id TEXT NOT NULL, stream TEXT NOT NULL,
          cursor TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(source_key, account_id, stream));
        CREATE TABLE IF NOT EXISTS trades (
          id TEXT PRIMARY KEY, source_key TEXT NOT NULL, account_id TEXT NOT NULL,
          close_id TEXT NOT NULL, position_id TEXT, product_type TEXT NOT NULL,
          market TEXT NOT NULL, position_side TEXT NOT NULL, entry_time TEXT,
          close_time TEXT NOT NULL,
          closed_quantity TEXT NOT NULL, quantity_unit TEXT NOT NULL,
          entry_price TEXT, exit_price TEXT,
          contract_multiplier TEXT, price_currency TEXT NOT NULL,
          position_notional_usd TEXT, gross_pnl_usd TEXT, fee_usd TEXT,
          funding_usd TEXT, net_pnl_usd TEXT, reported_gross_usd TEXT,
          reported_net_usd TEXT, reconciliation_status TEXT NOT NULL,
          basis_status TEXT, basis_currency TEXT,
          packet_json TEXT NOT NULL, packet_hash TEXT NOT NULL,
          reason TEXT, reason_revision INTEGER NOT NULL DEFAULT 0,
          UNIQUE(source_key, account_id, close_id));
        CREATE INDEX IF NOT EXISTS trades_newest ON trades(close_time DESC, id DESC);
        CREATE TABLE IF NOT EXISTS reason_history (
          trade_id TEXT NOT NULL REFERENCES trades(id), revision INTEGER NOT NULL,
          reason TEXT, edited_at TEXT NOT NULL, PRIMARY KEY(trade_id, revision));
        """)
        existing = {column[1] for column in db.execute("PRAGMA table_info(trades)")}
        for column in ("basis_status", "basis_currency"):
            if column not in existing:
                db.execute(f"ALTER TABLE trades ADD COLUMN {column} TEXT")


_UNSET_CURSOR = object()


def ingest_source_page(db: sqlite3.Connection, source_key: str, account_id: str,
                       stream: str, events, cursor: str | None, *,
                       expected_cursor=_UNSET_CURSOR, same_page_replay: bool = False) -> int:
    """Stage one stream page with a cursor compare-and-swap (opaque cursors are never ordered)."""
    if expected_cursor is _UNSET_CURSOR:
        raise ValueError("expected_cursor is required")
    with db:
        checkpoint = db.execute(
            "SELECT cursor FROM source_checkpoints WHERE source_key=? AND account_id=? AND stream=?",
            (source_key, account_id, stream),
        ).fetchone()
        actual = checkpoint["cursor"] if checkpoint else None
        if actual != expected_cursor and not (same_page_replay and actual == cursor):
            raise ValueError("stale source page: checkpoint changed")
        for event in events:
            value = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
            if not isinstance(value, dict) or not isinstance(value.get("event_id"), str) or not value["event_id"]:
                raise ValueError("source event requires a non-empty event_id")
            raw_event_id = value["event_id"]
            # Encode the pair without changing schema; opaque source IDs are stream-local.
            event_id = json.dumps([stream, raw_event_id], separators=(",", ":"))
            raw = canonical_json(value)
            old = db.execute(
                "SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                (source_key, account_id, event_id),
            ).fetchone()
            if old and old["raw_json"] != raw:
                raise ValueError("source event changed; investigate source drift")
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                       (source_key, account_id, event_id, raw))
        db.execute(
            "INSERT INTO source_checkpoints (source_key, account_id, stream, cursor, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_key, account_id, stream) DO UPDATE SET "
            "cursor=excluded.cursor, updated_at=excluded.updated_at",
            (source_key, account_id, stream, cursor, datetime.now(timezone.utc).isoformat()),
        )
    return len(events)


def get_trade(db: sqlite3.Connection, trade_id: str) -> dict | None:
    row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return dict(row) if row else None


def list_trades(db: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM trades ORDER BY close_time DESC, id DESC LIMIT ? OFFSET ?", (limit, offset))]

def canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def upsert_packet(db: sqlite3.Connection, p: ClosedTradePacket) -> str:
    data = project(p)
    packet_json = canonical_json(p.model_dump(mode="json"))
    digest = hashlib.sha256(packet_json.encode()).hexdigest()
    from contextlib import nullcontext
    transaction = db if not db.in_transaction else nullcontext()
    with transaction:
        for event in p.source_events:
            raw = canonical_json(event.model_dump(mode="json"))
            old = db.execute("SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                (p.source_key, p.account_id, event.event_id)).fetchone()
            if old and old["raw_json"] != raw:
                raise ValueError("source event changed; investigate source drift")
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                (p.source_key, p.account_id, event.event_id, raw))
        cols = ", ".join(COLUMNS)
        marks = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{column}=excluded.{column}" for column in COLUMNS if column != "id")
        db.execute(f"INSERT INTO trades ({cols}, packet_json, packet_hash) VALUES ({marks}, ?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}, packet_json=excluded.packet_json, packet_hash=excluded.packet_hash",
            tuple(data[c] for c in COLUMNS) + (packet_json, digest))
    return data["id"]


def upsert_packets_and_events(db: sqlite3.Connection, packets, *, source_key: str,
                              account_id: str, fills=(), wallet_events=(),
                              wallet_account_id: str | None = None) -> int:
    """Atomically stage raw fill history and project every close in the batch."""
    db.execute("SAVEPOINT tradebook_batch")
    try:
        for fill in fills:
            if not isinstance(fill, dict) or not isinstance(fill.get("entry_id"), str) or not fill["entry_id"]:
                raise ValueError("fill requires a non-empty entry_id")
            raw = canonical_json(fill)
            event_id = json.dumps(["fills", fill["entry_id"]], separators=(",", ":"))
            old = db.execute("SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                             (source_key, account_id, event_id)).fetchone()
            if old and old["raw_json"] != raw:
                raise ValueError("source event changed; investigate source drift")
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                       (source_key, account_id, event_id, raw))
        for event in wallet_events:
            if (not isinstance(wallet_account_id, str) or not wallet_account_id or
                not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]):
                raise ValueError("wallet source event requires account and ID")
            event_id = json.dumps(["wallet", wallet_account_id, event["id"]], separators=(",", ":"))
            raw = canonical_json(event)
            old = db.execute("SELECT raw_json FROM source_events WHERE source_key=? AND account_id=? AND event_id=?",
                             (source_key, account_id, event_id)).fetchone()
            if old and old["raw_json"] != raw:
                raise ValueError("source event changed; investigate source drift")
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                       (source_key, account_id, event_id, raw))
        for packet in packets:
            if packet.source_key != source_key or packet.account_id != account_id:
                raise ValueError("packet account/source mismatch")
            upsert_packet(db, packet)
        db.execute("RELEASE SAVEPOINT tradebook_batch")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT tradebook_batch")
        db.execute("RELEASE SAVEPOINT tradebook_batch")
        raise
    return len(packets)



def update_reason(db: sqlite3.Connection, trade_id: str, reason: str | None,
                  expected_revision: int) -> dict | None:
    if reason is not None and len(reason) > 4000:
        raise ValueError("reason too long")
    with db:
        current = get_trade(db, trade_id)
        if current is None:
            return None
        if current["reason_revision"] != expected_revision:
            raise ValueError("revision conflict")
        revision = expected_revision + 1
        db.execute("UPDATE trades SET reason=?, reason_revision=? WHERE id=?",
            (reason, revision, trade_id))
        db.execute("INSERT INTO reason_history VALUES (?, ?, ?, ?)",
            (trade_id, revision, reason, datetime.now(timezone.utc).isoformat()))
    return get_trade(db, trade_id)

def backup(db: sqlite3.Connection, target: Path) -> None:
    with sqlite3.connect(target) as destination:
        db.backup(destination)
