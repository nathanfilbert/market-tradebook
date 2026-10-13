"""One entry reason per position, shared by every proven exit row of that position.

A closed trade row belongs to a position group only on persisted fill evidence:

- **lifecycle**: the row links exactly to a verified forward-tracked lifecycle (see `lifecycles`).
- **entry_fill**: a legacy (pre-tracking) partial close. Every row of the group cites the same single
  persisted entry fill, scoped to source, account and exact product, and the rows together use no more
  than that fill. This is the entry allocation identity the closes were built from (one lot, split
  into several exits); no flat starting point is assumed and no current position is claimed.

Anything else is ungrouped. A row that looks like a partial close but fails a proof is reported as
`unresolved` with the reason, never grouped by coin, side, size or time.

The shared reason is stored in the existing lifecycle reason tables (`lifecycle_reasons`,
`lifecycle_reason_history`) under the group ID, so revisions, compare-and-set and history are the
same code as open-trade reasons. Each row's own `trades.reason` (written before grouping existed)
is a legacy value: it is never changed here. Until the shared reason is first saved, a group shows
its legacy values; two or more different values are a conflict that only an explicit save resolves,
and that save must name the legacy revisions it was shown. A saved clear is an override, not absence.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

from .lifecycle_reasons import annotation, initialize_reasons, reasons_by_lifecycle, write_reason
from .lifecycles import DERIVATIVE_SOURCES
from .store import canonical_json


def entry_group_id(source_key: str, account_id: str, product_id: str, entry_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{source_key}:{account_id}:{product_id}:entry:{entry_id}"))


@dataclass(frozen=True, slots=True)
class Group:
    """What `update_lifecycle_reason` needs to save a reason (same shape as a Lifecycle)."""
    lifecycle_id: str
    kind: str                    # "lifecycle" | "entry_fill"
    source_key: str
    account_id: str
    product_id: str
    side: str
    opening_entry_id: str
    opened_at: datetime
    members: tuple[str, ...]     # trade IDs, oldest close first


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _positive(value) -> Decimal | None:
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _same_fill(payload, raw: str) -> bool:
    """The close's copy of a fill equals the saved raw fill. The CFM close builder adds only
    `"fee": null` (commission has no documented currency); nothing else may differ."""
    if not isinstance(payload, dict):
        return False
    saved = json.loads(raw)
    copy = dict(payload)
    if "fee" not in saved and copy.get("fee", "absent") is None:
        del copy["fee"]
    return canonical_json(copy) == canonical_json(saved)


def _persisted_fills(db) -> dict:
    marks = ", ".join("?" for _ in DERIVATIVE_SOURCES)
    fills = {}
    for source_key, account_id, event_id, raw in db.execute(
            f"SELECT source_key, account_id, event_id, raw_json FROM source_events WHERE source_key IN ({marks})",
            DERIVATIVE_SOURCES):
        try:
            key = json.loads(event_id)
        except ValueError:
            continue
        if isinstance(key, list) and len(key) == 2 and key[0] == "fills":
            fills[(source_key, account_id, key[1])] = raw
    return fills


def legacy_groups(db, skip=frozenset()):
    """Return (groups by id, unresolved reasons by trade ID) for legacy partial closes.

    `skip` holds trade IDs already in a lifecycle group."""
    fills = _persisted_fills(db)
    rows = db.execute("SELECT id, source_key, account_id, market, position_side, entry_time, entry_price, "
                      "close_time, closed_quantity, packet_json, exit_price FROM trades ORDER BY close_time, id").fetchall()
    cited = defaultdict(list)          # (source, account, fill id) -> [(trade id, position)]
    parsed = {}
    for row in rows:
        trade_id, source_key, account_id = row[0], row[1], row[2]
        try:
            ids = [e["event_id"] for e in json.loads(row[9])["source_events"]]
        except (ValueError, KeyError, TypeError):
            ids = []
        parsed[trade_id] = ids
        for position, event_id in enumerate(ids):
            cited[(source_key, account_id, event_id)].append((trade_id, position, len(ids)))

    proven = defaultdict(list)         # (source, account, product, entry fill) -> member tuples
    failed = defaultdict(list)         # same key -> [(trade id, why)]
    for row in rows:
        (trade_id, source_key, account_id, market, side, entry_time, entry_price,
         close_time, closed_quantity, packet_json, exit_price) = row
        ids = parsed[trade_id]
        if source_key not in DERIVATIVE_SOURCES or trade_id in skip or len(ids) != 2:
            continue                   # unknown-episode closes have no single entry allocation
        entry_id, exit_id = ids
        key = (source_key, account_id, market, entry_id)
        entry_raw = fills.get((source_key, account_id, entry_id))
        exit_raw = fills.get((source_key, account_id, exit_id))
        entry = json.loads(entry_raw) if entry_raw else {}
        exit_fill = json.loads(exit_raw) if exit_raw else {}
        packet = json.loads(packet_json)
        events = packet["source_events"]
        size, closed = _positive(entry.get("size")), _positive(closed_quantity)
        exit_size, exit_px = _positive(exit_fill.get("size")), _positive(exit_fill.get("price"))
        entry_px = _positive(entry.get("price"))
        entry_at, exit_at = _time(entry.get("trade_time")), _time(exit_fill.get("trade_time"))
        why = None
        if not entry_raw or not exit_raw:
            why = "an entry or exit fill of this close is not saved"
        elif (packet.get("market") != market or packet.get("position_side") != side
              or _decimal(packet.get("closed_quantity")) != _decimal(closed_quantity)
              or _decimal(packet.get("entry_price")) != _decimal(entry_price)
              or _decimal(packet.get("exit_price")) != _decimal(exit_price)
              or _time(packet.get("entry_time")) != _time(entry_time) or _time(packet.get("close_time")) != _time(close_time)):
            why = "the trade row and its own close evidence disagree"
        elif not (_same_fill(events[0].get("payload"), entry_raw) and _same_fill(events[1].get("payload"), exit_raw)):
            why = "the close cites fill data that differs from the saved fill"
        elif (entry.get("trade_type") != "FILL" or exit_fill.get("trade_type") != "FILL"
              or entry.get("product_id") != market or exit_fill.get("product_id") != market
              or entry.get("retail_portfolio_id") != account_id or exit_fill.get("retail_portfolio_id") != account_id
              or entry.get("size_in_quote") is not False or exit_fill.get("size_in_quote") is not False):
            why = "the fills are not contract fills of this account and exact product"
        elif ({"long": "BUY", "short": "SELL"}.get(side) != entry.get("side")
              or exit_fill.get("side") != {"BUY": "SELL", "SELL": "BUY"}.get(entry.get("side"))):
            why = "fill directions do not match the close"
        elif None in (size, closed, exit_size, exit_px, entry_px, entry_at, exit_at):
            why = "a fill or the close has a missing, non-positive or unreadable size, price or time"
        elif (entry_at != _time(entry_time) or entry_px != _decimal(entry_price)
              or exit_at != _time(close_time) or exit_px != _decimal(exit_price)):
            why = "entry or exit time or price does not match the saved fills"
        elif not entry_at < exit_at:
            why = "the exit fill is not after the entry fill"
        elif closed != exit_size:
            # The exit fill must be wholly this close; split or reversing exits are not proven allocations.
            why = "closed quantity is not exactly the exit fill's size"
        elif closed > size:
            why = "closed quantity does not fit the entry fill"
        elif any(p != 0 or n != 2 for _, p, n in cited[(source_key, account_id, entry_id)]):
            why = "the entry fill is also cited another way by a different close"
        elif len(cited[(source_key, account_id, exit_id)]) != 1:
            why = "the exit fill is cited by more than one close"
        if why:
            failed[key].append((trade_id, why))
        else:
            proven[key].append((trade_id, side, _time(entry_time), closed, size, _decimal(entry_price)))

    groups, unresolved = {}, {}
    for key in set(proven) | set(failed):
        source_key, account_id, market, entry_id = key
        members, bad = proven.get(key, []), failed.get(key, [])
        sharing = len(cited[(source_key, account_id, entry_id)]) > 1
        if bad:
            # Report only what could be a partial close; a lone row stays an ordinary row.
            if sharing or any(m[3] < m[4] for m in members):
                for trade_id, why in bad:
                    unresolved[trade_id] = why
                for m in members:
                    unresolved[m[0]] = "another close of the same entry fill failed its proof"
            continue
        total = sum((m[3] for m in members), Decimal(0))
        if total > members[0][4] or len({(m[1], m[2], m[5]) for m in members}) != 1:
            for m in members:
                unresolved[m[0]] = "closes of this entry fill use more than it, or disagree about it"
            continue
        if len(members) == 1 and total == members[0][4]:
            continue                   # one row closing the whole entry fill: nothing to share
        group_id = entry_group_id(source_key, account_id, market, entry_id)
        groups[group_id] = Group(group_id, "entry_fill", source_key, account_id, market, members[0][1],
                                 entry_id, members[0][2], tuple(m[0] for m in members))
    return groups, unresolved


def lifecycle_groups(projection) -> dict:
    members = defaultdict(list)
    for trade_id, link in sorted(projection.links.items()):
        if "lifecycle_id" in link:
            members[link["lifecycle_id"]].append(trade_id)
    groups = {}
    for lc_id, ids in members.items():
        lc = projection.lifecycles[lc_id]
        if lc.status == "unresolved":
            continue
        groups[lc_id] = Group(lc_id, "lifecycle", lc.source_key, lc.account_id, lc.product_id, lc.side,
                              lc.opening_entry_id, lc.opened_at, tuple(ids))
    return groups


def all_groups(db, projection):
    """(groups by id, group id by trade id, unresolved reasons by trade id). Lifecycle links win."""
    groups = lifecycle_groups(projection)
    linked = {t for g in groups.values() for t in g.members}
    legacy, unresolved = legacy_groups(db, skip=linked)
    groups.update(legacy)
    by_trade = {t: g.lifecycle_id for g in groups.values() for t in g.members}
    return groups, by_trade, unresolved


def _legacy(db, group: Group) -> list[dict]:
    marks = ", ".join("?" for _ in group.members)
    rows = {r[0]: r for r in db.execute(
        f"SELECT id, reason, reason_revision, close_time FROM trades WHERE id IN ({marks})", group.members)}
    return [{"trade_id": t, "reason": rows[t][1], "reason_revision": rows[t][2], "close_time": rows[t][3]}
            for t in group.members if t in rows]


def view(db, group: Group, saved: dict | None) -> dict:
    """The shared reason as a row shows it. `state`: set, cleared, legacy (one value), conflict, none."""
    legacy = _legacy(db, group)
    values = sorted({r["reason"] for r in legacy if r["reason"] is not None})
    shared = annotation(saved)
    if shared["revision"]:
        state, reason = shared["state"], shared["reason"]
    elif len(values) > 1:
        state, reason = "conflict", None
    elif values:
        state, reason = "legacy", values[0]
    else:
        state, reason = "none", None
    return {"group_id": group.lifecycle_id, "kind": group.kind, "product_id": group.product_id,
            "side": group.side, "opening_entry_id": group.opening_entry_id, "members": list(group.members),
            "state": state, "reason": reason, "revision": shared["revision"], "updated_at": shared["updated_at"],
            "legacy": legacy}


def views_by_trade(db, projection) -> dict:
    groups, by_trade, unresolved = all_groups(db, projection)
    saved = reasons_by_lifecycle(db)
    shown = {gid: view(db, g, saved.get(gid)) for gid, g in groups.items()}
    out = {t: shown[gid] for t, gid in by_trade.items()}
    out.update({t: {"unresolved": why} for t, why in unresolved.items()})
    return out


@contextmanager
def reserved(db):
    """Hold SQLite's write reservation from the first read to the commit, so group membership, the legacy
    per-exit revisions and the shared reason are checked and written as one state. Another writer (a legacy
    reason edit, a sync, another shared save) waits or fails; it cannot slip in between."""
    if db.in_transaction:
        raise RuntimeError("a transaction is already open on this connection")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


class Unattributable(ValueError):
    """The lifecycle exists but cannot take a reason."""


def _check_legacy(db, group: Group, expected_revision: int, legacy_seen) -> None:
    if expected_revision != 0:
        return
    current = {(r["trade_id"], r["reason_revision"]) for r in _legacy(db, group) if r["reason"] is not None}
    seen = {(item["trade_id"], item["reason_revision"]) for item in (legacy_seen or [])}
    if current != seen:
        raise ValueError("legacy reasons changed or were not reviewed; reload and choose again")


def save(db, projector, group_id: str, reason, expected_revision: int, legacy_seen) -> dict:
    """Save the shared reason. Membership, the legacy check and the write happen under one reservation.
    The first save of a group with legacy values must name exactly the legacy (trade, revision) pairs
    the person was shown, so a legacy edit in between is a conflict. `projector(db)` recomputes lifecycles."""
    initialize_reasons(db)                   # its executescript commits, so it runs before the reservation
    with reserved(db):
        groups, _, _ = all_groups(db, projector(db))
        group = groups.get(group_id)
        if group is None:
            raise LookupError("position group not found")
        _check_legacy(db, group, expected_revision, legacy_seen)
        write_reason(db, group, reason, expected_revision)
    return view(db, group, reasons_by_lifecycle(db).get(group_id))


def save_lifecycle(db, projector, lifecycle_id: str, reason, expected_revision: int):
    """The open-trade route's save, under the same reservation. Its first save is refused when the
    lifecycle's exits already carry their own reasons (those are resolved through `save`)."""
    initialize_reasons(db)
    with reserved(db):
        projection = projector(db)
        lc = projection.lifecycles.get(lifecycle_id)
        if lc is None:
            raise LookupError("lifecycle not found")
        if lc.status == "unresolved":
            raise Unattributable("lifecycle cannot be attributed: " + (lc.unresolved_reason or "unresolved"))
        group = lifecycle_groups(projection).get(lifecycle_id)
        if expected_revision == 0 and group and any(r["reason"] is not None for r in _legacy(db, group)):
            raise Unattributable("this position's exits already have their own reasons; "
                                 "choose the position reason in the Tradebook")
        write_reason(db, lc, reason, expected_revision)
    return projection, lc
