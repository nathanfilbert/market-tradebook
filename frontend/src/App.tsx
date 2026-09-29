// frontend/src/App.tsx
import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchTrades, saveReason, type Trade } from './api'
import './App.css'
const money = (value: string | null) => value === null ? '—' :
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value))
const quantity = (value: string, unit: string) =>
  `${new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value))} ${unit}`
const price = (value: string | null | undefined, currency?: string) =>
  value === null || value === undefined ? '—' : `${value} ${currency ?? ''}`.trim()

export default function App() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const selectedRef = useRef<string | null>(null)
  const closeButton = useRef<HTMLButtonElement | null>(null)
  const openedFrom = useRef<HTMLButtonElement | null>(null)
  const [reason, setReason] = useState('')
  const [query, setQuery] = useState('')
  const [recentOnly, setRecentOnly] = useState(true)
  const generation = useRef(0)
  const [hasMore, setHasMore] = useState(false)
  const loadingMore = useRef(false)
  const [loadingMoreView, setLoadingMoreView] = useState(false)
  const offset = useRef(0)
  const [status, setStatus] = useState('Loading trades…')
  const [saving, setSaving] = useState(false)
  const active = trades.find(t => t.id === selected)
  const reasonStatus = status === 'Reason saved.' || status.startsWith('Could not save reason.')
  const visible = useMemo(() => trades.filter(t =>
    `${t.market} ${t.product_type} ${t.source_key}`.toLowerCase().includes(query.toLowerCase())), [trades, query])
  const visiblePnl = useMemo(() => {
    const cents = visible.reduce((total, trade) => {
      const value = trade.execution_net_pnl_usd ?? trade.net_pnl_usd
      return value === null ? total : total + Math.round(Number(value) * 100)
    }, 0)
    return cents === 0 && !visible.some(trade => (trade.execution_net_pnl_usd ?? trade.net_pnl_usd) !== null)
      ? null : (cents / 100).toFixed(2)
  }, [visible])
  useEffect(() => {
    let cancelled = false
    fetchTrades(0, recentOnly).then(page => {
      if (cancelled) return
      setTrades(page.items); setHasMore(page.has_more)
      offset.current = page.items.length
      selectedRef.current = null
      setSelected(null); setReason('')
      setStatus(page.items.length ? '' : 'No captured trades in this view.')
    }).catch(() => { if (!cancelled) setStatus('Could not load trades.') })
    return () => { cancelled = true }
  }, [recentOnly])
  useEffect(() => { if (selected) closeButton.current?.focus() }, [selected])
  function changeWindow() {
    generation.current += 1
    loadingMore.current = false; setLoadingMoreView(false)
    setTrades([]); setHasMore(false); setSelected(null); selectedRef.current = null
    setReason(''); offset.current = 0; setStatus('Loading trades…')
    setRecentOnly(value => !value)
  }
  async function more() {
    if (loadingMore.current) return
    const requestGeneration = generation.current
    loadingMore.current = true
    setLoadingMoreView(true)
    setStatus('Loading more…')
    try {
      const page = await fetchTrades(offset.current, recentOnly)
      if (requestGeneration !== generation.current) return
      offset.current += page.items.length
      setTrades(items => {
        const known = new Set(items.map(item => item.id))
        return [...items, ...page.items.filter(item => !known.has(item.id))]
      })
      setHasMore(page.has_more); setStatus('')
    } catch { if (requestGeneration === generation.current) setStatus('Could not load more trades.') }
    finally { if (requestGeneration === generation.current) { loadingMore.current = false; setLoadingMoreView(false) } }
  }
  function choose(t: Trade) {
    openedFrom.current = document.getElementById(`trade-${t.id}`) as HTMLButtonElement | null
    selectedRef.current = t.id; setSelected(t.id); setReason(t.reason ?? ''); setStatus('')
  }
  function closeDetails() {
    selectedRef.current = null; setSelected(null); setReason('')
    openedFrom.current?.focus()
  }
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
    <header className="topbar"><div><p className="eyebrow">COINBASE · CAPTURE ONLY</p><h1>Tradebook</h1>
      <p className="subtitle">A clear view of your closed trades. Select a row for details and your reason.</p></div>
    </header>
    <section className="pnl-summary" aria-label="Visible total profit and loss">
      <span className="pnl-summary-label">VISIBLE TOTAL P/L</span>
      <strong className={visiblePnl === null ? 'muted' : Number(visiblePnl) < 0 ? 'negative' : 'positive'}>{money(visiblePnl)}</strong>
      <span className="pnl-summary-note">Visible rows only; unavailable P/L excluded.</span>
    </section>
    <div className="toolbar"><div className="view-controls"><span className="view-label">VIEW</span>
      <button className={recentOnly ? 'view-button current' : 'view-button'} onClick={() => { if (!recentOnly) changeWindow() }} aria-pressed={recentOnly}>Last 30 days</button>
      <button className={!recentOnly ? 'view-button current' : 'view-button'} onClick={() => { if (recentOnly) changeWindow() }} aria-pressed={!recentOnly}>{recentOnly ? 'Show all history' : 'All history'}</button></div>
      <label className="filter">Search trades<input value={query} onChange={e => setQuery(e.target.value)} placeholder="Market, type or source" /></label></div>
    <div className="table-meta"><strong>{visible.length} {visible.length === 1 ? 'trade' : 'trades'}</strong><span>Values are displayed to 2 decimals; — means unavailable, not zero. Source records keep full precision.</span></div>
    {status && !reasonStatus && <p role="status">{status}</p>}
    <section className="sheet" aria-label="Captured trades"><div className="sheet-scroll">
      <table aria-label="Trades"><thead><tr><th scope="col">Market</th><th scope="col">Closed</th>
        <th scope="col">Type</th><th scope="col">Side</th><th scope="col" className="numeric">Position</th>
        <th scope="col" className="numeric">Exposure</th><th scope="col" className="numeric">Entry</th>
        <th scope="col" className="numeric">Exit</th><th scope="col" className="numeric">Gross P/L</th>
        <th scope="col" className="numeric">Fees</th><th scope="col" className="numeric">Net P/L</th>
        <th scope="col">Entry reason</th></tr></thead><tbody>
      {visible.map(t => <tr key={t.id} className={t.id === selected ? 'selected' : ''} onClick={() => choose(t)}>
        <td><button id={`trade-${t.id}`} className="market-button" onClick={() => choose(t)} aria-label={`Open details for ${t.market}`}>{t.market}</button></td>
        <td className="date-cell">{new Date(t.close_time).toLocaleDateString()}</td>
        <td>{t.product_type.replaceAll('_', ' ')}</td><td><span className={`side ${t.position_side}`}>{t.position_side}</span></td>
        <td className="numeric">{quantity(t.closed_quantity, t.quantity_unit)}</td>
        <td className="numeric">{money(t.position_notional_usd)}</td>
        <td className="numeric">{price(t.entry_price, t.price_currency)}</td><td className="numeric">{price(t.exit_price, t.price_currency)}</td>
        <td className={`numeric ${t.gross_pnl_usd === null ? 'muted' : Number(t.gross_pnl_usd) < 0 ? 'negative' : 'positive'}`}>{money(t.gross_pnl_usd)}</td>
        <td className="numeric">{money(t.fee_usd)}{t.fee_currency_assumed && t.fee_usd !== null ? <span title="USD fee currency assumed">*</span> : null}</td>
        <td className={`numeric ${(t.execution_net_pnl_usd ?? t.net_pnl_usd) === null ? 'muted' : Number(t.execution_net_pnl_usd ?? t.net_pnl_usd) < 0 ? 'negative' : 'positive'}`}>
          {money(t.execution_net_pnl_usd ?? t.net_pnl_usd)}{t.pnl_method === 'coinbase_orders_ex_funding' ? <span title="Calculated net after fees, excluding funding and settlement">†</span> : null}</td>
        <td className="reason-cell" title={t.reason ?? ''}>{t.reason || '—'}</td>
      </tr>)}
      </tbody></table></div>
      {trades.length > 0 && visible.length === 0 && <p className="sheet-message">No matching trades.</p>}
      {trades.length === 0 && !status && <p className="sheet-message">No trades in this view.</p>}
    </section>
    <div className="below-sheet"><span>* USD fee currency assumed, not confirmed by Coinbase.
      {trades.some(t => t.pnl_method === 'coinbase_orders_ex_funding') && <><br />† Net after fees excludes funding and settlement adjustments; calculated from Coinbase orders, not Coinbase-settled P/L. Gross P/L is also calculated from orders.</>}</span>
      {hasMore && <button className="secondary-button" onClick={more} disabled={loadingMoreView}>Load more trades</button>}</div>
    {active && <aside className="detail" role="region" aria-label="Trade details" onKeyDown={event => { if (event.key === 'Escape') closeDetails() }}>
      <div className="detail-heading"><div><span className="eyebrow">TRADE DETAILS</span><h2>{active.market}</h2></div>
        <button ref={closeButton} className="close-button" onClick={closeDetails} aria-label="Close details">×</button></div><dl>
        <dt>Source</dt><dd>{active.source_key}</dd><dt>Type</dt><dd>{active.product_type}</dd>
        <dt>Position</dt><dd>{active.position_side} {quantity(active.closed_quantity, active.quantity_unit)}</dd>
        <dt>Entry USD notional</dt><dd>{money(active.position_notional_usd)}</dd>
        <dt>Entry basis</dt><dd>{active.basis_status === 'cross_currency_unavailable'
          ? `Cross-currency basis unavailable (${active.basis_currency ?? 'unknown currency'})`
          : active.basis_status === 'unknown_transfer_basis' ? 'Transfer acquisition basis unavailable'
          : active.basis_status === 'known_quote_basis' ? `Source-priced in ${active.basis_currency ?? 'quote currency'}` : '—'}</dd>
        <dt>Valued at entry</dt><dd>{active.entry_time ? new Date(active.entry_time).toLocaleString() : '—'}</dd>
        <dt>Entry fill price</dt><dd>{price(active.entry_price, active.price_currency)}</dd>
        <dt>Exit fill price</dt><dd>{price(active.exit_price, active.price_currency)}</dd>
        <dt>{active.pnl_method === 'coinbase_orders_ex_funding' ? 'Gross P/L (calculated from orders)' : 'Gross P/L'}</dt><dd>{money(active.gross_pnl_usd)}</dd><dt>Fees</dt><dd>{active.fee_currency_assumed && active.fee_usd
          ? `${money(active.fee_usd)} USD assumed (Coinbase commission)` : money(active.fee_usd)}</dd>
        <dt>Funding</dt><dd>{money(active.funding_usd)}</dd>
        {active.pnl_method === 'coinbase_orders_ex_funding' && <><dt>Net after trading fees (excludes funding)</dt><dd>{money(active.execution_net_pnl_usd ?? null)} (calculated, not Coinbase-settled)</dd></>}
        <dt>Net P/L including funding</dt><dd>{money(active.net_pnl_usd)}</dd>
        <dt>Source-reported gross</dt><dd>{money(active.reported_gross_usd)}</dd>
        <dt>Source-reported net</dt><dd>{money(active.reported_net_usd)}</dd>
        <dt>Reconciliation</dt><dd>{active.reconciliation_status.replaceAll('_', ' ')}</dd>
      </dl><label>Entry reason<textarea value={reason} onChange={e => setReason(e.target.value)} maxLength={4000} /></label>
      <button className="primary-button" onClick={submit} disabled={saving}>Save reason</button>
      {reasonStatus && <p role="status" className="detail-status">{status}</p>}
    </aside>}
  </main>
}
