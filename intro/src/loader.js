/* The intro's loading is real: these are the requests the site's pages make on first
   use, so the database is woken up, the server's caches are warm, and (where the
   URLs match the homepage's exactly) the browser keeps the answers for a few minutes. */

const isLocal = ['localhost', '127.0.0.1', ''].includes(window.location.hostname)
// same rule as assets/js/config.js: from localhost the API is on :5000, elsewhere it is the same origin
export const API = window.RW_API_BASE || (isLocal && window.location.port !== '5000' && !import.meta.env.DEV ? 'http://localhost:5000' : '')

export const STEPS = [
  { label: 'Towns and flat types', url: '/api/options' },
  { label: 'Data freshness', url: '/api/meta' },
  { label: 'Market summary', url: '/api/insights/summary?flat_type=4%20ROOM&months=24' },
  { label: 'Town medians', url: '/api/insights/town-medians?flat_type=4+ROOM&months=12' },
  { label: 'Nearby places', url: '/api/amenities/status' },
  { label: 'Best-fit example', url: '/api/amenities/rank?flat_type=4+ROOM&months=24&limit=1' },
  { label: 'Market trend', url: '/api/insights/trend?flat_type=4%20ROOM&months=24' },
  { label: 'Recent sales', url: '/api/search?limit=20&page=1' },
  { label: 'Search statistics', url: '/api/search-stats' },
]

/* Runs the requests three at a time (the API rate-limits). A failed request counts as
   finished, so one slow endpoint never hangs the intro, and the failures are reported. */
export async function loadDatabase(onUpdate, isStale = () => false) {
  const state = { done: 0, total: STEPS.length, failed: [], current: STEPS[0].label }
  let next = 0
  async function worker() {
    while (next < STEPS.length && !isStale()) {
      const step = STEPS[next++]
      state.current = step.label
      onUpdate({ ...state })
      const ctl = new AbortController()
      const timer = setTimeout(() => ctl.abort(), 25000)
      try {
        const r = await fetch(API + step.url, { signal: ctl.signal })
        if (!r.ok) throw new Error(String(r.status))
        await r.json()                                   // read the whole answer
      } catch {
        state.failed.push(step.label)
      } finally {
        clearTimeout(timer)
      }
      state.done += 1
      if (!isStale()) onUpdate({ ...state })
    }
  }
  await Promise.all([worker(), worker(), worker()])
  return state
}

/* where to go afterwards: only a plain page name, so the intro can never be used to send someone elsewhere */
export function nextPage() {
  const n = new URLSearchParams(window.location.search).get('next') || ''
  return /^[A-Za-z0-9_-]+\.html$/.test(n) ? n : 'index.html'
}

/* leave the intro. ?nointro=1 stops the homepage sending us straight back, even if storage is blocked. */
export function enterSite() {
  try { window.sessionStorage.setItem('rw_intro', '1') } catch { /* private mode: ?nointro=1 still protects us */ }
  window.location.replace(`../../${nextPage()}?nointro=1`)
}

/* the address of the page we are heading to, for real links (so ctrl-click and middle-click work) */
export const enterHref = () => `../../${nextPage()}?nointro=1`
