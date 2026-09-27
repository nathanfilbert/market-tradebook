// frontend/src/api.test.ts
import { afterEach, expect, it, vi } from 'vitest'
import { fetchTrades, saveReason } from './api'

afterEach(() => vi.unstubAllGlobals())

it('reads a page and sends only reason plus revision', async () => {
  const calls: Array<[string, RequestInit | undefined]> = []
  vi.stubGlobal('fetch', async (url: string, options?: RequestInit) => {
    calls.push([url, options])
    return { ok: true, json: async () => url === '/api/trades?offset=0&limit=100' ?
      { items: [], has_more: false } : { id: 't', reason: 'why', reason_revision: 1 } }
  })
  expect(await fetchTrades()).toEqual({ items: [], has_more: false })
  await saveReason('t', 'why', 0)
  expect(calls[1][0]).toBe('/api/trades/t/reason')
  expect(JSON.parse(calls[1][1]?.body as string)).toEqual({ reason: 'why', expected_revision: 0 })
})
