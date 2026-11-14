import { useEffect, useState, useSyncExternalStore } from 'react'
import { SCENE, SCENES } from './scenes.js'
import { STEPS, enterHref, enterSite, loadDatabase } from './loader.js'

/* ---------------------------------------------------------------------------
   ResaleWise SG: the loading intro.
   Photographs of Singapore's estates take over the screen one after another, each with a
   short headline, and the sequence goes round again for as long as the database takes. The
   moment it has loaded, the visitor is sent to the page they asked for.

   The loading is real: the counter is how many of the site's own API requests have succeeded
   (loader.js). Every motion is a function of the time into its scene (see index.css), so
   Debug: ?t=5 freezes the intro 5 s in (window.__goto(9) moves it), ?hold=1 keeps it from leaving by itself.
--------------------------------------------------------------------------- */

const MIN_SHOW = 5        // seconds: even a warm server gets this long before the redirect (set 0 to leave at once)
const LEAVE_DELAY = 0.3   // seconds after the database is ready, so the counter can reach 100 first

/* ------------------------------------------------- shared state, outside React */
const params = new URLSearchParams(window.location.search)
const freeze = (() => { const t = parseFloat(params.get('t')); return Number.isFinite(t) ? t : null })()
const hold = params.has('hold') || freeze !== null
const began = performance.now()
const store = {
  s: { phase: 'loading', done: 0, total: STEPS.length, current: STEPS[0].label, failed: [], frozenT: freeze },
  subs: new Set(),
  set(patch) { store.s = { ...store.s, ...patch }; store.subs.forEach((f) => f()) },
  sub(f) { store.subs.add(f); return () => store.subs.delete(f) },
}
const useStore = (select) => useSyncExternalStore(store.sub, () => select(store.s))
if (freeze !== null) window.__goto = (t) => store.set({ frozenT: t })

let token = 0
function startLoading() {
  const mine = ++token
  const startedAt = performance.now()
  store.set({ phase: 'loading', done: 0, total: STEPS.length, current: STEPS[0].label, failed: [] })
  if (freeze !== null) return
  loadDatabase((st) => {
    if (mine === token) store.set({ done: st.done, current: st.current, failed: st.failed })
  }, () => mine !== token).then((st) => {
    if (mine !== token) return
    const error = st.failed.length === st.total
    store.set({ phase: error ? 'error' : 'ready', done: st.done, failed: st.failed })
    if (error || hold) return
    // loaded: go straight to the page (once the intro has had its minimum, and the counter has reached 100)
    const wait = Math.max(0, MIN_SHOW - (performance.now() - startedAt) / 1000) + LEAVE_DELAY
    setTimeout(() => { if (mine === token) enterSite() }, wait * 1000)
  })
}
function onLinkClick(e) {          // a plain click leaves at once; a ctrl/middle click is left to the browser (new tab)
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return
  e.preventDefault()
  enterSite()
}

/* ---------------------------------------------------------------- the scenes */
/* each word of a headline rises through its own mask; `from` keeps the stagger running across both lines */
function Words({ text, from = 0 }) {
  return text.split(' ').map((word, i) => (
    <span key={i}><span className="mask"><span className="w" style={{ '--i': from + i }}>{word}</span></span>{' '}</span>
  ))
}

function Photo({ scene }) {
  return (
    <img
      className="photo" src={scene.src} alt="" decoding="async"
      style={{ objectPosition: scene.pos }}
      ref={(el) => { if (el?.complete) el.classList.add('ready') }}
      onLoad={(e) => e.currentTarget.classList.add('ready')}
    />
  )
}

