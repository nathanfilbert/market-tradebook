# Tradebook field inventory (design; not a frozen database schema)

## Agreed scope
- Capture Coinbase spot, perpetual and futures trading activity through interchangeable source adapters. Additional trade sources can be added later. The app **does not place trades**.
- Trading fields are populated from Coinbase records or transparently derived from those records; **only `reason` is manually entered**. An unavailable source value stays unavailable, never invented.
- Show position size in fiat terms as well as in native units. Use a responsive frontend (proposed: React + TypeScript + Vite) and a simple Python backend (proposed: FastAPI). The user accepted this architecture proposal; implementation is not authorized yet.

## Candidate columns and provenance

| Field | Origin | Meaning / limits |
| --- | --- | --- |
| `source` / `source_key` | Coinbase source adapter | Exact venue/account product family; initial conceptual adapters for spot, perpetual, and futures. Do not hard-code jurisdiction-specific identifiers before confirming account/product. |
| `source_account_id` | Coinbase | Non-secret stable account/portfolio reference where applicable. |
| `source_order_id`, `source_fill_id`, `source_position_id` | Coinbase | Preserve available identifiers; do not require all three for every product. Support traceability and deduplicated sync. |
| `timestamp` | Coinbase | Source event time with timezone, stored in UTC; whether the displayed row is a fill, close, or position summary remains undecided. |
| `product_type` | Coinbase / adapter | Spot, perpetual, or dated futures as applicable; distinguish exact source product identity. |
| `market` | Coinbase | Exact instrument symbol/product ID, not just the underlying asset. |
| `position_side` | Coinbase / derived | Long or short where meaningful and inferable; do not fabricate from a single isolated fill. |
| `position_quantity`, `quantity_unit` | Coinbase / derived | Base asset units or contracts; carry the contract multiplier/specification when a conversion needs it. |
| `position_notional_fiat`, `notional_currency` | Coinbase if available, otherwise derived | Fiat value of position exposure at a defined valuation point (proposed: entry), **not** margin posted. Retain valuation time, reference price, conversion rate and contract multiplier used; null if insufficient inputs. |
| `entry_timestamp`, `exit_timestamp`, `entry_price`, `exit_price` | Coinbase / grouped from fills | Optional until row granularity and grouping rules are agreed. Preserve the underlying fill records separately. |
| `gross_pnl`, `pnl_currency` | Coinbase if reported, otherwise derived | Realized result before allocated fees/adjustments, with derivation and source references. Unavailable until sufficient closing events and valuation rules exist. |
| `fees`, `fee_currency` | Coinbase / aggregated | Trading fees allocated to the record. Preserve source components and original denomination; no silent currency conversion. |
| `funding_adjustment`, `funding_currency` | Coinbase if applicable | Signed carrying/funding cash flow, separate from fees. May not apply to spot or every futures product. |
| `net_pnl`, `net_currency` | Coinbase if reported, otherwise derived | Net realized result with a documented calculation and cost scope. Flag conflicts between reported and computed figures; never silently replace source values. |
| `reason` | **Manual only** | User's discretionary rationale, editable from the UI; may be blank until supplied. Example reasons in discussion were fictional, not imported facts. |
| `synced_at`, `source_reference`, `revision` | System | Sync provenance, source lineage and reason/edit history. These are system metadata, not trading inputs. |

Use exact decimals for financial amounts. Keep reported and derived figures distinguishable; retain raw source references and cost components. Distinct currencies should not be summed without an explicit, traceable conversion. Trade summaries can be rebuilt from immutable source events after grouping rules are chosen.

## Decisions still open (not implementation approval)
1. Does a displayed row mean a fill, a realized partial close, a completed position, or a selectable summary? This determines grouping, entry/exit, P/L and reason association.
2. Which exact Coinbase account/product class is meant by “perp future,” and which API records are available for each product? Do not imply endpoint coverage before checking.
3. At what timestamp and in which fiat currency is position notional valued? Entry USD notional is a proposal, not a settled definition. What happens when price, multiplier or FX conversion is missing?
4. Which fee/funding/rebate/other cash flows count in gross versus net, and how should currency conversion and discrepancies be handled?
5. Which local persistence/deployment and account-access model is preferred? No storage engine, OAuth flow, sync cadence or hosting mode was agreed.
