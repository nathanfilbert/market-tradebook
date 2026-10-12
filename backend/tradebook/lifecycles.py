"""Read-only position lifecycle projection over saved Coinbase CFM fills.

A lifecycle is one position in one exact product, from the fill that opens it from a
proven flat point to the fill that brings it back to flat. It is computed on read from
`source_events` and position checks, and never stored, so replays are identical.

Fail-closed rules (design r2 with Nathan's D3 correction and the positions audit, 2026-10-12):
- A scope is (source_key, account_id, exact product_id). Scopes are never mixed.
- Replay starts only at a flat position check: a bracketed Coinbase positions snapshot
  taken after forward tracking was enabled. A calculated zero from an unknown start is
  never a flat point, so without one the scope is `unavailable`.
- Every later check must agree with the replayed position. Disagreement, non-FILL
  types, account or field mismatches, a close with nothing open, same-time fills in
  both directions, or an expired contract make the scope `unresolved` until the next
  flat check.
- Links to closed trades are exact fill-role matches only, never by coin, side, size
  or time, and linked quantities may not exceed what the fills closed.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

REPLAY_VERSION = 1
DERIVATIVE_SOURCES = ("coinbase.cfm.us_perpetual", "coinbase.cfm.dated_future")
OPENING_ROLES = ("open", "add", "residual_open")


@dataclass(frozen=True, slots=True)
class PositionCheck:
    """Coinbase-reported position for one scope immediately after `as_of`.

    `quantity` is signed (long positive). A zero check is a flat point that may start a
    replay; a non-zero check only verifies one. Fills at or before `as_of` are before it.
    Never build one from a replayed total.
    """
    source_key: str
    account_id: str
    product_id: str
    as_of: datetime
    evidence: str
    quantity: Decimal = Decimal(0)


FlatBaseline = PositionCheck


@dataclass(slots=True)
class Lifecycle:
    lifecycle_id: str
    source_key: str
    account_id: str
    product_id: str
    side: str
    opening_entry_id: str
    opened_at: datetime
    status: str = "open"
    open_quantity: Decimal = Decimal(0)
    closed_at: datetime | None = None
    unresolved_reason: str | None = None
    fills: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        opening = [f for f in self.fills if f["role"] in OPENING_ROLES]
        opened_qty = sum((Decimal(f["quantity"]) for f in opening), Decimal(0))
        entry = (sum((Decimal(f["quantity"]) * Decimal(f["price"]) for f in opening), Decimal(0)) / opened_qty
                 if opened_qty else None)
        return {
            "lifecycle_id": self.lifecycle_id, "source_key": self.source_key,
            "account_id": self.account_id, "product_id": self.product_id, "side": self.side,
            "status": self.status, "opening_entry_id": self.opening_entry_id,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "open_quantity": _text(self.open_quantity), "opened_quantity": _text(opened_qty),
            "weighted_entry_price": _text(entry) if entry is not None else None,
            "adds": sum(1 for f in self.fills if f["role"] == "add"),
            "closes": sum(1 for f in self.fills if f["role"] == "close"),
            "last_fill_entry_id": self.fills[-1]["entry_id"],
            "unresolved_reason": self.unresolved_reason,
            "fills": [dict(f) for f in self.fills], "replay_version": REPLAY_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ScopeProjection:
    source_key: str
    account_id: str
    product_id: str
    status: str  # "verified" | "unresolved" | "unavailable"
    reason: str | None
    baseline: PositionCheck | None
    lifecycles: tuple[Lifecycle, ...]
    last_check_at: datetime | None = None  # latest check that agreed with the replay
    last_fill_at: datetime | None = None

    def public(self) -> dict:
        return {"source_key": self.source_key, "account_id": self.account_id,
                "product_id": self.product_id, "status": self.status, "reason": self.reason,
                "baseline": None if self.baseline is None else {
                    "as_of": self.baseline.as_of.isoformat(), "evidence": self.baseline.evidence},
                "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
                "last_fill_at": self.last_fill_at.isoformat() if self.last_fill_at else None,
                "replay_version": REPLAY_VERSION}


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def lifecycle_id(source_key: str, account_id: str, product_id: str, entry_id: str, *, residual: bool = False) -> str:
    name = f"{source_key}:{account_id}:{product_id}:lifecycle:{entry_id}" + (":residual" if residual else "")
    return str(uuid5(NAMESPACE_URL, name))


def _aware(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing trade_time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("trade_time must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _positive(value, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"invalid {name}") from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f"invalid {name}")
    return result


def _fee(fill: dict) -> Decimal | None:
    value = fill.get("commission")
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _checked(fill, account_id: str, product_id: str) -> dict:
    """Return a normalized fill or raise ValueError naming why it cannot be replayed."""
    if not isinstance(fill, dict) or not isinstance(fill.get("entry_id"), str) or not fill["entry_id"]:
        raise ValueError("fill without entry_id")
    entry_id = fill["entry_id"]
    if fill.get("trade_type") != "FILL":
        raise ValueError(f"{entry_id}: non-fill event {fill.get('trade_type')!r}")
    if fill.get("retail_portfolio_id") != account_id:
        raise ValueError(f"{entry_id}: account mismatch")
    if fill.get("product_id") != product_id:
        raise ValueError(f"{entry_id}: product mismatch")
    if fill.get("side") not in ("BUY", "SELL"):
        raise ValueError(f"{entry_id}: unknown direction")
    if fill.get("size_in_quote") is not False:
        raise ValueError(f"{entry_id}: size is not in contracts")
    if fill.get("position_effect") not in (None, "OPEN", "CLOSE"):
        raise ValueError(f"{entry_id}: unsupported position effect")
    return {"entry_id": entry_id, "time": _aware(fill.get("trade_time")),
            "sign": 1 if fill["side"] == "BUY" else -1, "quantity": _positive(fill.get("size"), "size"),
            "price": _positive(fill.get("price"), "price"), "fee": _fee(fill),
            "position_effect": fill.get("position_effect")}


def replay_scope(fills, checks=(), *, source_key: str, account_id: str, product_id: str) -> ScopeProjection:
    """Replay one exact scope. Order is (trade_time, entry_id); input order never matters."""
    def scope(status, reason, baseline, lifecycles=(), last_check=None, last_fill=None):
        return ScopeProjection(source_key, account_id, product_id, status, reason, baseline, tuple(lifecycles),
                               last_check, last_fill)

    usable = sorted((c for c in checks if (c.source_key, c.account_id, c.product_id) ==
                     (source_key, account_id, product_id)), key=lambda c: (c.as_of, c.quantity != 0))
    if not any(c.quantity == 0 for c in usable):
        return scope("unavailable", "no proven flat starting point for this product", None)
    rows = list(fills)
    try:
        ordered = sorted(rows, key=lambda f: (_aware(f.get("trade_time")), f.get("entry_id", "")))
    except (ValueError, AttributeError, TypeError) as exc:
        return scope("unresolved", f"unreadable fill time: {exc}", None)
    ids = [f.get("entry_id") for f in ordered]
    if len(ids) != len(set(ids)):
        return scope("unresolved", "duplicate fill entry_id", None)
    by_time = defaultdict(set)
    for f in ordered:
        by_time[_aware(f["trade_time"])].add(f.get("side"))

    lifecycles: list[Lifecycle] = []
    current: Lifecycle | None = None
    position = Decimal(0)
    status, reason, baseline = "unavailable", "no proven flat starting point for this product", None
    last_check = last_fill = None
    pending = list(usable)

    def fail(why: str):
        nonlocal current, status, reason, position
        if current is not None and current.status == "open":
            current.status, current.unresolved_reason = "unresolved", why
        current, position, status, reason = None, Decimal(0), "unresolved", why

    def apply(mark: PositionCheck):
        nonlocal current, position, baseline, status, reason, last_check
        if status == "verified" and position != mark.quantity:
            fail(f"Coinbase position at {mark.as_of.isoformat()} is {_text(mark.quantity)} "
                 f"but fills replay to {_text(position)}")
        if mark.quantity == 0:
            current, position, baseline, status, reason = None, Decimal(0), mark, "verified", None
        if status == "verified":
            last_check = mark.as_of

    for raw in ordered:
        when = _aware(raw["trade_time"])
        while pending and pending[0].as_of < when:
            apply(pending.pop(0))
        if baseline is None or status == "unresolved":
            continue
        last_fill = when
        try:
            fill = _checked(raw, account_id, product_id)
            if len(by_time[when]) > 1:
                raise ValueError(f"{fill['entry_id']}: same-time fills in both directions; order unproven")
        except ValueError as exc:
            fail(str(exc))
            continue
        qty, sign, fee = fill["quantity"], fill["sign"], fill["fee"]

        def record(lc: Lifecycle, role: str, part: Decimal, fee_share):
            lc.fills.append({"entry_id": fill["entry_id"], "role": role, "quantity": _text(part),
                             "price": _text(fill["price"]), "fee_share": None if fee_share is None else _text(fee_share),
                             "trade_time": when.isoformat()})

        if position == 0:
            if fill["position_effect"] == "CLOSE":
                fail(f"{fill['entry_id']}: close with nothing open")
                continue
            current = Lifecycle(lifecycle_id(source_key, account_id, product_id, fill["entry_id"]), source_key,
                                account_id, product_id, "long" if sign > 0 else "short", fill["entry_id"], when)
            lifecycles.append(current)
            record(current, "open", qty, fee)
            position = sign * qty
        elif (position > 0) == (sign > 0):
            if fill["position_effect"] == "CLOSE":
                fail(f"{fill['entry_id']}: close flag on a same-direction fill")
                continue
            record(current, "add", qty, fee)
            position += sign * qty
        else:
            if fill["position_effect"] == "OPEN":
                fail(f"{fill['entry_id']}: open flag on an opposite-direction fill")
                continue
            held = abs(position)
            closing = min(qty, held)
            close_fee = None if fee is None else (fee if closing == qty else fee * closing / qty)
            record(current, "close", closing, close_fee)
            position += sign * closing
            current.open_quantity = abs(position)
            if position == 0:
                current.status, current.closed_at = "closed", when
            if qty > held:
                residual = qty - held
                current = Lifecycle(lifecycle_id(source_key, account_id, product_id, fill["entry_id"], residual=True),
                                    source_key, account_id, product_id, "long" if sign > 0 else "short",
                                    fill["entry_id"], when)
                lifecycles.append(current)
                record(current, "residual_open", residual, None if fee is None else fee - close_fee)
                position = sign * residual
        if current is not None:
            current.open_quantity = abs(position)
    while pending:
        apply(pending.pop(0))
    return scope(status, reason, baseline, lifecycles, last_check, last_fill)


def expire(scope: ScopeProjection, expirations: dict, now: datetime) -> ScopeProjection:
    """An open position past contract expiry settles without a fill, so it cannot stay open."""
    expiry = expirations.get((scope.account_id, scope.product_id))
    open_now = [lc for lc in scope.lifecycles if lc.status == "open"]
    if expiry is None or now < expiry or not open_now:
        return scope
    why = f"contract expired at {expiry.isoformat()}; settlement is not a fill"
    for lc in open_now:
        lc.status, lc.unresolved_reason = "unresolved", why
    return ScopeProjection(scope.source_key, scope.account_id, scope.product_id, "unresolved", why, scope.baseline,
                           scope.lifecycles, scope.last_check_at, scope.last_fill_at)


@dataclass(frozen=True, slots=True)
class Projection:
    scopes: tuple[ScopeProjection, ...]
    lifecycles: dict
    links: dict  # trade_id -> {"lifecycle_id": ...} or {"unresolved": why}


def _scope_fills(db) -> dict:
    scopes = defaultdict(list)
    marks = ", ".join("?" for _ in DERIVATIVE_SOURCES)
    for source_key, account_id, event_id, raw in db.execute(
            f"SELECT source_key, account_id, event_id, raw_json FROM source_events WHERE source_key IN ({marks}) "
            "ORDER BY source_key, account_id, event_id", DERIVATIVE_SOURCES):
        try:
            key = json.loads(event_id)
        except ValueError:
            continue  # bare IDs are close-packet copies of the same fills
        if not isinstance(key, list) or len(key) != 2 or key[0] != "fills":
            continue
        fill = json.loads(raw)
        scopes[(source_key, account_id, str(fill.get("product_id")))].append(fill)
    return scopes


def link_trade(trade: dict, lifecycles_by_fill: dict, scope_status: dict) -> dict:
    """Exact link: every saved fill in the close maps to roles in exactly one lifecycle."""
    key = (trade["source_key"], trade["account_id"], trade["market"])
    if trade["source_key"] not in DERIVATIVE_SOURCES:
        return {"unresolved": "lifecycles cover derivative products only"}
    state = scope_status.get(key)
    if state is None or state.status == "unavailable":
        return {"unresolved": state.reason if state else "no saved fills for this product"}
    try:
        events = json.loads(trade["packet_json"])["source_events"]
        ids = [e["event_id"] for e in events]
    except (ValueError, KeyError, TypeError):
        return {"unresolved": "close has no readable fill evidence"}
    if len(ids) < 2:
        return {"unresolved": "close has no opening fill evidence"}
    *opening, closing = ids
    candidates = None
    for entry_id, roles in [(i, OPENING_ROLES) for i in opening] + [(closing, ("close",))]:
        found = {lc.lifecycle_id for lc, role in lifecycles_by_fill.get((key, entry_id), ()) if role in roles}
        candidates = found if candidates is None else candidates & found
    if not candidates or len(candidates) != 1:
        return {"unresolved": "close fills do not map to exactly one replayed lifecycle"}
    (found,) = candidates
    lc = next(lc for lc, _ in lifecycles_by_fill[(key, closing)] if lc.lifecycle_id == found)
    if lc.status == "unresolved":
        return {"unresolved": lc.unresolved_reason}
    return {"lifecycle_id": found, "status": lc.status, "method": "exact_fill"}


def _limit_allocations(links: dict, trades: dict, lifecycles: dict) -> None:
    """Linked closes may not, together, exceed what the fills actually opened or closed."""
    groups = defaultdict(list)
    for trade_id, link in links.items():
        if "lifecycle_id" not in link:
            continue
        lc_id, (opening, closing, _) = link["lifecycle_id"], trades[trade_id]
        groups[(lc_id, "close", closing)].append(trade_id)
        groups[(lc_id, "total", None)].append(trade_id)
        if len(opening) == 1:
            groups[(lc_id, "opening", opening[0])].append(trade_id)
    for (lc_id, kind, entry_id), members in groups.items():
        fills = lifecycles[lc_id].fills
        if kind == "close":
            limit = sum((Decimal(f["quantity"]) for f in fills if f["entry_id"] == entry_id and f["role"] == "close"), Decimal(0))
        elif kind == "opening":
            limit = sum((Decimal(f["quantity"]) for f in fills if f["entry_id"] == entry_id and f["role"] in OPENING_ROLES), Decimal(0))
        else:
            limit = sum((Decimal(f["quantity"]) for f in fills if f["role"] == "close"), Decimal(0))
        used = sum((Decimal(str(trades[t][2])) for t in members), Decimal(0))
        if used > limit:
            for trade_id in members:
                links[trade_id] = {"unresolved": f"linked closes ({_text(used)}) exceed the fills' {kind} quantity ({_text(limit)})"}


def project(db, checks=None, *, now: datetime | None = None, expirations: dict | None = None) -> Projection:
    """Compute every scope, lifecycle and close link from saved rows; writes nothing.

    With `checks=None`, position checks and contract expiries come from saved, bracketed
    positions snapshots (see `positions.load_checks`).
    """
    from .positions import load_checks
    fills_by_scope = _scope_fills(db)
    extra = ()
    if checks is None:
        checks, saved_expirations, extra = load_checks(db, fills_by_scope)
        expirations = saved_expirations if expirations is None else expirations
    now = now or datetime.now(timezone.utc)
    scopes = []
    for (source_key, account_id, product_id), fills in sorted(fills_by_scope.items()):
        scope = replay_scope(fills, checks, source_key=source_key, account_id=account_id, product_id=product_id)
        scopes.append(expire(scope, expirations or {}, now))
    scopes.extend(extra)
    by_id, by_fill = {}, defaultdict(list)
    for scope in scopes:
        for lc in scope.lifecycles:
            by_id[lc.lifecycle_id] = lc
            for f in lc.fills:
                by_fill[((lc.source_key, lc.account_id, lc.product_id), f["entry_id"])].append((lc, f["role"]))
    status = {(s.source_key, s.account_id, s.product_id): s for s in scopes}
    links, evidence = {}, {}
    for row in db.execute("SELECT id, source_key, account_id, market, closed_quantity, packet_json FROM trades"):
        trade = dict(zip(("id", "source_key", "account_id", "market", "closed_quantity", "packet_json"), row))
        links[row[0]] = link_trade(trade, by_fill, status)
        if "lifecycle_id" in links[row[0]]:
            *opening, closing = [e["event_id"] for e in json.loads(trade["packet_json"])["source_events"]]
            evidence[row[0]] = (opening, closing, trade["closed_quantity"])
    _limit_allocations(links, evidence, by_id)
    return Projection(tuple(scopes), by_id, links)
