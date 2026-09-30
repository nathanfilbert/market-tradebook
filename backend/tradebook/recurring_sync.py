"""Run explicit read-only Coinbase selections under one non-overlapping local lock."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path

from .sync import DEFAULT_DB, SOURCE_TYPES, main as sync_main
from .coinbase_order_sync import main as order_sync_main

MAX_SOURCE_SELECTIONS = 32


def load_config(path: Path) -> tuple[Path, int, list[dict]]:
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {"db", "max_fills", "sources"}:
        raise ValueError("invalid sync configuration")
    if not isinstance(config["db"], str) or not config["db"].strip():
        raise ValueError("explicit database required")
    db = Path(config["db"]).expanduser()
    if not db.is_absolute() or db.resolve() == DEFAULT_DB.resolve():
        raise ValueError("explicit non-demo absolute database required")
    maximum = config["max_fills"]
    if type(maximum) is not int or not 1 <= maximum <= 10000:
        raise ValueError("invalid history bound")
    sources = config["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCE_SELECTIONS:
        raise ValueError("explicit bounded source selections required")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source", "product_id", "portfolio_id"}:
            raise ValueError("invalid source selection")
        if source["source"] not in SOURCE_TYPES:
            raise ValueError("unsupported source")
        for field in ("product_id", "portfolio_id"):
            if not isinstance(source[field], str) or not source[field].strip() or source[field].startswith("-"):
                raise ValueError("missing source identity")
        identity = (source["portfolio_id"], source["product_id"])
        if identity in seen:
            raise ValueError("duplicate product selection")
        seen.add(identity)
    return db.resolve(), maximum, sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--confirm-live-read", action="store_true")
    parser.add_argument("--confirm-local-import", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.confirm_live_read or args.dry_run == args.confirm_local_import:
            raise ValueError("explicit read and dry-run/import selection required")
        db, maximum, sources = load_config(args.config)
        # Dry-run neither creates a database nor a lock alongside it.
        if args.dry_run:
            return run_sources(db, maximum, sources, dry_run=True)
        db.parent.mkdir(parents=True, exist_ok=True)
        with db.with_suffix(db.suffix + ".sync.lock").open("a") as lock:
            db.with_suffix(db.suffix + ".sync.lock").chmod(0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("sync skipped: another recurring run holds the database lock")
                return 0
            return run_sources(db, maximum, sources, dry_run=False)
    except Exception as exc:
        # Config or dependency failures must not leak credentials or source payloads.
        print(f"Recurring sync refused: {type(exc).__name__}")
        return 2


def run_sources(db: Path, maximum: int, sources: list[dict], *, dry_run: bool) -> int:
    failed = False
    for source in sources:
        args = ["--source", source["source"], "--product-id", source["product_id"],
                "--portfolio-id", source["portfolio_id"], "--db", str(db),
                "--max-fills", str(maximum), "--confirm-live-read",
                "--dry-run" if dry_run else "--confirm-local-import"]
        if source["source"] != "coinbase-spot":
            args.append("--import-verified-closes")
        print(f"syncing {source['product_id']}", flush=True)
        if sync_main(args) != 0:
            failed = True
            continue
        if source["source"] != "coinbase-spot" and db.is_file():
            enrichment_args = ["--db", str(db), "--market", source["product_id"],
                               "--portfolio-id", source["portfolio_id"], "--pending-only",
                               "--confirm-live-read", "--dry-run" if dry_run else "--confirm-local-import"]
            if order_sync_main(enrichment_args) != 0:
                failed = True
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
