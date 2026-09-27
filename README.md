# Market Tradebook

Local, single-user trade capture and review. **No Coinbase connection or order execution exists yet.** The included mock source contains fictional spot, perpetual, and dated-futures close records so the downstream ledger and UI can be exercised. Only the `reason` is manually editable; all other fields originate with a source packet or a traceable calculation.

## Scope and data semantics

- One visible row represents a realized close or partial close. The source supplies a stable close ID, closed quantity, allocated entry/exit prices and costs, and underlying source events. Real Coinbase adapters, source matching and cash-flow allocation are future work; the mock does not assert any Coinbase API shape.
- USD entry notional is the exposure of the **closed allocation**: quantity × entry price × contract multiplier. It is not margin. Original price, quantity, multiplier, timestamps, account/source IDs, and source event payloads remain in SQLite.
- Calculated gross and net are separate from any source-reported values; the detail view labels reconciliation as matched, mismatched, not comparable, or unavailable. Net is unknown unless USD-quoted entry/exit prices, multiplier, fee, and funding allocations are all known. Zero cost is explicit zero, not a missing value. Non-USD quoted markets and spot shorts are not calculated in this initial slice; no currency conversion or borrowing economics are guessed.
- Source events are keyed by `(source_key, account_id, event_id)` and cannot silently change on replay. Sync preserves the manually entered reason and its revision history.

## Requirements

Python 3.11+, Node.js compatible with the installed Vite version, and npm. Bind only to localhost: this app has no authentication and must not be exposed to a network.

## Set up

```bash
cd /home/nathan/projects/market-tradebook/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cd ../frontend
npm ci
```

## Run with fictional data

First terminal:

```bash
cd /home/nathan/projects/market-tradebook/backend
export TRADEBOOK_DB="$PWD/demo.sqlite3"
.venv/bin/python -m tradebook.sync --mock
.venv/bin/python -m uvicorn tradebook.api:app --host 127.0.0.1 --port 8017
```

Second terminal:

```bash
cd /home/nathan/projects/market-tradebook/frontend
npm run dev -- --host 127.0.0.1
```

Open the URL printed by Vite (normally `http://127.0.0.1:5173/`). Sync is an explicit offline CLI action, never an API route. Running it again leaves four fictional close rows and their reasons intact. The API has `GET /api/health`, paged `GET /api/trades`, `GET /api/trades/{id}`, and `PATCH /api/trades/{id}/reason` with `{ "reason": "...", "expected_revision": 0 }`; the last endpoint rejects any attempt to edit exchange fields. The frontend filters currently loaded rows; use **Load more trades** to reach older records.

## Verify

```bash
cd /home/nathan/projects/market-tradebook/backend
.venv/bin/python -m pytest tests -q
cd ../frontend
npm test -- --run
npm run build
npm run lint
curl -fsS http://127.0.0.1:8017/api/health
curl -fsS http://127.0.0.1:8017/api/trades
```

The mock sync prints `synced 4 fictional close packets`. The first page has four records and `has_more: false`. Tests also check partial-close event deduplication, reason-only editing, and backup restoration. The built frontend is generated at `frontend/dist/` but is not served by FastAPI; local operation uses the two processes above.

## Backup and restore

Use SQLite's online backup API, not a file copy of a database that might be in use:

```bash
cd /home/nathan/projects/market-tradebook/backend
TRADEBOOK_DB="$PWD/demo.sqlite3" .venv/bin/python - <<'PY'
import os
from pathlib import Path
from tradebook.store import backup, connect
with connect(os.environ['TRADEBOOK_DB']) as db:
    backup(db, Path('demo-backup.sqlite3'))
print('backup written: demo-backup.sqlite3')
PY
```

To inspect a backup without touching the original database, set `TRADEBOOK_DB` to its absolute path and use the read API. Test `PRAGMA integrity_check` and `PRAGMA foreign_key_check` before treating any restored copy as sound.

## Boundaries

No API credentials, live sync scheduler, hosted deployment, multi-user access, trade placement, tax accounting, or automatic reason generation are included. Exact Coinbase product coverage, lot matching, currency conversion and fee/funding allocation need separate design and verification before live data is imported. See `docs/field-contract.md` and `docs/architecture.html` for the design record.
