# backend/tradebook/sync.py
import argparse
import os
from pathlib import Path
from .mock import MockSource
from .source import TradeSource
from .store import connect, initialize, upsert_packet

DEFAULT_DB = Path.home() / ".local/share/market-tradebook/trades.sqlite3"


def sync(db, source: TradeSource) -> int:
    count = 0
    for packet in source.iter_closes():
        if packet.source_key != source.source_key:
            raise ValueError("source key mismatch")
        upsert_packet(db, packet)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", required=True,
                        help="load fictional demo records only")
    args = parser.parse_args()
    db = connect(os.environ.get("TRADEBOOK_DB", str(DEFAULT_DB)))
    try:
        initialize(db)
        print(f"synced {sync(db, MockSource())} fictional close packets")
    finally:
        db.close()


if __name__ == "__main__":
    main()
