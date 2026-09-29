// frontend/src/App.test.tsx
import { afterEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, cleanup, within } from '@testing-library/react'
import App from './App'
import * as api from './api'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const trade: api.Trade = { id: 't1', source_key: 'mock.spot', account_id: 'demo', close_id: 'c1',
  product_type: 'spot', market: 'BTC-USD', position_side: 'long',
  entry_time: '2026-09-10T13:00:00+00:00',
  close_time: '2026-09-10T14:32:00+00:00', closed_quantity: '0.10', quantity_unit: 'BTC',
  position_notional_usd: '6000', gross_pnl_usd: '200', fee_usd: '12', funding_usd: '0',
  net_pnl_usd: '188', reported_gross_usd: null, reported_net_usd: null,
  reconciliation_status: 'not_available',
  reason: null, reason_revision: 0 }

it('shows a spreadsheet of trades with two-decimal position and fee displays', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [{ ...trade,
    closed_quantity: '1.2345', fee_usd: '0.878455', position_notional_usd: '6000.999' }], has_more: false })
  render(<App />)
  const table = await screen.findByRole('table', { name: 'Trades' })
  expect(table).toBeTruthy()
  expect(within(table).getAllByRole('columnheader')[0].textContent).toBe('Market')
  for (const heading of ['Closed', 'Market', 'Type', 'Side', 'Position', 'Entry', 'Exit', 'Gross P/L', 'Fees', 'Net P/L']) {
    expect(screen.getByRole('columnheader', { name: heading })).toBeTruthy()
  }
  expect(screen.getByRole('region', { name: 'Visible total profit and loss' }).textContent).toContain('$188.00')
  const row = screen.getByRole('row', { name: /BTC-USD/ })
  expect(row.textContent).toContain('1.23 BTC')
  expect(row.textContent).toContain('$0.88')
  expect(screen.queryByRole('region', { name: 'Trade details' })).toBeNull()
  fireEvent.click(row)
  const details = screen.getByRole('region', { name: 'Trade details' })
  expect(within(details).getByText(/1\.23 BTC/)).toBeTruthy()
  expect(within(details).getByText('$6,001.00')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
  expect(screen.queryByRole('region', { name: 'Trade details' })).toBeNull()
})

it('totals only visible filtered trades and excludes unavailable P/L', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade,
    { ...trade, id: 't2', market: 'ETH-USD', net_pnl_usd: '-50.25' },
    { ...trade, id: 't3', market: 'SOL-USD', net_pnl_usd: null }], has_more: false })
  render(<App />)
  await screen.findByText('SOL-USD')
  expect(screen.getByRole('region', { name: 'Visible total profit and loss' }).textContent).toContain('$137.75')
  fireEvent.change(screen.getByPlaceholderText('Market, type or source'), { target: { value: 'ETH' } })
  expect(screen.getByRole('region', { name: 'Visible total profit and loss' }).textContent).toContain('-$50.25')
})

it('moves keyboard focus into details and back to the trade on Escape', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade], has_more: false })
  render(<App />)
  const opener = await screen.findByRole('button', { name: 'Open details for BTC-USD' })
  opener.focus()
  fireEvent.click(opener)
  const close = screen.getByRole('button', { name: 'Close details' })
  await waitFor(() => expect(document.activeElement).toBe(close))
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(screen.queryByRole('region', { name: 'Trade details' })).toBeNull()
  expect(document.activeElement).toBe(opener)
})

it('shows sourced columns and saves only reason', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade], has_more: false })
  vi.spyOn(api, 'saveReason').mockResolvedValue({ ...trade, reason: 'Breakout', reason_revision: 1 })
  render(<App />)
  expect((await screen.findAllByText('BTC-USD')).length).toBeGreaterThan(0)
  expect(api.fetchTrades).toHaveBeenCalledWith(0, true)
  expect(screen.getByText('$6,000.00')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Open details for BTC-USD' }))
  fireEvent.change(screen.getByLabelText('Entry reason'), { target: { value: 'Breakout' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  await waitFor(() => expect(api.saveReason).toHaveBeenCalledWith('t1', 'Breakout', 0))
  expect(await within(screen.getByRole('region', { name: 'Trade details' })).findByText('Reason saved.')).toBeTruthy()
  expect(screen.queryByLabelText('Net P/L')).toBeNull()
})

it('shows an empty state', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [], has_more: false })
  render(<App />)
  expect(await screen.findByText('No captured trades in this view.')).toBeTruthy()
})

it('labels unknown cross-currency basis without inventing profit', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [{ ...trade,
    basis_status: 'cross_currency_unavailable', basis_currency: 'USDC',
    position_notional_usd: null, gross_pnl_usd: null, net_pnl_usd: null }], has_more: false })
  render(<App />)
  await screen.findAllByText('BTC-USD')
  fireEvent.click(screen.getByRole('button', { name: 'Open details for BTC-USD' }))
  expect(screen.getByText(/Cross-currency basis unavailable/)).toBeTruthy()
  expect(screen.getByText(/USDC/)).toBeTruthy()
})

it('loads older history rather than silently hiding it', async () => {
  vi.spyOn(api, 'fetchTrades')
    .mockResolvedValueOnce({ items: [trade], has_more: true })
    .mockResolvedValueOnce({ items: [{ ...trade, id: 't2', market: 'ETH-USD' }], has_more: false })
  render(<App />)
  await screen.findByText('Load more trades')
  fireEvent.click(screen.getByText('Load more trades'))
  expect(await screen.findByText('ETH-USD')).toBeTruthy()
  expect(api.fetchTrades).toHaveBeenLastCalledWith(1, true)
})

