"""Bracketed, read-only Coinbase CFM position snapshots used as lifecycle checks.

One capture reads the full fills history for the selected products, then
`GET /api/v3/brokerage/cfm/positions`, then the fills again. The snapshot is only
usable when both fills reads match, the key's portfolio is the configured account and,
per product, no fill lies within `MARGIN` of the positions request. A flat snapshot
taken after forward tracking was enabled is a flat point; a non-zero one only checks
the replay. Earlier history stays unresolved: a snapshot is current evidence, never
proof that the account was flat before it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from .coinbase_http import KEY_PERMISSIONS_ROUTE, POSITIONS_ROUTE
from .coinbase_products import UnsupportedProduct, classify_product
from .coinbase_source import _REQUIRED as REQUIRED_FILL_FIELDS
from .lifecycles import DERIVATIVE_SOURCES, PositionCheck, ScopeProjection
from .store import canonical_json, upsert_packets_and_events

CFM_SOURCE_KEYS = {"perpetual": "coinbase.cfm.us_perpetual", "dated_future": "coinbase.cfm.dated_future"}
MARGIN = timedelta(seconds=10)  # covers clock skew and fill visibility around the request


def initialize_positions(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS position_snapshots (
      snapshot_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, route TEXT NOT NULL,
      requested_at TEXT NOT NULL, received_at TEXT NOT NULL, product_ids_json TEXT NOT NULL,
      fills_consistent INTEGER NOT NULL, response_sha256 TEXT NOT NULL, raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS position_snapshots_account ON position_snapshots(account_id, requested_at);
    CREATE TABLE IF NOT EXISTS position_tracking (
      account_id TEXT PRIMARY KEY, enabled_at TEXT NOT NULL,
      enabled_snapshot_id TEXT NOT NULL REFERENCES position_snapshots(snapshot_id));
    """)


