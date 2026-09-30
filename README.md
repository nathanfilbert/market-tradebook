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

Open the URL printed by Vite (normally `http://127.0.0.1:5173/`). Sync is an explicit offline CLI action, never an API route. Running it again leaves four fictional close rows and their reasons intact. The API has `GET /api/health`, paged `GET /api/trades` (`recent=true` limits closes to the rolling last 30 days), `GET /api/trades/{id}`, and `PATCH /api/trades/{id}/reason` with `{ "reason": "...", "expected_revision": 0 }`; the last endpoint rejects any attempt to edit exchange fields. The frontend **defaults to the last 30 days**; **Show all history** reveals older saved rows without deleting them. Text filtering acts on the loaded rows; use **Load more trades** when available.

The frontend shows closed trades in a horizontally scrollable spreadsheet, including position, exposure, entry/exit prices, gross P/L, fees, net P/L and reason. Click a row (or its market button with the keyboard) to open the detail panel and edit only the reason. Position quantities and monetary amounts display two decimal places while stored source values keep their original precision. A dash means a value is unavailable, not zero.

For Coinbase CFM futures, the user's working assumption treats the fill `commission` as USD when no currency is supplied; the detail view labels those fees **USD assumed**, not verified by Coinbase. The original fill values remain in source events. Spot commissions are not covered by this assumption. Without matched historical order values and funding inputs, the full settled P/L remains unavailable.

For complete, two-fill CFM closes, read-only historical-order enrichment can use Coinbase's `filled_value`, `total_fees` and `total_value_after_fees` to calculate **execution gross P/L** and **net after trading fees**. These are not Coinbase-reported settled P/L and exclude funding and settlement adjustments; the UI marks net with † and retains the full net P/L field as unavailable. The order records are stored as immutable source evidence, with a separate calculation row that survives identical source replay. To verify then import only the selected saved markets (after configuring the view-only key), from `backend/`:

```bash
.venv/bin/python -m tradebook.coinbase_order_sync --db ~/.local/share/market-tradebook/coinbase.sqlite3 --market NOL-19OCT26-CDE --market BCP-20DEC30-CDE --confirm-live-read --dry-run
.venv/bin/python -m tradebook.coinbase_order_sync --db ~/.local/share/market-tradebook/coinbase.sqlite3 --market NOL-19OCT26-CDE --market BCP-20DEC30-CDE --confirm-live-read --confirm-local-import
```

The second command creates a timestamped SQLite backup, checks exact order/fill/account/fee consistency, and writes both selected rows atomically. It processes each saved close in the selected markets and refuses incomplete/multiple-fill orders rather than estimating a per-close allocation. Rerunning the same import is idempotent; changed source evidence requires investigation.

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

Select `coinbase-cfm-perpetual` or `coinbase-cfm-dated-future` only after product metadata verifies `MANAGED_BY_FCM`; do not substitute INTX identifiers. The command uses allowlisted GET requests and does **not** write a database on dry-run. For a separately reviewed **Spot or single-entry CFM dated-future** local capture, use a database path distinct from the default mock database and add `--confirm-local-import` while omitting `--dry-run`. The CLI backs up an existing target, refuses mock data in it, and stores source events plus close allocations atomically. Multi-entry CFM histories may be staged with `--confirm-local-import --stage-unresolved-events`; this saves their immutable raw fills but **publishes no trade rows** until starting position and entry allocation can be verified. It does not turn unknown fees, funding, multiplier or basis into P/L. No US CFM perpetual fills were found in the bounded account history. Never place credentials in command arguments, chat or committed files.

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

No credentials are bundled; recurring sync uses explicitly configured products. There is no hosted deployment, multi-user access, trade placement, tax accounting, or automatic reason generation. Coinbase dry-run requires a separately configured read-only CDP key, explicit product/portfolio IDs and `--confirm-live-read`; it reads Coinbase but writes no trade records. Actual product coverage, lot matching, currency conversion and fee/funding allocation require account-specific verification before live writes can be enabled. See `docs/field-contract.md` and `docs/architecture.html` for the design record.

## Local systemd service

