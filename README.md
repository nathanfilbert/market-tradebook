# Market Tradebook

Local, single-user trade capture and review. **Read-only Coinbase access has been verified; local source-backed import requires explicit confirmation into a separate database.** The Spot importer reconciles asset-wide fill and wallet histories before building source-backed FIFO close allocations; it does not establish USD basis for transfers or USDC buys. CFM dated-futures imports are limited to unambiguous fill reductions and leave unverified contract economics, fees and P/L unavailable. The included mock source contains fictional spot, perpetual, and dated-futures close records so the downstream ledger and UI can be exercised. Only the `reason` is manually editable; all other fields originate with a source packet or a traceable calculation. See [the Coinbase source contract](docs/coinbase-source-contract.md) for public field mapping, account-specific unknowns, and fail-closed limits.

## Scope and data semantics

- One visible row represents a realized close or partial close. The source supplies a stable close ID, closed quantity, allocated entry/exit prices and known costs, and underlying source events. Spot exits allocate source-backed buy fills FIFO by execution time (a logging convention, not a tax-lot election); unknown fees/funding stay null. Derivative matching and cash-flow attribution remain conservative. The mock does not assert any Coinbase API shape.
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

## Coinbase read-only preflight (not live import)

After configuring a **view-only** CDP key in the ignored, owner-readable project `.env` as `COINBASE_API_KEY_NAME` and `COINBASE_API_KEY_SECRET`, and selecting a verified canonical product and portfolio ID, an explicitly authorized dry-run can be invoked from `backend/`. The direct API signer uses the CDP SDK and accepts Ed25519; the older Advanced Trade SDK does not.

```bash
.venv/bin/python -m tradebook.sync --source coinbase-spot \
  --product-id "$COINBASE_PRODUCT_ID" --portfolio-id "$COINBASE_PORTFOLIO_ID" \
  --db "$HOME/.local/share/market-tradebook/trades.sqlite3" \
  --max-fills 1000 --confirm-live-read --dry-run
```

Select `coinbase-cfm-perpetual` or `coinbase-cfm-dated-future` only after product metadata verifies `MANAGED_BY_FCM`; do not substitute INTX identifiers. The command uses allowlisted GET requests and does **not** write a database on dry-run. For a separately reviewed **Spot or unambiguous CFM dated-future** local capture, use a database path distinct from the default mock database and add `--confirm-local-import` while omitting `--dry-run`. The CLI backs up an existing target, refuses mock data in it, and stores source events plus close allocations atomically. It does not turn unknown fees, funding, multiplier or basis into P/L. Review the dry-run first; complex CFM histories fail closed, and no US CFM perpetual fills were found in the bounded account history. Never place credentials in command arguments, chat or committed files.

The reviewed BTC Spot and two unambiguous CFM dated-future imports are stored in `~/.local/share/market-tradebook/coinbase.sqlite3`, separate from the fictional demo. To review it in the local UI, set `TRADEBOOK_DB` to that path **only for the backend server process**, then launch the frontend as above. Do not run `--mock` against this database. This is a capture ledger, not tax accounting or validated trading performance; unknown-basis/economics rows show unavailable P/L.

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

No credentials are bundled, and there is no live sync scheduler, hosted deployment, multi-user access, trade placement, tax accounting, or automatic reason generation. Coinbase dry-run requires a separately configured read-only CDP key, explicit product/portfolio IDs and `--confirm-live-read`; it reads Coinbase but writes no trade records. Actual product coverage, lot matching, currency conversion and fee/funding allocation require account-specific verification before live writes can be enabled. See `docs/field-contract.md` and `docs/architecture.html` for the design record.