function Layer({ n, ts, leaving }) {
  const scene = SCENES[n % SCENES.length]
  const lead = scene.lead.split(' ').length
  return (
    <div className={`layer enter${leaving ? ' leave' : ''}`} style={{ '--ts': ts, '--lt': Math.max(0, ts - SCENE) }}>
      {scene.finale ? (
        <div className="copy absolute inset-x-5 top-[36%] sm:inset-x-12 sm:top-[32%]">
          <h2 className="cutout" style={{ backgroundImage: `url(${scene.src})` }}>{scene.lead}</h2>
          <p className="h-tail mt-2 !text-[clamp(1.6rem,4vw,3rem)]">{scene.tail}</p>
        </div>
      ) : (
        <>
          <Photo scene={scene} />
          <div className="scrim" />
          <div className="copy absolute left-5 top-[24%] max-w-[min(46rem,90vw)] sm:left-12 sm:top-[28%]">
            <h2>
              <span className="h-lead block"><Words text={scene.lead} /></span>
              <span className="h-tail block"><Words text={scene.tail} from={lead} /></span>
            </h2>
            <p className="h-note mt-6">{scene.note}</p>
          </div>
        </>
      )}
      <p className="absolute bottom-2 right-5 max-w-[calc(100%-2.5rem)] text-right text-[10px] leading-snug text-[#f4ead0]/75 sm:right-12 sm:text-[11px]">{scene.credit}</p>
    </div>
  )
}

/* the scene number comes from the clock; the scene before it stays underneath until the new one has covered it */
function Stage() {
  const frozenT = useStore((st) => st.frozenT)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (freeze !== null) return undefined
    let id
    const step = () => {
      const t = (performance.now() - began) / 1000
      const n = Math.floor(t / SCENE)
      setTick(n)
      id = setTimeout(step, ((n + 1) * SCENE - t) * 1000 + 8)
    }
    step()
    SCENES.forEach((s) => { const i = new Image(); i.src = s.src })      // the next photographs are fetched while the first plays
    return () => clearTimeout(id)
  }, [])
  const frozen = freeze !== null
  const n = frozen ? Math.floor(frozenT / SCENE) : tick
  const ts = frozen ? frozenT - n * SCENE : 0
  return (
    <div className={`absolute inset-0 ${frozen ? 'frozen' : ''}`} aria-hidden="true">
      {n > 0 && <Layer key={n - 1} n={n - 1} ts={frozen ? ts + SCENE : 0} leaving />}   {/* live: leave its running animations alone, a changed delay would re-time them */}
      <Layer key={n} n={n} ts={ts} />
    </div>
  )
}

/* ----------------------------------------------------------------------- UI */
const SEGMENTS = {
  a: 'M10 3 L50 3 L44 10 L16 10 Z', b: 'M53 6 L53 46 L47 49 L47 13 Z', c: 'M53 54 L53 94 L47 87 L47 51 Z',
  d: 'M10 97 L50 97 L44 90 L16 90 Z', e: 'M7 54 L13 51 L13 87 L7 94 Z', f: 'M7 6 L13 13 L13 49 L7 46 Z',
  g: 'M10 50 L16 46 L44 46 L50 50 L44 54 L16 54 Z',
}
const DIGITS = { 0: 'abcdef', 1: 'bc', 2: 'abdeg', 3: 'abcdg', 4: 'bcfg', 5: 'acdfg', 6: 'acdefg', 7: 'abc', 8: 'abcdefg', 9: 'abcdfg' }

function Digit({ d }) {
  const lit = d === null ? '' : DIGITS[d]
  return (
    <svg viewBox="0 0 60 100" className="h-11 w-6 sm:h-12 sm:w-7" aria-hidden="true">
      {Object.entries(SEGMENTS).map(([k, path]) => (
        <path key={k} d={path} fill={lit.includes(k) ? '#ffb02e' : '#1e1611'} style={lit.includes(k) ? { filter: 'drop-shadow(0 0 4px rgba(255,176,46,.7))' } : undefined} />
      ))}
    </svg>
  )
}

/* the loading percentage (how much of the database is in), drawn like an HDB lift's floor indicator */
function LiftDisplay({ value }) {
  const s = String(value)
  const digits = [s.length > 2 ? Number(s[s.length - 3]) : null, s.length > 1 ? Number(s[s.length - 2]) : null, Number(s[s.length - 1])]
  return (
    <div className="inline-flex items-center gap-1 bg-[#120d0a] px-3 py-2 ring-1 ring-[#3a2a1c]" aria-hidden="true">
      {digits.map((d, i) => <Digit key={i} d={d} />)}
      <span className="ml-1 self-end pb-1.5 text-lg font-bold text-[#ffb02e]">%</span>
    </div>
  )
}