def parse_positions(payload) -> dict:
    """Map product_id to signed contracts and expiry; refuse anything ambiguous."""
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        raise ValueError("invalid positions response")
    result = {}
    for item in payload["positions"]:
        if not isinstance(item, dict) or not isinstance(item.get("product_id"), str) or not item["product_id"]:
            raise ValueError("position without product_id")
        product = item["product_id"]
        if product in result:
            raise ValueError(f"{product}: duplicate position")
        try:
            size = Decimal(str(item.get("number_of_contracts")))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"{product}: invalid number_of_contracts") from None
        if not size.is_finite() or size < 0:
            raise ValueError(f"{product}: invalid number_of_contracts")
        side = item.get("side")
        if size and side not in ("LONG", "SHORT"):
            raise ValueError(f"{product}: unknown position side")
        expiry = None
        if item.get("expiration_time"):
            expiry = datetime.fromisoformat(str(item["expiration_time"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError(f"{product}: expiration_time without timezone")
            expiry = expiry.astimezone(timezone.utc)
        result[product] = {"quantity": size if side == "LONG" else -size, "expiration_time": expiry}
    return result


def capture(http, db: sqlite3.Connection, *, account_id: str, selections, max_fills: int = 1000,
            clock=lambda: datetime.now(timezone.utc), enable: bool = False, read_fills=None) -> dict:
    """Read fills, positions, fills; save the bracket fills and the snapshot in one transaction."""
    from .coinbase_fills import iter_fills
    read_fills = read_fills or (lambda products: list(iter_fills(http, limit=250, product_ids=products,
                                                                  max_records=max_fills)))
    sources = {}
    for source_key, product_id in selections:
        if source_key not in DERIVATIVE_SOURCES or not isinstance(product_id, str) or not product_id:
            raise ValueError("positions snapshots cover CFM selections only")
        if product_id in sources:
            raise ValueError(f"{product_id}: selected more than once")
        sources[product_id] = source_key
    products = sorted(sources)
    if not products:
        raise ValueError("no CFM products selected")
    verify_cfm_products(http, sources)
    permissions = http.get(KEY_PERMISSIONS_ROUTE)
    if (not isinstance(permissions, dict) or permissions.get("can_view") is not True
            or permissions.get("portfolio_uuid") != account_id):
        raise ValueError("key is not a view key bound to the configured portfolio")
    before = read_fills(products)
    requested = clock()
    payload = http.get(POSITIONS_ROUTE)
    received = clock()
    parse_positions(payload)
    after = read_fills(products)
    order = lambda rows: canonical_json({"rows": sorted(rows, key=lambda f: f["entry_id"])})
    consistent = order(before) == order(after)
    for fill in after:
        if fill.get("retail_portfolio_id") != account_id or fill.get("product_id") not in sources:
            raise ValueError("fill outside the selected account or products")
        missing = [name for name in REQUIRED_FILL_FIELDS if name not in fill]
        if missing or fill.get("size_in_quote") is not False:
            raise ValueError("fill is not a contract-sized CFM fill with the documented fields")
    raw = canonical_json(payload)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    snapshot_id = hashlib.sha256(f"{account_id}|{requested.isoformat()}|{digest}".encode()).hexdigest()
    initialize_positions(db)
    db.execute("SAVEPOINT positions_capture")
    try:
        by_source = {}
        for fill in after:
            by_source.setdefault(sources[fill["product_id"]], []).append(fill)
        for source_key, fills in sorted(by_source.items()):
            upsert_packets_and_events(db, [], source_key=source_key, account_id=account_id, fills=fills)
        db.execute("INSERT INTO position_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (snapshot_id, account_id, POSITIONS_ROUTE, requested.isoformat(), received.isoformat(),
                    json.dumps(products), int(consistent), digest, raw))
        enabled = False
        if enable:
            if not consistent:
                raise ValueError("fills changed during the snapshot; tracking not enabled")
            enabled = db.execute("INSERT OR IGNORE INTO position_tracking VALUES (?, ?, ?)",
                                 (account_id, requested.isoformat(), snapshot_id)).rowcount == 1
        db.execute("RELEASE SAVEPOINT positions_capture")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT positions_capture")
        db.execute("RELEASE SAVEPOINT positions_capture")
        raise
    db.commit()
    return {"snapshot_id": snapshot_id, "fills_consistent": consistent, "tracking_enabled": enabled}


def verify_cfm_products(http, sources: dict) -> None:
    """Each selection's source family must match Coinbase's own product metadata (fail closed).

    A selection labelled CFM that Coinbase classifies as spot, as the other CFM family, or not at all
    is refused before anything is read or saved, so empty CFM positions never vouch for it."""
    for product_id, source_key in sorted(sources.items()):
        metadata = http.get("/api/v3/brokerage/products/" + quote(product_id, safe=""))
        try:
            spec = classify_product(metadata, account_family="CFM", source_reference="Coinbase Advanced Trade product GET")
        except (UnsupportedProduct, ValueError) as exc:
            raise ValueError(f"{product_id}: not a verified CFM product ({exc})") from None
        if spec.product_id != product_id or CFM_SOURCE_KEYS.get(spec.product_type) != source_key:
            raise ValueError(f"{product_id}: Coinbase classifies it as {spec.product_type}, not {source_key}")


def _tables(db) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def tracking_enabled(db, account_id: str) -> bool:
    if "position_tracking" not in _tables(db):
        return False
    return db.execute("SELECT 1 FROM position_tracking WHERE account_id=?", (account_id,)).fetchone() is not None


def latest_snapshots(db) -> dict:
    """Newest usable snapshot time per tracked account (for freshness)."""
    if not {"position_snapshots", "position_tracking"} <= _tables(db):
        return {}
    return {row[0]: datetime.fromisoformat(row[1]) for row in db.execute(
        "SELECT s.account_id, MAX(s.received_at) FROM position_snapshots s JOIN position_tracking t "
        "ON s.account_id=t.account_id WHERE s.requested_at >= t.enabled_at AND s.fills_consistent=1 "
        "GROUP BY s.account_id")}


def load_checks(db, fills_by_scope: dict):
    """Return (checks, expirations, extra scopes) from saved snapshots after enablement."""
    if not {"position_snapshots", "position_tracking"} <= _tables(db):
        return (), {}, ()
    times = {}
    for (source_key, account_id, product_id), fills in fills_by_scope.items():
        stamps = []
        for fill in fills:
            try:
                stamp = datetime.fromisoformat(str(fill.get("trade_time")).replace("Z", "+00:00"))
            except ValueError:
                continue  # the replay reports the scope unresolved: "unreadable fill time"
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                continue  # likewise for a time without a timezone; never compared with aware times
            stamps.append(stamp)
        times[(source_key, account_id, product_id)] = stamps
    checks, expirations, latest = [], {}, {}
    rows = db.execute(
        "SELECT s.snapshot_id, s.account_id, s.requested_at, s.received_at, s.product_ids_json, s.raw_json "
        "FROM position_snapshots s JOIN position_tracking t ON s.account_id=t.account_id "
        "WHERE s.requested_at >= t.enabled_at AND s.fills_consistent=1 ORDER BY s.requested_at, s.snapshot_id")
    for snapshot_id, account_id, requested_at, received_at, product_ids, raw in rows:
        requested, received = datetime.fromisoformat(requested_at), datetime.fromisoformat(received_at)
        try:
            positions = parse_positions(json.loads(raw))
        except ValueError:
            continue
        covered = set(json.loads(product_ids))
        for product_id, value in positions.items():
            if value["expiration_time"] is not None:
                expirations[(account_id, product_id)] = value["expiration_time"]
        latest[account_id] = (snapshot_id, received, positions, covered)
        for (source_key, scope_account, product_id), stamps in times.items():
            if scope_account != account_id or product_id not in covered:
                continue
            if any(requested - MARGIN <= t <= received + MARGIN for t in stamps):
                continue  # order between this fill and the snapshot is unproven
            quantity = positions.get(product_id, {"quantity": Decimal(0)})["quantity"]
            checks.append(PositionCheck(source_key, account_id, product_id, requested,
                                        f"positions snapshot {snapshot_id[:12]}", quantity))
    extra = []
    for account_id, (snapshot_id, received, positions, covered) in sorted(latest.items()):
        for product_id, value in sorted(positions.items()):
            if product_id not in covered and value["quantity"] != 0:
                extra.append(ScopeProjection("coinbase.cfm.positions", account_id, product_id, "unresolved",
                                             "Coinbase reports a position in a product whose fills are not synced",
                                             None, ()))
    return tuple(checks), expirations, tuple(extra)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture a bracketed read-only CFM positions snapshot")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--portfolio-id", required=True)
    parser.add_argument("--product", action="append", required=True, metavar="SOURCE_KEY:PRODUCT_ID")
    parser.add_argument("--max-fills", type=int, default=1000)
    parser.add_argument("--confirm-live-read", action="store_true")
    parser.add_argument("--confirm-local-import", action="store_true")
    parser.add_argument("--enable-tracking", action="store_true",
                        help="start forward tracking for this portfolio from this snapshot")
    args = parser.parse_args(argv)
    try:
        if not args.confirm_live_read or not args.confirm_local_import:
            raise ValueError("explicit live read and local import confirmation required")
        selections = [tuple(item.split(":", 1)) for item in args.product]
        from .coinbase_auth import create_coinbase_authenticator, load_coinbase_env
        from .coinbase_http import CoinbaseHTTP
        load_coinbase_env()
        if not os.environ.get("COINBASE_API_KEY_NAME") or not os.environ.get("COINBASE_API_KEY_SECRET"):
            raise ValueError("Coinbase signer credentials are not configured")
        http = CoinbaseHTTP(authenticator=create_coinbase_authenticator())
        db = sqlite3.connect(args.db.expanduser())
        try:
            result = capture(http, db, account_id=args.portfolio_id, selections=selections,
                             max_fills=args.max_fills, enable=args.enable_tracking)
        finally:
            db.close()
        print(f"positions snapshot saved; fills consistent: {result['fills_consistent']}; "
              f"tracking enabled now: {result['tracking_enabled']}")
        return 0
    except Exception as exc:
        print(f"Positions snapshot refused: {type(exc).__name__}")
        return 2


def readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:" + quote(str(db_path)) + "?mode=ro", uri=True)


if __name__ == "__main__":
    raise SystemExit(main())
