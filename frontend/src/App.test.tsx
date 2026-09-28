// frontend/src/App.test.tsx
import { afterEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
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

it('shows sourced columns and saves only reason', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade], has_more: false })
  vi.spyOn(api, 'saveReason').mockResolvedValue({ ...trade, reason: 'Breakout', reason_revision: 1 })
  render(<App />)
  expect((await screen.findAllByText('BTC-USD')).length).toBeGreaterThan(0)
  expect(screen.getByText('$6,000.00')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Breakout' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  await waitFor(() => expect(api.saveReason).toHaveBeenCalledWith('t1', 'Breakout', 0))
  expect(screen.queryByLabelText('Net P/L')).toBeNull()
})

it('shows an empty state', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [], has_more: false })
  render(<App />)
  expect(await screen.findByText('No captured trades yet.')).toBeTruthy()
})

it('labels unknown cross-currency basis without inventing profit', async () => {
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [{ ...trade,
    basis_status: 'cross_currency_unavailable', basis_currency: 'USDC',
    position_notional_usd: null, gross_pnl_usd: null, net_pnl_usd: null }], has_more: false })
  render(<App />)
  await screen.findAllByText('BTC-USD')
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
  expect(api.fetchTrades).toHaveBeenLastCalledWith(1)
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
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'My thesis' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  expect(await screen.findByText(/Could not save reason/)).toBeTruthy()
  expect((screen.getByLabelText('Reason') as HTMLTextAreaElement).value).toBe('My thesis')
})

it('does not attribute a late save to a newly selected trade', async () => {
  const other = { ...trade, id: 't2', market: 'ETH-USD', reason: 'ETH thesis' }
  vi.spyOn(api, 'fetchTrades').mockResolvedValue({ items: [trade, other], has_more: false })
  let resolveSave!: (value: api.Trade) => void
  vi.spyOn(api, 'saveReason').mockImplementation(() => new Promise(resolve => { resolveSave = resolve }))
  render(<App />)
  await screen.findAllByText('BTC-USD')
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'BTC thesis' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
  fireEvent.click(screen.getByRole('button', { name: /ETH-USD/ }))
  await act(async () => resolveSave({ ...trade, reason: 'BTC thesis', reason_revision: 1 }))
  expect((screen.getByLabelText('Reason') as HTMLTextAreaElement).value).toBe('ETH thesis')
  expect(screen.queryByText('Reason saved.')).toBeNull()
})
