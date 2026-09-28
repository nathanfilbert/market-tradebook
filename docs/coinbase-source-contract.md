# Coinbase source contract (read access verified; import not verified)

This documents the implemented read-only adapter components and the public Coinbase field names they consume. A bounded authenticated pagination read returned 189 fills across 28 product IDs, including Spot and US CFM dated-futures records; one returned product had an OIL-like CFM contract root (`CDEOIL`). Four historical product-detail lookups failed, including two expired CFM contracts, so this is **not** proof of complete metadata coverage, US-perpetual coverage, correct financial allocation or live import. The project remains localhost-only, capture/review only, and has no order-placement path.

## Public source fields and mapping

The public Advanced Trade API documents historical fills at `GET /api/v3/brokerage/orders/historical/fills` and product metadata at `GET /api/v3/brokerage/products` (and the product detail route). The adapter's expected field names correspond to the documented public schemas; exact account values and availability must be confirmed for the user's account before use.

| Coinbase field | Adapter use | Boundary |
| --- | --- | --- |
| Fill `entry_id` | Stable fill/event identity; pagination deduplication | Required, nonempty string; conflicting duplicate IDs fail. |
| `trade_id`, `trade_type`, `sequence_timestamp` | Retained source evidence; pagination deduplication/order uses sequence timestamp then entry ID, while close assembly orders actual execution by `trade_time` then entry ID | Cursor, not a guessed `has_next`, is the continuation signal. |
| `trade_time` | Event time used for chronological close assembly | Must parse as timezone-aware; naive/invalid values are unresolved. |
| `price`, `size`, `side` | Decimal fill input and BUY/SELL direction | Positive finite values and recognized sides required. |
| `product_id`, `retail_portfolio_id` | Product/account identity checks | Must match explicitly supplied product and account identifiers. |
| `size_in_quote` | Quantity interpretation guard | For Spot, explicit `true` converts source quote `size` to base quantity as `Decimal(size) / Decimal(price)` only if exactly representable at the current 28-digit calculation precision, retaining the original `size`, `price` and flag in the event. Non-terminating or precision-losing divisions remain unresolved; derivatives remain unsupported pending a contract-unit rule. A bounded check of two quote-sized Spot orders matched the order's base `filled_size` within 1e-8 (one exactly); this does not establish a universal rounding rule. |
| `commission` | Preserved in raw source payload | Public fill schema does not establish commission currency; adapter does not convert it to USD or treat it as a verified fee amount. `fee_usd` is unavailable for these fetched fills. |
| Product `product_id`, `product_type`, `base_currency_id`, `quote_currency_id` | Spot identity, base quantity unit and quote currency | Requires explicit SPOT account family for spot. |
| `future_product_details.risk_managed_by` | Derivative family guard | Only `MANAGED_BY_FCM` is accepted for CFM classification; venue-managed/ambiguous products are not treated as US CFM. |
| `future_product_details.contract_expiry_type` | CFM type classification | `PERPETUAL` → perpetual; `EXPIRING` → dated future; other/missing values fail closed. |
| `future_product_details.contract_size` | Not used as multiplier | Metadata alone does not establish economic/unit meaning; multiplier remains unavailable. |

Public OpenAPI references: [Historical fills](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills), [product](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product), [list products](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/list-products), [portfolios](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/portfolios/list-portfolios). Their `.md` pages were inspected; public schema names do not guarantee access or field population for any particular account. The portfolio type enum has no CFM value, so portfolio discovery alone cannot prove a US futures account family.

## Implemented capabilities, bounded to fixtures/offline tests

- Read-only HTTP transport is allowlisted to GET historical fills, portfolio listings, product list and product detail routes. The direct-API authenticator reads `COINBASE_API_KEY_NAME` and `COINBASE_API_KEY_SECRET` at call time, loaded from an ignored local `.env` if not already in the environment. It uses the CDP SDK Ed25519-capable JWT generator, **not** the ECDSA-only Advanced Trade SDK. Neither value is included in docs, logs, or committed files.
- Historical fill paging validates page shape and cursor, deduplicates identical `entry_id` records, sorts by `(sequence_timestamp, entry_id)`, and fails on conflicting duplicates or repeated cursors.
- Product classifiers and source adapters exist for spot, CFM perpetual and CFM dated-future records supplied to the adapter. Assembly is conservative and preserves original event payloads.
- Offline/synthetic support remains available through `python -m tradebook.sync --mock`; current fixture data is fictional. Coinbase adapter tests use injected fake HTTP/data and do not constitute live integration verification.
- The CLI supports explicit Coinbase source, product and portfolio selection with `--dry-run --confirm-live-read` and a bounded fill history. It reads only allowlisted GET routes and does not write during dry-run. Spot source reconciliation spans all returned markets for the asset and the complete paged wallet transaction history, checking unique per-fill evidence, asset balance, and repeated source snapshots. An opt-in `--confirm-local-import` path can persist source-backed Spot and unambiguous CFM dated-future closes into a separate SQLite database after a reviewed dry-run; ambiguous CFM histories fail closed, and unverified derivative economics stay null.
- A bounded read-only wallet reader pages `GET /v2/accounts` and `GET /v2/accounts/{account_id}/transactions`. The audited BTC wallet returned 55 completed transactions; its signed BTC transaction amounts summed to its current balance, and grouped Advanced Trade fill transactions reconciled with source fills by order within one base increment. These records are now used for Spot inventory reconciliation, **not** for automatically assigning cost basis to transfers; source events are retained in the local ledger on import.
- A bounded read of the accessible USD wallet returned daily `derivatives_settlement` events alongside other cash transactions. A sampled settlement event had a timestamp and currency amount but **no instrument or position ID**; preserve these as account-level cash evidence, not an allocated fee, funding payment or per-close P/L.