const LINK = 'underline underline-offset-4 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#ffb02e]'
const BTN_SOLID = 'whitespace-nowrap bg-[#f4ead0] px-5 py-3 font-semibold text-[#14163a] transition-colors hover:bg-[#ffb02e] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white'
const BTN_LINE = 'whitespace-nowrap border border-[#f4ead0]/40 px-5 py-3 font-semibold transition-colors hover:bg-[#f4ead0] hover:text-[#14163a] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white'

function Hud() {
  const s = useStore((st) => st)
  const loaded = s.done - s.failed.length
  const pct = Math.round((100 * loaded) / s.total)
  const loading = s.phase === 'loading'
  const error = s.phase === 'error'
  const announce = loading ? 'Loading database' : error ? 'Couldn’t reach the database' : 'Database ready'
  return (
    <>
      {/* one polite announcement per change of phase, not one per request */}
      <p className="sr-only" role="status" aria-live="polite">{announce}</p>
      <div className="absolute bottom-[4.5rem] left-5 w-[min(24rem,calc(100%-2.5rem))] sm:bottom-10 sm:left-12">
        {error ? (
          <div className="bg-[#14163a]/95 p-5">
            <p className="text-lg font-semibold leading-snug [text-wrap:balance]">Couldn’t Reach the Database</p>
            <p className="mt-1 text-sm text-[#f4ead0]/80">The server didn’t answer. Check that it’s running, or carry on to the site.</p>
            <div className="mt-4 flex flex-wrap gap-3">
              <a href={enterHref()} onClick={onLinkClick} className={BTN_SOLID}>Continue Anyway →</a>
              <button type="button" onClick={startLoading} className={BTN_LINE}>Try Again</button>
            </div>
          </div>
        ) : (
          <>
            <LiftDisplay value={pct} />
            <div className="mt-3 h-1 w-full overflow-hidden bg-[#f4ead0]/25">
              <div className="h-full w-full origin-left bg-[#ffb02e] transition-transform duration-300 motion-reduce:transition-none" style={{ transform: `scaleX(${pct / 100})` }} />
            </div>
            <p className="mt-3 text-sm font-semibold">{loading ? 'Loading Database…' : 'Database Ready'}</p>
            <p className="mt-0.5 text-sm tabular-nums text-[#f4ead0]/85">{loading ? `${s.current} · ${s.done} of ${s.total}` : 'Opening ResaleWise…'}</p>
            <p className="mt-2 flex flex-wrap gap-x-4 text-sm text-[#f4ead0]/85">
              <a href={enterHref()} onClick={onLinkClick} className={LINK}>Skip Intro</a>
              {hold && !loading && <button type="button" onClick={startLoading} className={LINK}>Replay</button>}
            </p>
          </>
        )}
      </div>
    </>
  )
}

export default function App() {
  useEffect(() => { startLoading() }, [])
  return (
    <main className="relative h-full w-full select-none overflow-hidden bg-[#14163a]">
      <h1 className="sr-only">ResaleWise SG: loading</h1>
      <Stage />
      <header className="pointer-events-none absolute left-5 top-5 flex items-center gap-3 sm:left-12 sm:top-8" aria-hidden="true">
        <span className="grid grid-cols-3 gap-0.5">
          {Array.from({ length: 9 }, (_, i) => <span key={i} className={`h-2 w-2 ${i === 4 ? 'bg-[#ec4b8c]' : 'bg-[#f4ead0]/45'}`} />)}
        </span>
        <span>
          <span className="block text-base font-bold leading-tight" translate="no">ResaleWise SG</span>
          <span className="block text-xs leading-tight text-[#f4ead0]/80">HDB resale prices, made clearer</span>
        </span>
      </header>
      <Hud />
    </main>
  )
}
