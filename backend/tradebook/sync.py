"""Explicit, read-only Coinbase import orchestration and fictional mock sync."""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from .mock import MockSource
from .source import TradeSource
from .store import connect, initialize, upsert_packets_and_events, backup

DEFAULT_DB = Path.home() / ".local/share/market-tradebook/trades.sqlite3"
SOURCE_TYPES = ("coinbase-spot", "coinbase-cfm-perpetual", "coinbase-cfm-dated-future")


def sync(db, source: TradeSource) -> int:
    packets = list(source.iter_closes())
    fills = getattr(source, "_fills", ())
    wallet_events = getattr(source, "_wallet_events", ())
    wallet_account_id = getattr(source, "wallet_account_id", None)
    account_id = getattr(source, "account_id", packets[0].account_id if packets else "")
    if not account_id and packets:
        account_id = packets[0].account_id
    return upsert_packets_and_events(db, packets, source_key=source.source_key,
                                     account_id=account_id, fills=fills,
                                     wallet_events=wallet_events, wallet_account_id=wallet_account_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync trade closes from mock or Coinbase reads")
    parser.add_argument("--source", choices=("mock", *SOURCE_TYPES))
    parser.add_argument("--product-id")
    parser.add_argument("--portfolio-id")
    parser.add_argument("--confirm-live-read", action="store_true")
    parser.add_argument("--confirm-local-import", action="store_true",
                        help="allow verified source-backed closes to write the selected local SQLite file")
    parser.add_argument("--db", type=Path, help="explicit SQLite path (required for Coinbase)")
    parser.add_argument("--max-fills", type=int, default=1000, help="maximum accepted history (1..10000)")
    parser.add_argument("--dry-run", action="store_true", help="read and validate without writing records")
    parser.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)
    return parser


def validate_live_args(args) -> None:
    if not args.confirm_live_read:
        raise ValueError("Coinbase reads require --confirm-live-read")
    if not args.product_id or not args.portfolio_id:
        raise ValueError("Coinbase reads require --product-id and --portfolio-id")
    if not args.db:
        raise ValueError("Coinbase reads require an explicit --db path")
    if not 1 <= args.max_fills <= 10000:
        raise ValueError("--max-fills must be between 1 and 10000")
    if not os.environ.get("COINBASE_API_KEY_NAME") or not os.environ.get("COINBASE_API_KEY_SECRET"):
        raise ValueError("Coinbase signer credentials are not configured")