## Fail-closed and unresolved

Unsupported/missing account or product identity, mismatches, unknown expiry, non-CFM futures, missing fill fields, duplicate/conflicting IDs, non-fill adjustments, derivative quote-sized fills, missing entry events, reversals, invalid timestamps/amounts and uncertain units do not yield an invented completed trade. For Spot, user-selected FIFO allocation can split a sale across multiple dated buy lots; this is a logging convention, not a tax-lot election. Derivative matching remains conservative and is not a complete Coinbase ledger or position matcher.

The following remain unknown until separately confirmed from account-specific records and documented semantics:

- whether all historical entry and settlement events are available for the verified Spot and US CFM dated-futures accounts; no US-perpetual fill was established by the bounded audit;
- commission denomination and any fee/rebate adjustment semantics;
- multiplier/contract economics, venue-specific quantity meaning, and funding records/currency for US perpetuals;
- dated-future settlement/expiry cashflows, full position lifecycle and reliable entry/exit allocation;
- exact contract economics of the observed OIL-like CFM product (`NOL-19OCT26-CDE`), and whether other named products are entitled;
- fiat notional valuation, FX, margin distinction, reported realized P/L, and funding/fee allocation rules.

The BTC wallet includes non-trade buy/deposit/withdrawal events and fills in both BTC-USD and BTC-USDC. Spot FIFO uses an **asset-wide** event timeline; deposits and transfers establish quantity but not acquisition price. Do not turn a `native_amount` display valuation into cost basis or count a v2 `advanced_trade_fill` a second time as a trade. The importer requires a unique order/product/price/signed-quantity match for each fill and repeat reads of wallet balance, transactions and fills before writing. A reviewed BTC-USD run imported 37 source-backed close allocations and 189 source events into the separate local Coinbase SQLite database; 14 rows have unavailable entry basis (3 transfers, 11 cross-currency). Replay into a separate scratch database retained the same row/event counts, and SQLite integrity, foreign keys and API readback passed. Two additional single-close CFM dated futures, including the OIL-like `NOL-19OCT26-CDE`, were imported with null multiplier, notional, fees, funding, and P/L. Other CFM histories containing multiple ambiguous lots were refused. This is not a tax lot election or claim of complete economic P/L.

Accordingly the adapter does not claim **complete** realized/net P&L, converted fees, funding, derivative fiat notional, complete account coverage, or reconciliation. For unambiguous USD-quoted spot closes, the existing projector can derive gross P/L and entry USD exposure from the source-backed base quantity and prices; net P/L remains unavailable when fees or funding are unknown. Unknown values remain unavailable rather than guessed. Only `reason` is user-editable; exchange-derived values are not editable.

## Safe future setup and operation

Local setup supplies `COINBASE_API_KEY_NAME` and `COINBASE_API_KEY_SECRET` through a protected environment/secret mechanism, only after the user chooses the account and explicitly authorizes a live read-only verification. Do not paste key material into chat, source, documentation, command history, or logs. Keep the application bound to `127.0.0.1`; it has no authentication and must not be exposed to a network. No order placement is supported or in scope. Until account-specific live reads and attribution have been verified, use synthetic/offline fixtures for trade import; the dry-run path is read-only and explicitly opt-in.

## Tests

Run from the repository root (after installing backend test dependencies):

```bash
cd backend
.venv/bin/python -m pytest tests/test_coinbase_auth.py tests/test_coinbase_http.py tests/test_coinbase_fills.py tests/test_coinbase_products.py tests/test_coinbase_source.py tests/test_coinbase_spot.py tests/test_coinbase_derivatives.py tests/test_coinbase_event_store.py -q
```

Full backend and frontend verification commands are listed in the repository README. Tests are local fixture/fake-based; no Coinbase network request is part of the test command.
