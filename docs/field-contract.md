# Tradebook field inventory (design; not a frozen database schema)

## Agreed scope
- Read-only Coinbase spot and CFM adapter components now exist for injected fill/product data, including conservative classification and close assembly; this does not constitute live connectivity or establish account/product coverage. The current sync CLI remains mock-only. See `docs/coinbase-source-contract.md` for the public field mapping and explicit unknowns. Additional sources remain a future extension point. The app **does not place trades**.
- Trading fields are populated from Coinbase records or transparently derived from those records; **only `reason` is manually entered**. An unavailable source value stays unavailable, never invented.
- Show position size in fiat terms as well as in native units. Use a responsive frontend (React + TypeScript + Vite) and Python backend (FastAPI). These were accepted architectural directions; deployment and live account access are not thereby approved or verified.

## Candidate columns and provenance

| Field | Origin | Meaning / limits |
| --- | --- | --- |
| `source` / `source_key` | Coinbase source adapter | Exact venue/account product family; initial conceptual adapters for spot, perpetual, and futures. Do not hard-code jurisdiction-specific identifiers before confirming account/product. |
| `source_account_id` | Coinbase | Non-secret stable account/portfolio reference where applicable. |
| `source_order_id`, `source_fill_id`, `source_position_id` | Coinbase | Preserve available identifiers; do not require all three for every product. Support traceability and deduplicated sync. |
| `timestamp` | Coinbase | Source event time with timezone, stored in UTC; the implemented visible row is a realized close or partial close. |
| `product_type` | Coinbase / adapter | Spot, perpetual, or dated futures as applicable; distinguish exact source product identity. |
| `market` | Coinbase | Exact instrument symbol/product ID, not just the underlying asset. |
| `position_side` | Coinbase / derived | Long or short where meaningful and inferable; do not fabricate from a single isolated fill. |
| `position_quantity`, `quantity_unit` | Coinbase / derived | Base asset units or contracts; carry the contract multiplier/specification when a conversion needs it. |
| `position_notional_fiat`, `notional_currency` | Coinbase if available, otherwise derived | Fiat value of position exposure at a defined valuation point (proposed: entry), **not** margin posted. Retain valuation time, reference price, conversion rate and contract multiplier used; null if insufficient inputs. |
| `entry_timestamp`, `exit_timestamp`, `entry_price`, `exit_price` | Coinbase / grouped from fills | Optional when source-backed entries or allocation are unavailable. Preserve the underlying fill records separately. |
| `gross_pnl`, `pnl_currency` | Coinbase if reported, otherwise derived | Realized result before allocated fees/adjustments, with derivation and source references. Unavailable until sufficient closing events and valuation rules exist. |
| `fees`, `fee_currency` | Coinbase / aggregated | Trading fees allocated to the record. Preserve source components and original denomination; no silent currency conversion. |
| `funding_adjustment`, `funding_currency` | Coinbase if applicable | Signed carrying/funding cash flow, separate from fees. May not apply to spot or every futures product. |
| `net_pnl`, `net_currency` | Coinbase if reported, otherwise derived | Net realized result with a documented calculation and cost scope. Flag conflicts between reported and computed figures; never silently replace source values. |
| `reason` | **Manual only** | User's discretionary rationale, editable from the UI; may be blank until supplied. Example reasons in discussion were fictional, not imported facts. |
| `synced_at`, `source_reference`, `revision` | System | Sync provenance, source lineage and reason/edit history. These are system metadata, not trading inputs. |

Use exact decimals for financial amounts. Keep reported and derived figures distinguishable; retain raw source references and cost components. Distinct currencies should not be summed without an explicit, traceable conversion. Trade summaries can be rebuilt from immutable source events after grouping rules are chosen.

## Implemented choices and remaining questions
The current local app uses one realized full/partial close per row, entry USD notional for the closed allocation when its inputs are known, and single-user SQLite persistence. These choices do not establish that Coinbase exposes every required historical event for a specific account.
1. The selected target is Coinbase Advanced Trade spot and US CFM perpetual/dated futures, including OIL only if an eligible canonical product is actually returned. Exact account access and fill/funding/settlement coverage remain unverified.
2. Spot sale allocation is user-selected FIFO across oldest open buys of the same product (a logging convention, not a tax-lot election). Correction and fee/cost-allocation rules beyond that remain unresolved; unknown source inputs remain null or unresolved.
3. Which fee/funding/rebate/other cash flows count in net P/L, and how should currencies be converted and discrepancies handled? Do not infer a futures multiplier or fiat notional from an unverified unit.
   - For the two saved single-fill-pair CFM closes, matched Coinbase historical orders supply quote-USD `filled_value` and `total_value_after_fees`. The local view now distinguishes calculated execution gross/net-after-trading-fees from unavailable Coinbase-reported and fully settled net P/L; funding/settlement remain excluded. This does not resolve semantics for other futures, mixed currencies, partial fills or multi-entry closes.
4. Deployment beyond localhost, authentication, sync cadence and live account verification remain open; code-level adapter tests do not constitute live import approval.