def _live_source(args):
    from decimal import Decimal
    from .coinbase_auth import create_coinbase_authenticator
    from .coinbase_fills import iter_fills
    from .coinbase_http import CoinbaseHTTP
    from .coinbase_discovery import CoinbaseDiscovery
    from .coinbase_products import classify_product
    from .coinbase_source import CoinbaseAssetSpotSource, CoinbaseCFMPerpetualSource, CoinbaseCFMDatedFutureSource

    http = CoinbaseHTTP(authenticator=create_coinbase_authenticator())
    CoinbaseDiscovery(http).require_portfolio(args.portfolio_id)
    product_data = http.get("/api/v3/brokerage/products/" + args.product_id)
    account_family = "SPOT" if args.source == "coinbase-spot" else "CFM"
    specification = classify_product(product_data, account_family=account_family, source_reference="Coinbase Advanced Trade product GET")
    expected = {"coinbase-spot": "spot", "coinbase-cfm-perpetual": "perpetual", "coinbase-cfm-dated-future": "dated_future"}[args.source]
    if specification.product_id != args.product_id or specification.product_type != expected:
        raise ValueError("Coinbase product classification did not match selected source")
    if expected == "spot":
        from .store import canonical_json
        from .coinbase_wallet import CoinbaseWalletReader
        from .coinbase_spot_inventory import build_spot_fifo
        fills = list(iter_fills(http, limit=min(args.max_fills, 250), max_records=args.max_fills))
        asset = specification.quantity_unit
        candidate_ids = sorted({fill.get("product_id") for fill in fills
                                if isinstance(fill.get("product_id"), str) and
                                fill["product_id"].startswith(asset + "-")})
        specs = {args.product_id: specification}
        for product_id in candidate_ids:
            if product_id not in specs:
                product = http.get("/api/v3/brokerage/products/" + product_id)
                specs[product_id] = classify_product(product, account_family="SPOT",
                                                     source_reference="Coinbase Advanced Trade product GET")
        asset_fills = [fill for fill in fills if fill.get("product_id") in specs]
        reader = CoinbaseWalletReader(authenticator=create_coinbase_authenticator(),
                                      max_pages=20, max_records=args.max_fills)
        wallets = [row for row in reader.accounts() if isinstance(row, dict) and
                   isinstance(row.get("currency"), dict) and row["currency"].get("code") == asset]
        if len(wallets) != 1:
            raise ValueError("Coinbase asset wallet identity is ambiguous")
        wallet = wallets[0]
        transactions = reader.transactions(wallet["id"])
        after_wallets = [row for row in reader.accounts() if isinstance(row, dict) and
                         isinstance(row.get("currency"), dict) and row["currency"].get("code") == asset]
        if len(after_wallets) != 1 or after_wallets[0]["id"] != wallet["id"] or canonical_json(after_wallets[0]) != canonical_json(wallet):
            raise ValueError("Coinbase wallet changed during collection")
        after_transactions = reader.transactions(wallet["id"])
        after_fills = list(iter_fills(http, limit=min(args.max_fills, 250), max_records=args.max_fills))
        if (canonical_json({"rows": sorted(transactions, key=lambda row: row["id"])}) !=
            canonical_json({"rows": sorted(after_transactions, key=lambda row: row["id"])}) or
            canonical_json({"rows": sorted(fills, key=lambda row: row["entry_id"])}) !=
            canonical_json({"rows": sorted(after_fills, key=lambda row: row["entry_id"])})
        ):
            raise ValueError("Coinbase source history changed during collection")
        result = build_spot_fifo(transactions, asset_fills, asset=asset,
                                 account_id=args.portfolio_id, wallet_account_id=wallet["id"],
                                 wallet_balance=Decimal(wallet["balance"]["amount"]),
                                 quantity_increment=Decimal(http.get("/api/v3/brokerage/products/" + args.product_id)["base_increment"]),
                                 product_specs=specs)
        source = CoinbaseAssetSpotSource(args.portfolio_id, asset_fills, wallet["id"], transactions, result)
        if source.unresolved:
            raise ValueError(f"Coinbase inventory has {len(source.unresolved)} unresolved records; refusing import")
        return source, list(source.iter_closes())
    fills = list(iter_fills(http, limit=min(args.max_fills, 250), product_ids=[args.product_id], max_records=args.max_fills))
    cls = {"perpetual": CoinbaseCFMPerpetualSource, "dated_future": CoinbaseCFMDatedFutureSource}[expected]
    source = cls(args.portfolio_id, specification, fills)
    packets = list(source.iter_closes())
    if source.unresolved:
        raise ValueError(f"Coinbase history has {len(source.unresolved)} unresolved records; refusing import")
    return source, packets


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mock:
        if args.source not in (None, "mock"):
            parser.error("--mock cannot be combined with a Coinbase source")
        args.source = "mock"
    if args.source is None:
        parser.error("--source is required (or use --mock)")
    if args.source == "mock":
        if args.confirm_local_import or args.confirm_live_read:
            parser.error("Coinbase confirmation flags cannot be used with mock")
        path = args.db or Path(os.environ.get("TRADEBOOK_DB", str(DEFAULT_DB)))
        existed = path.exists()
        db = connect(str(path))
        try:
            if existed:
                backup(db, path.with_suffix(path.suffix + ".bak"))
            initialize(db)
            print(f"synced {sync(db, MockSource())} fictional close packets")
        finally:
            db.close()
        return 0
    try:
        from .coinbase_auth import load_coinbase_env
        load_coinbase_env()
        validate_live_args(args)
        if not args.dry_run and not args.confirm_local_import:
            raise ValueError("Local Coinbase import requires explicit confirmation")
        source, packets = _live_source(args)
        if args.dry_run:
            print(f"dry-run: validated {len(packets)} Coinbase close packets; no records written")
            return 0
        if not packets:
            raise ValueError("no source-backed close packets to import")
        if args.db.resolve() == DEFAULT_DB.resolve():
            raise ValueError("Coinbase import requires a separate explicit database")
        existed = args.db.exists()
        db = connect(str(args.db))
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if existed:
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                destination = args.db.with_name(f"{args.db.stem}.{suffix}.backup.sqlite3")
                if destination.exists():
                    raise ValueError("backup target already exists")
                backup(db, destination)
                destination.chmod(0o600)
            initialize(db)
            existing_sources = {row[0] for row in db.execute("SELECT DISTINCT source_key FROM trades")}
            if "mock.tradebook" in existing_sources:
                raise ValueError("refusing to mix fictional and Coinbase trades")
            print(f"synced {sync(db, source)} Coinbase close packets")
            db_path = args.db
            db_path.chmod(0o600)
        finally:
            db.close()
        return 0
    except Exception as exc:
        # Emit only sanitized, known-safe exception categories/messages.
        print(f"Coinbase sync refused: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
