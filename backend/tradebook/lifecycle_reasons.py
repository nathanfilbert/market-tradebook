"""Revisioned entry reasons keyed by lifecycle ID; the only user data lifecycles carry.

Reasons are never moved, copied into closed trades or deleted by a replay. A reason
whose lifecycle no longer appears (earlier history arrived, a check disagreed) is
reported as detached for manual review. A deliberately cleared reason is still an
edit: its state comes from the revision, not from whether text is present.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MAX_REASON = 4000


def initialize_reasons(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS lifecycle_reasons (
      lifecycle_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, account_id TEXT NOT NULL,
      product_id TEXT NOT NULL, side TEXT NOT NULL, opening_entry_id TEXT NOT NULL,
      opened_at TEXT NOT NULL, reason TEXT, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS lifecycle_reason_history (
      lifecycle_id TEXT NOT NULL REFERENCES lifecycle_reasons(lifecycle_id), revision INTEGER NOT NULL,
      reason TEXT, edited_at TEXT NOT NULL, PRIMARY KEY(lifecycle_id, revision));
    """)


def _rows(db) -> list[dict]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "lifecycle_reasons" not in tables:
        return []
    cursor = db.execute("SELECT * FROM lifecycle_reasons ORDER BY lifecycle_id")
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def reasons_by_lifecycle(db) -> dict:
    return {row["lifecycle_id"]: row for row in _rows(db)}


def annotation(row: dict | None) -> dict:
    if row is None:
        return {"state": "none", "reason": None, "revision": 0, "updated_at": None}
    state = "set" if row["reason"] is not None else "cleared"
    return {"state": state, "reason": row["reason"], "revision": row["revision"], "updated_at": row["updated_at"]}


def detached(db, lifecycle_ids) -> list[dict]:
    present = set(lifecycle_ids)
    return [{**row, "state": annotation(row)["state"]} for row in _rows(db) if row["lifecycle_id"] not in present]


def update_lifecycle_reason(db, lifecycle, reason: str | None, expected_revision: int) -> dict:
    """Compare-and-set on the revision; 0 means no reason has ever been saved."""
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be text or null")
    reason = (reason.strip() or None) if reason is not None else None   # blank is a deliberate clear
    if reason is not None and len(reason) > MAX_REASON:
        raise ValueError("reason too long")
    now = datetime.now(timezone.utc).isoformat()
    initialize_reasons(db)
    with db:
        row = db.execute("SELECT revision FROM lifecycle_reasons WHERE lifecycle_id=?",
                         (lifecycle.lifecycle_id,)).fetchone()
        current = row[0] if row else 0
        if current != expected_revision:
            raise ValueError("revision conflict")
        revision = current + 1
        if row is None:
            db.execute("INSERT OR IGNORE INTO lifecycle_reasons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (lifecycle.lifecycle_id, lifecycle.source_key, lifecycle.account_id, lifecycle.product_id,
                        lifecycle.side, lifecycle.opening_entry_id, lifecycle.opened_at.isoformat(),
                        reason, revision, now))
        else:
            db.execute("UPDATE lifecycle_reasons SET reason=?, revision=?, updated_at=? "
                       "WHERE lifecycle_id=? AND revision=?",
                       (reason, revision, now, lifecycle.lifecycle_id, current))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("revision conflict")  # another writer won the race
        db.execute("INSERT INTO lifecycle_reason_history VALUES (?, ?, ?, ?)",
                   (lifecycle.lifecycle_id, revision, reason, now))
    return reasons_by_lifecycle(db)[lifecycle.lifecycle_id]
