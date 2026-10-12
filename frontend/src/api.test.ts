// frontend/src/api.test.ts
import { afterEach, expect, it, vi } from 'vitest'
import { fetchTrades, saveReason } from './api'

afterEach(() => vi.unstubAllGlobals())

it('reads a page and sends only reason plus revision', async () => {
  const calls: Array<[string, RequestInit | undefined]> = []
  vi.stubGlobal('fetch', async (url: string, options?: RequestInit) => {
    calls.push([url, options])
    return { ok: true, json: async () => url === '/api/trades?offset=0&limit=100&recent=true' ?
      { items: [], has_more: false } : { id: 't', reason: 'why', reason_revision: 1 } }
  })
  expect(await fetchTrades()).toEqual({ items: [], has_more: false })
  expect(calls[0][0]).toBe('/api/trades?offset=0&limit=100&recent=true')
  await saveReason('t', 'why', 0)
  expect(calls[1][0]).toBe('/api/trades/t/reason')
  expect(JSON.parse(calls[1][1]?.body as string)).toEqual({ reason: 'why', expected_revision: 0 })
})

it('sends only notes and its revision', async () => {
  const { saveNotes } = await import('./api')
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetch)
  await saveNotes('trade/id', 'Review', 2)
  expect(fetch.mock.calls[0][0]).toBe('/api/trades/trade%2Fid/notes')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ notes: 'Review', expected_revision: 2 })
})
