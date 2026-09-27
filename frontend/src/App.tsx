// frontend/src/App.tsx
import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchTrades, saveReason, type Trade } from './api'
import './App.css'
const money = (value: string | null) => value === null ? '—' :
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value))

export default function App() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const selectedRef = useRef<string | null>(null)
  const [reason, setReason] = useState('')
  const [query, setQuery] = useState('')
  const [hasMore, setHasMore] = useState(false)
  const loadingMore = useRef(false)
  const [loadingMoreView, setLoadingMoreView] = useState(false)
  const offset = useRef(0)
  const [status, setStatus] = useState('Loading trades…')
  const [saving, setSaving] = useState(false)
  const active = trades.find(t => t.id === selected)
  const visible = useMemo(() => trades.filter(t =>
    `${t.market} ${t.product_type} ${t.source_key}`.toLowerCase().includes(query.toLowerCase())), [trades, query])
  useEffect(() => { fetchTrades().then(page => {
    setTrades(page.items); setHasMore(page.has_more)
    offset.current = page.items.length
    selectedRef.current = page.items[0]?.id ?? null
    setSelected(selectedRef.current); setReason(page.items[0]?.reason ?? '')
    setStatus(page.items.length ? '' : 'No captured trades yet.')
  }).catch(() => setStatus('Could not load trades.')) }, [])
  async function more() {
    if (loadingMore.current) return
    loadingMore.current = true
    setLoadingMoreView(true)
    setStatus('Loading more…')
    try {
      const page = await fetchTrades(offset.current)
      offset.current += page.items.length
      setTrades(items => {
        const known = new Set(items.map(item => item.id))
        return [...items, ...page.items.filter(item => !known.has(item.id))]
      })
      setHasMore(page.has_more); setStatus('')
    } catch { setStatus('Could not load more trades.') }
    finally { loadingMore.current = false; setLoadingMoreView(false) }
  }
  function choose(t: Trade) { selectedRef.current = t.id; setSelected(t.id); setReason(t.reason ?? ''); setStatus('') }
  async function submit() {
    if (!active) return
    const tradeId = active.id
    const revision = active.reason_revision
    const draft = reason
    setSaving(true); setStatus('')
    try {
      const saved = await saveReason(tradeId, draft, revision)
      setTrades(items => items.map(t => t.id === saved.id ? saved : t))
      if (selectedRef.current === tradeId) setStatus('Reason saved.')
    } catch {
      if (selectedRef.current === tradeId) setStatus('Could not save reason. Reload before retrying if the record changed.')
    }
    finally { setSaving(false) }
  }
  return <main className="shell">
    <header><h1>Market Tradebook</h1><p>Read-only trade data · reasons are yours to add</p></header>
    <label>Filter trades<input value={query} onChange={e => setQuery(e.target.value)} placeholder="Market, type or source" /></label>
    {status && <p role="status">{status}</p>}
    <div className="layout"><section aria-label="Captured trades" className="list">
      {visible.map(t => <button key={t.id} className={t.id === selected ? 'row active' : 'row'} onClick={() => choose(t)}>
        <strong>{t.market}</strong><span>{t.product_type} · {t.position_side} {t.closed_quantity} {t.quantity_unit}</span>
        <span>{new Date(t.close_time).toLocaleString()} · net {money(t.net_pnl_usd)}</span>
      </button>)}
      {trades.length > 0 && visible.length === 0 && <p>No matching trades.</p>}
      {hasMore && <button onClick={more} disabled={loadingMoreView}>Load more trades</button>}
    </section><section aria-label="Trade detail" className="detail">
      {active ? <><h2>{active.market}</h2><dl>
        <dt>Source</dt><dd>{active.source_key}</dd><dt>Type</dt><dd>{active.product_type}</dd>
        <dt>Position</dt><dd>{active.position_side} {active.closed_quantity} {active.quantity_unit}</dd>
        <dt>Entry USD notional</dt><dd>{money(active.position_notional_usd)}</dd>
        <dt>Valued at entry</dt><dd>{active.entry_time ? new Date(active.entry_time).toLocaleString() : '—'}</dd>
        <dt>Gross P/L</dt><dd>{money(active.gross_pnl_usd)}</dd><dt>Fees</dt><dd>{money(active.fee_usd)}</dd>
        <dt>Funding</dt><dd>{money(active.funding_usd)}</dd><dt>Net P/L</dt><dd>{money(active.net_pnl_usd)}</dd>
        <dt>Source-reported gross</dt><dd>{money(active.reported_gross_usd)}</dd>
        <dt>Source-reported net</dt><dd>{money(active.reported_net_usd)}</dd>
        <dt>Reconciliation</dt><dd>{active.reconciliation_status.replaceAll('_', ' ')}</dd>
      </dl><label>Reason<textarea value={reason} onChange={e => setReason(e.target.value)} maxLength={4000} /></label>
      <button onClick={submit} disabled={saving}>Save reason</button></> : <p>Select a trade to review.</p>}
    </section></div>
  </main>
}
