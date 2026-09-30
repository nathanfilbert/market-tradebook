"""Explicit, bounded order-based P/L enrichment of saved Coinbase CFM closes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .coinbase_order_pnl import collect_saved_executions, collect_pending_executions, persist_execution
from .store import backup, connect, get_trade, initialize
from .sync import DEFAULT_DB


def main(argv=None, *, http=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile saved CFM closes to Coinbase historical orders")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--pending-only", action="store_true", help="enrich each eligible unenriched close independently")
    parser.add_argument("--portfolio-id", help="required account selection for automatic enrichment")
    parser.add_argument("--max-closes", type=int, default=100, help="maximum automatic close attempts per run (1..1000)")
    parser.add_argument("--confirm-live-read", action="store_true")
    parser.add_argument("--confirm-local-import", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.db.expanduser().resolve()
        if (not args.confirm_live_read or not path.is_file() or path == DEFAULT_DB.resolve()
                or not 1 <= len(args.market) <= 5 or (not args.dry_run and not args.confirm_local_import)
                or (args.dry_run and args.confirm_local_import)
                or (args.pending_only and not args.portfolio_id) or not 1 <= args.max_closes <= 1000):
            raise ValueError("explicit read, database, and dry-run/import selection required")
        if http is None:
            from .coinbase_auth import load_coinbase_env, create_coinbase_authenticator
            from .coinbase_http import CoinbaseHTTP
            import os
            load_coinbase_env()
            if not os.environ.get("COINBASE_API_KEY_NAME") or not os.environ.get("COINBASE_API_KEY_SECRET"):
                raise ValueError("Coinbase read-only credentials unavailable")
            http = CoinbaseHTTP(authenticator=create_coinbase_authenticator())
        with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True) as reader:
            reader.row_factory = sqlite3.Row
            failed = 0
            if args.pending_only:
                collected, skipped, failed, deferred = collect_pending_executions(
                    reader, args.market, args.portfolio_id, http, max_closes=args.max_closes)
                print(f"order enrichment: {len(collected)} verified; {skipped} unsupported allocations; {failed} rejected/failed; {deferred} deferred")
            else:
                collected = collect_saved_executions(reader, args.market, http)
            if not collected:
                return 2 if failed else 0
            if not args.dry_run:
                target = path.with_name(path.stem + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".backup.sqlite3")
                if target.exists():
                    raise ValueError("backup target exists")
                backup(reader, target)
                target.chmod(0o600)
        if args.dry_run:
            for _, packet, _, result in collected:
                print(f"{packet.market}: calculated gross {result.gross_usd} USD; net after fees {result.net_after_fees_usd} USD (excludes funding and settlement)")
            return 2 if failed else 0
        db = connect(path)
        try:
            initialize(db)
            with db:
                db.execute("BEGIN IMMEDIATE")
                for trade_id, packet, orders, result in collected:
                    persist_execution(db, trade_id, packet, orders, result)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            for trade_id, packet, _, result in collected:
                stored = get_trade(db, trade_id)
                if (stored is None or stored["pnl_method"] != "coinbase_orders_ex_funding"
                        or stored["gross_pnl_usd"] != str(result.gross_usd)
                        or stored["execution_net_pnl_usd"] != str(result.net_after_fees_usd)):
                    raise ValueError("stored execution readback failed")
                print(f"{packet.market}: calculated gross {result.gross_usd} USD; net after fees {result.net_after_fees_usd} USD (excludes funding and settlement)")
        finally:
            db.close()
        return 2 if failed else 0
    except Exception as exc:
        print(f"Coinbase order enrichment refused: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