The user service `market-tradebook.service` manages the API and local Vite frontend
through two companion units. Unit sources are in `ops/systemd/`; installed copies
are in `~/.config/systemd/user/`. The API uses the existing Coinbase ledger at
`~/.local/share/market-tradebook/coinbase.sqlite3`; starting the service performs no
Coinbase sync or mock import. Both listeners bind to localhost only.

- UI: http://127.0.0.1:5173/
- API: http://127.0.0.1:8017/api/health

```bash
systemctl --user status market-tradebook.service market-tradebook-api.service market-tradebook-ui.service
systemctl --user restart market-tradebook.service
systemctl --user stop market-tradebook.service
journalctl --user -u market-tradebook-api.service -u market-tradebook-ui.service
```

The main unit is enabled at user-manager startup. User lingering is already enabled,
so it also starts at boot without requiring an interactive login. The frontend uses
the existing local development server; this is a single-user local setup.


## Recurring Coinbase capture

`tradebook.recurring_sync` polls only the account/product selections in an explicit
local configuration; it does not discover or import arbitrary markets. Copy
`ops/sync.example.json` to `~/.config/market-tradebook/sync.json`, replace the
portfolio ID with the verified account identity, and add other reviewed products.
Keep the configuration owner-readable (`chmod 600`). Credentials continue to come
from the ignored project `.env`, never the configuration or systemd arguments.

For CFM histories, `--import-verified-closes` publishes only closes with matched
entry time and price, while preserving all raw fills, including ambiguous older
allocations and open positions. Unknown entry allocations remain withheld; invalid
fields, reversals, source conflicts and uncertain account/product identity still
refuse that product's entire batch. Spot retains its asset-wide reconciliation.
Entry matching does not verify multipliers, funding or settled P/L; unavailable
values stay unavailable. Replays preserve reasons and existing order enrichment.

Preview the configured selections before enabling writes:

```bash
cd /home/nathan/projects/market-tradebook/backend
.venv/bin/python -m tradebook.recurring_sync --config ~/.config/market-tradebook/sync.json --confirm-live-read --dry-run
```

Install `ops/systemd/market-tradebook-sync.service` and
`ops/systemd/market-tradebook-sync.timer` in `~/.config/systemd/user/`, then run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now market-tradebook-sync.timer
systemctl --user start market-tradebook-sync.service
systemctl --user list-timers market-tradebook-sync.timer
journalctl --user -u market-tradebook-sync.service
```

The timer checks five minutes after each completed run and starts after boot.
A database-specific lock prevents overlapping recurring runs; individual product
failures do not prevent other configured products from syncing, and a failed batch
marks the service failed in the journal. Scheduled capture is independent of the UI
service. Stop capture with `systemctl --user disable --now market-tradebook-sync.timer`
(and stop the sync service if a run is already active).

Changed imports receive timestamped SQLite backups before atomic writes. Unchanged
polls perform no writes or backups. History is bounded by `max_fills`; exceeding it
fails rather than silently truncating and requires review. New markets require a
configuration update. Reload the browser to see newly imported closes; the frontend
does not poll the API automatically. After each successful CFM capture, recurring sync enriches eligible saved closes
that do not already have order-based calculations. It reads their exact historical
entry/exit orders and calculates entry exposure, execution gross P/L and net after
trading fees. A close must cover complete one-fill entry and exit orders; partial
allocations, missing inputs, wrong accounts, inconsistent amounts and changed order
evidence never receive estimated values. Failed candidates remain unavailable and
mark the run failed while other verified candidates may still be saved. The run
reports verified, unsupported, rejected/failed and deferred counts in the journal.

Order enrichment is bounded to 100 candidate closes per product per run. Already
enriched rows need no further order reads or writes; source-packet changes invalidate
the calculation and require another validated enrichment. Multiple saved closes in
one market are supported. Funding, settlement adjustments and Coinbase-reported P/L
remain distinct and unavailable unless separately sourced. For a manual preview or
retry of pending closes, use:

```bash
.venv/bin/python -m tradebook.coinbase_order_sync --db ~/.local/share/market-tradebook/coinbase.sqlite3 --market BIP-20DEC30-CDE --portfolio-id VERIFIED_PORTFOLIO_ID --pending-only --confirm-live-read --dry-run
```

Replace `--dry-run` with `--confirm-local-import` to persist verified candidates.
Without `--pending-only`, the manual command requires every selected close to
validate and imports the selected batch atomically.
