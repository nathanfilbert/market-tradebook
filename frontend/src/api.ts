// frontend/src/api.ts
export type Trade = {
  id: string; source_key: string; account_id: string; close_id: string;
  product_type: string; market: string; position_side: string;
  entry_time: string | null; close_time: string;
  entry_price?: string | null; exit_price?: string | null; price_currency?: string;
  closed_quantity: string; quantity_unit: string; position_notional_usd: string | null;
  gross_pnl_usd: string | null; fee_usd: string | null; funding_usd: string | null;
  fee_currency_assumed?: boolean | number;
  net_pnl_usd: string | null; reported_gross_usd: string | null;
  execution_net_pnl_usd?: string | null;
  pnl_method?: 'coinbase_orders_ex_funding' | null;
  reported_net_usd: string | null; reconciliation_status: string;
  basis_status?: 'known_quote_basis' | 'unknown_transfer_basis' | 'cross_currency_unavailable' | null;
  basis_currency?: string | null;
  reason: string | null; reason_revision: number;
}
export type TradePage = { items: Trade[]; has_more: boolean }

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  return response.json() as Promise<T>
}
export const fetchTrades = (offset = 0, recentOnly = true) =>
  json<TradePage>(`/api/trades?offset=${offset}&limit=100&recent=${recentOnly}`)
export const saveReason = (id: string, reason: string | null, revision: number) =>
  json<Trade>(`/api/trades/${encodeURIComponent(id)}/reason`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason, expected_revision: revision }),
  })