it('defaults to the last 30 days and can show all saved history', async () => {
  const older = { ...trade, id: 'old', market: 'ETH-USD' }
  vi.spyOn(api, 'fetchTrades')
    .mockResolvedValueOnce({ items: [trade], has_more: false })
    .mockResolvedValueOnce({ items: [trade, older], has_more: false })
  render(<App />)
  await screen.findAllByText('BTC-USD')
  expect(screen.queryByText('ETH-USD')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Show all history' }))
  expect(await screen.findByText('ETH-USD')).toBeTruthy()
  expect(api.fetchTrades).toHaveBeenLastCalledWith(0, false)
  expect(screen.getByRole('button', { name: 'Last 30 days' })).toBeTruthy()
})

it('shows fill prices and labels assumed USD commissions as an assumption', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [{ ...trade, product_type: 'dated_future',
    entry_price: '321.9', exit_price: '332.65', price_currency: 'USD',
    fee_usd: '0.878455', fee_currency_assumed: true }], has_more: false })
  render(<App />)
  expect(await screen.findByText('321.9 USD')).toBeTruthy()
  expect(screen.getByText('332.65 USD')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Open details for BTC-USD' }))
  expect(within(screen.getByRole('region', { name: 'Trade details' })).getByText(/\$0\.88.*USD assumed/)).toBeTruthy()
})

it('shows order-derived gross and net after fees without claiming full settled net', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [{ ...trade, product_type: 'dated_future',
    gross_pnl_usd: '43.9', net_pnl_usd: null, execution_net_pnl_usd: '41.826195',
    pnl_method: 'coinbase_orders_ex_funding', funding_usd: null,
    fee_usd: '2.073805', fee_currency_assumed: true }], has_more: false })
  render(<App />)
  const row = await screen.findByRole('row', { name: /BTC-USD/ })
  expect(row.textContent).toContain('$43.90')
  expect(row.textContent).toContain('$41.83')
  expect(screen.getByText(/Net after fees.*excludes funding/)).toBeTruthy()
  fireEvent.click(row)
  const details = screen.getByRole('region', { name: 'Trade details' })
  expect(within(details).getByText(/\$41\.83/)).toBeTruthy()
  expect(within(details).getByText(/not Coinbase-settled/)).toBeTruthy()
  expect(within(details).getByText('Net P/L including funding').nextElementSibling?.textContent).toBe('—')
})

it('ignores overlapping load-more clicks and duplicate returned rows', async () => {
  let resolvePage!: (value: api.TradePage) => void
  vi.spyOn(api, 'fetchTrades')
    .mockResolvedValueOnce({ items: [trade], has_more: true })
    .mockImplementationOnce(() => new Promise(resolve => { resolvePage = resolve }))
  render(<App />)
  await screen.findByText('Load more trades')
  fireEvent.click(screen.getByText('Load more trades'))
  fireEvent.click(screen.getByText('Load more trades'))
  expect(api.fetchTrades).toHaveBeenCalledTimes(2)
  await act(async () => resolvePage({ items: [trade, { ...trade, id: 't2', market: 'ETH-USD' }], has_more: false }))
  expect(screen.getAllByRole('button', { name: /BTC-USD/ })).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: /ETH-USD/ })).toHaveLength(1)
})

it('reports API failure without inventing trade rows', async () => {
  vi.spyOn(api, 'fetchTrades').mockRejectedValue(new Error('offline'))
  render(<App />)
  expect(await screen.findByText('Could not load trades.')).toBeTruthy()
  expect(screen.queryByText('BTC-USD')).toBeNull()
})

it('shows a failed reason save without claiming success', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade], has_more: false })
  vi.spyOn(api, 'saveReason').mockRejectedValue(new Error('conflict'))
  render(<App />)
  await screen.findAllByText('BTC-USD')
  fireEvent.click(screen.getByRole('button', { name: 'Open details for BTC-USD' }))
  fireEvent.change(screen.getByLabelText('Entry reason'), { target: { value: 'My thesis' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  expect(await screen.findByText(/Could not save reason/)).toBeTruthy()
  expect((screen.getByLabelText('Entry reason') as HTMLTextAreaElement).value).toBe('My thesis')
})

it('does not attribute a late save to a newly selected trade', async () => {
  const other = { ...trade, id: 't2', market: 'ETH-USD', reason: 'ETH thesis' }
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade, other], has_more: false })
  let resolveSave!: (value: api.Trade) => void
  vi.spyOn(api, 'saveReason').mockImplementation(() => new Promise(resolve => { resolveSave = resolve }))
  render(<App />)
  await screen.findAllByText('BTC-USD')
  fireEvent.click(screen.getByRole('button', { name: 'Open details for BTC-USD' }))
  fireEvent.change(screen.getByLabelText('Entry reason'), { target: { value: 'BTC thesis' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  fireEvent.click(screen.getByRole('button', { name: /ETH-USD/ }))
  await act(async () => resolveSave({ ...trade, reason: 'BTC thesis', reason_revision: 1 }))
  expect((screen.getByLabelText('Entry reason') as HTMLTextAreaElement).value).toBe('ETH thesis')
  expect(screen.queryByText('Reason saved.')).toBeNull()
})
