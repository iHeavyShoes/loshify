// "Get to know me" (click-through, no chat) and the optional Tune-in strip.
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

globalThis.document = { createElement: () => ({ remove() {} }), head: { append() {} } }

const sdk = await import('@hermes/plugin-sdk')
const { createStore, flowNext, tuneInView, demoView, DemoStrip, anchorsFromText, GetToKnowMe, TuneInStrip, FLOW_STAGES, FLOW_FAILED } = await import('../../desktop/plugin.js')
const withoutJob = body => { const { job, ...rest } = body || {}; return rest }

const ROUND = { reacted: 0, total: 2, complete: false, tracks: [
  { n: 1, title: 'A', artists: ['X'], uri: 'spotify:track:a', kind: 'familiar', reason: 'r', reaction: null },
  { n: 2, title: 'B', artists: ['Y'], uri: 'spotify:track:b', kind: 'stretch', reason: 'r', reaction: null }] }
const MEET = { ok: true, data: { ok: true, portrait: 'Right now you keep coming back to X.', portrait_by: 'llm', claims: [], calibration_ok: true, timing: { total: 900 } } }
const BUILD = { ok: true, data: { ok: true, playlists: [{ slot: 'Then', url: 'https://open.spotify.com/playlist/1', tracks: 15 },
  { slot: 'Next', url: 'https://open.spotify.com/playlist/2', tracks: 12 }],
slots: [{ slot: 'Then', ok: true }, { slot: 'Next', ok: true, short: true }], timing: { total: 30000 } } }

function fakeCtx(routes) {
  const calls = []
  return {
    calls,
    storage: { get: (k, d) => d, set() {} },
    rest: async (path, opts = {}) => {
      calls.push({ path, ...opts })
      const key = Object.keys(routes).sort((a, b) => b.length - a.length).find(k => path.startsWith(k))
      if (key === undefined) throw new Error('no route ' + path)
      const r = routes[key]
      return typeof r === 'function' ? r(opts.body, path) : r
    }
  }
}

const routes = (extra = {}) => ({
  '/flow/meet': MEET,
  '/flow/verdict': body => ({ ok: true, data: { verdict: body.verdict, ...(body.verdict === 'not_quite' ? { portrait: 'Revised.', portrait_by: 'llm' } : {}) } }),
  '/flow/build': BUILD,
  '/onboarding': { ok: true, data: { active: true, calibration: ROUND, anchors: [], note: '' } },
  '/rotation': { ok: true, data: { spec: null, presets: [{ id: 'then_now_next', name: 'Then · Now · Next', cadence: 'weekly', intent: 'x' }] } },
  '/setup': { ready: true, step: 'done', onboarded: true },
  '/tune-in': { ok: true, data: { eligible: true, available: false, line: 'Next tune-in available in 6 days' } },
  ...extra
})

beforeEach(() => { sdk.calls.notify.length = 0 })

test('flowNext is a pure state machine with readable errors', () => {
  assert.deepEqual(FLOW_STAGES, ['portrait', 'calibration', 'arrange', 'done'])
  const p = flowNext(null, 'meet', MEET)
  assert.equal(p.stage, 'portrait')
  assert.equal(p.calibrationReady, true)
  assert.equal(flowNext(p, 'verdict', { ok: true, data: { verdict: 'sounds_like_me' } }).stage, 'calibration')
  assert.equal(flowNext({ ...p, calibrationReady: false }, 'verdict', { ok: true, data: { verdict: 'sounds_like_me' } }).stage, 'arrange')
  const revised = flowNext(p, 'verdict', { ok: true, data: { verdict: 'not_quite', portrait: 'New' } })
  assert.equal(revised.stage, 'portrait')
  assert.equal(revised.revised, true)
  const done = flowNext(p, 'build', BUILD)
  assert.equal(done.stage, 'done')
  assert.equal(done.links.length, 2)
  assert.deepEqual(done.short, ['Next'])
  const failed = flowNext(p, 'build', { ok: true, data: { ok: false, error: 'Pick a card' } })
  assert.equal(failed.stage, 'portrait')
  assert.equal(failed.error, 'Pick a card')
  assert.equal(flowNext(p, 'meet', null).error, FLOW_FAILED.meet) // never a raw transport string
  assert.equal(flowNext(p, 'build', { ok: false, error: 'signal is aborted without reason' }).error, FLOW_FAILED.build)
  assert.equal(flowNext(p, 'build', { ok: true, data: { ok: false, error: 'Loshify failed unexpectedly (KeyError).' } }).error, FLOW_FAILED.build)
  assert.deepEqual(anchorsFromText(' Khruangbin \n\nMoondog, Air\nFour'), ['Khruangbin', 'Moondog', 'Air'])
})

test('click-through: start → portrait → Not quite with anchors → calibration → build, no chat', async () => {
  const ctx = fakeCtx(routes())
  const store = createStore(ctx)
  store.setup.set({ ready: true, step: 'done', onboarded: false })
  assert.equal(await store.flowStart(), true)
  assert.equal(store.flow.get().stage, 'portrait')
  assert.ok(JSON.stringify(GetToKnowMe({ store })).includes('Right now you keep coming back to X.'))
  store.anchorsDraft.set('Moondog\nAir')
  assert.equal(await store.flowVerdict('not_quite'), true)
  const verdict = ctx.calls.find(c => c.path === '/flow/verdict')
  assert.deepEqual(withoutJob(verdict.body), { verdict: 'not_quite', anchors: ['Moondog', 'Air'] })
  assert.match(verdict.body.job, /^[A-Za-z0-9_-]{8,64}$/)
  assert.equal(store.flow.get().portrait, 'Revised.')
  assert.ok(JSON.stringify(GetToKnowMe({ store })).includes('YOUR PORTRAIT, REVISED'))
  store.flowGo('calibration')
  assert.ok(JSON.stringify(GetToKnowMe({ store })).includes('never skips or removes'))
  store.flowGo('arrange')
  assert.equal(await store.flowBuild(), false) // nothing picked yet
  store.flowPreset.set('then_now_next')
  store.noteDraft.set('no christmas music')
  assert.equal(await store.flowBuild(), true)
  const build = ctx.calls.find(c => c.path === '/flow/build')
  assert.deepEqual(withoutJob(build.body), { preset: 'then_now_next', note: 'no christmas music' })
  assert.equal(store.flow.get().stage, 'done')
  const json = JSON.stringify(GetToKnowMe({ store }))
  assert.ok(json.includes('YOUR PLAYLISTS ARE READY') && json.includes('Still filling: Next'))
  assert.equal(sdk.calls.composer?.length || 0, 0)
})

test('own words instead of a preset; a failed step shows its error and can be retried', async () => {
  let fail = true
  const ctx = fakeCtx(routes({ '/flow/build': () => (fail ? { ok: false, error: 'Spotify is busy' } : BUILD) }))
  const store = createStore(ctx)
  await store.flowStart()
  store.flowGo('arrange')
  store.arrange.set('one list for cooking')
  assert.equal(await store.flowBuild(), false)
  assert.equal(store.flow.get().error, 'Spotify is busy')
  assert.equal(store.flow.get().stage, 'arrange')
  assert.ok(JSON.stringify(GetToKnowMe({ store })).includes('Try again'))
  fail = false
  assert.equal(await store.flowRetry(), true) // Try again: the same input
  const builds = ctx.calls.filter(c => c.path === '/flow/build')
  assert.deepEqual(withoutJob(builds.at(-1).body), { words: 'one list for cooking', note: '' })
  assert.notEqual(builds.at(-1).body.job, builds.at(-2).body.job) // a fresh job each try
})

test('tune-in strip: hidden before the first run, a quiet countdown, then a 5-pick round', async () => {
  assert.deepEqual(tuneInView({ ok: true, data: { eligible: false } }), { show: false })
  assert.deepEqual(tuneInView(null), { show: false })
  let available = false
  let round = null
  const ctx = fakeCtx(routes({
    '/tune-in/start': () => { round = ROUND; return { ok: true, data: { ok: true, round } } },
    '/tune-in/react': body => ({ ok: true, data: { reaction: body.reaction } }),
    '/tune-in/finish': () => { round = null; available = false; return { ok: true, data: { ok: true } } },
    '/tune-in': () => ({ ok: true, data: { eligible: true, available, line: available ? 'Tune in is available' : 'Next tune-in available in 6 days', ...(round ? { round } : {}) } })
  }))
  const store = createStore(ctx)
  await store.loadTuneIn()
  assert.ok(JSON.stringify(TuneInStrip({ store })).includes('Next tune-in available in 6 days'))
  available = true
  await store.loadTuneIn()
  assert.ok(JSON.stringify(TuneInStrip({ store })).includes('Tune in · 5 quick picks'))
  assert.equal(await store.startTuneIn(), true)
  assert.equal(await store.reactTuneIn(2, 'down'), true)
  assert.deepEqual(ctx.calls.find(c => c.path === '/tune-in/react').body, { n: 2, reaction: 'down' })
  assert.equal(store.tuneIn.get().reacted, 1)
  assert.ok(!ctx.calls.some(c => c.path.startsWith('/player') || c.path.startsWith('/feedback')))
  assert.equal(await store.finishTuneIn(), true)
  assert.ok(JSON.stringify(TuneInStrip({ store })).includes('Next tune-in available'))
})

test('demo strip (demo_mode): real last-refresh counts and the taste line, or nothing', async () => {
  assert.deepEqual(demoView(null), { show: false, lines: [] })
  assert.deepEqual(demoView({ ok: true, data: { on: false } }), { show: false, lines: [] })
  assert.deepEqual(demoView({ ok: true, data: { on: true } }), { show: false, lines: [] }) // on, but no data yet
  const full = demoView({ ok: true, data: { on: true,
    archive: { added: 14, expired: 9, keepers: 3, kept: 52 },
    taste: { line: 'Taste · leans → X, Y · 4 ❤️ · 1 👎' } } })
  assert.deepEqual(full.lines, ['Last Archive refresh: 14 added · 9 aged out · 3 keepers', 'Taste · leans → X, Y · 4 ❤️ · 1 👎'])
  assert.equal(demoView({ ok: true, data: { on: true, archive: { added: 1, expired: 0, keepers: 1 } } }).lines[0],
    'Last Archive refresh: 1 added · 0 aged out · 1 keeper')
  let on = false
  const ctx = fakeCtx(routes({
    '/demo': () => ({ ok: true, data: on ? { on: true, archive: { added: 2, expired: 1, keepers: 0 } } : { on: false } }),
    '/tune-in': { ok: true, data: { eligible: true, available: true, line: 'Tune in', demo: true } }
  }))
  const store = createStore(ctx)
  await store.loadTuneIn()
  await store.loadDemo()
  assert.equal(DemoStrip({ store }), null)
  on = true
  await store.loadDemo()
  assert.ok(JSON.stringify(DemoStrip({ store })).includes('2 added · 1 aged out · 0 keepers'))
  // a missing route (older backend) hides it instead of failing
  const old = createStore(fakeCtx(routes()))
  await old.loadDemo()
  assert.equal(DemoStrip({ store: old }), null)
})

// -- Finding 3: long steps are backend jobs; a client abort/timeout never shows an error ---------------

/** A job backend: POST {job} → running; each poll advances; `finish` is the step's answer. */
function jobRoutes({ finish = BUILD, polls = 2, postThrows = false, pollThrowsOnce = false, failWith = undefined } = {}) {
  const jobs = {}
  let pollThrew = false
  const start = kind => body => {
    jobs[body.job] = { id: body.job, kind, state: 'running', progress: 'Writing your playlists… 1/3', left: polls }
    if (postThrows) throw new Error('signal is aborted without reason')
    return { ok: true, data: { job: { ...jobs[body.job] } } }
  }
  return routes({
    '/flow/build': start('build'),
    '/flow/meet': start('meet'),
    '/flow/job': (_b, path) => {
      if (pollThrowsOnce && !pollThrew) { pollThrew = true; throw new Error('Failed to fetch') }
      const id = decodeURIComponent(path.split('id=')[1])
      const j = jobs[id]
      if (!j) return { ok: true, data: { id, state: 'unknown', progress: '' } }
      if (--j.left > 0) return { ok: true, data: { ...j, progress: `Writing your playlists… ${3 - j.left}/3` } }
      if (failWith !== undefined) return { ok: true, data: { ...j, state: 'failed', message: failWith } }
      return { ok: true, data: { ...j, state: 'done', result: finish } }
    }
  })
}

const BUILD_NAMED = { ok: true, data: { ok: true, playlists: [{ slot: 'Morning', name: 'Loshify · Morning', url: 'https://open.spotify.com/playlist/m', tracks: 15 }],
  slots: [{ slot: 'Morning', ok: true }] } }

async function toArrange(ctx) {
  const store = createStore(ctx, { pollMs: 0 })
  await store.flowStart()
  store.flowGo('arrange')
  store.flowPreset.set('then_now_next')
  return store
}

test('build as a job: progress while it runs, then the playlist cards with names, counts and links', async () => {
  const ctx = fakeCtx(jobRoutes({ finish: BUILD_NAMED, polls: 3 }))
  const store = await toArrange(ctx)
  const seen = []
  const rest = ctx.rest
  ctx.rest = (path, opts) => { if (path.startsWith('/flow/job')) seen.push(store.flow.get().progress); return rest(path, opts) }
  assert.equal(await store.flowBuild(), true)
  assert.equal(store.flow.get().stage, 'done')
  assert.equal(store.flow.get().error, null)
  const json = JSON.stringify(GetToKnowMe({ store }))
  for (const s of ['YOUR PLAYLISTS ARE READY', 'Loshify · Morning', '15 tracks · open in Spotify']) assert.ok(json.includes(s), s)
  assert.ok(ctx.calls.filter(c => c.path.startsWith('/flow/job')).length >= 2)
  assert.ok(seen.some(p => /Writing your playlists… \d\/3/.test(p)), seen.join('|'))
})

test('an aborted POST is not an error: the card polls and shows the result', async () => {
  const ctx = fakeCtx(jobRoutes({ postThrows: true, pollThrowsOnce: true }))
  const store = await toArrange(ctx)
  assert.equal(await store.flowBuild(), true)
  assert.equal(store.flow.get().stage, 'done')
  const json = JSON.stringify(GetToKnowMe({ store }))
  assert.ok(!/abort|signal|fetch/i.test(json), 'no raw transport text')
  assert.ok(json.includes('YOUR PLAYLISTS ARE READY'))
})

test('a build that truly failed server-side: one friendly line and Try again, never a raw error', async () => {
  const ctx = fakeCtx(jobRoutes({ failWith: null }))
  const store = await toArrange(ctx)
  assert.equal(await store.flowBuild(), false)
  assert.equal(store.flow.get().error, FLOW_FAILED.build)
  const json = JSON.stringify(GetToKnowMe({ store }))
  assert.ok(json.includes(FLOW_FAILED.build) && json.includes('Try again'))
  const plain = fakeCtx(jobRoutes({ failWith: 'Pick an arrangement first.' }))
  const s2 = await toArrange(plain)
  assert.equal(await s2.flowBuild(), false)
  assert.equal(s2.flow.get().error, 'Pick an arrangement first.') // the step's own plain reason
})

test('a start that never reached the backend (job unknown) ends in Try again, not an endless spinner', async () => {
  const ctx = fakeCtx(routes({
    '/flow/build': () => { throw new Error('signal is aborted without reason') },
    '/flow/job': (_b, path) => ({ ok: true, data: { id: path.split('id=')[1], state: 'unknown', progress: '' } })
  }))
  const store = await toArrange(ctx)
  assert.equal(await store.flowBuild(), false)
  assert.equal(store.flow.get().error, FLOW_FAILED.build)
  assert.equal(store.flow.get().working, null)
})

test('Taste → Start over: resets on the backend and runs Get to know me again in the Taste tab', async () => {
  const { TasteView } = await import('../../desktop/plugin.js')
  const ctx = fakeCtx(routes({ '/onboarding/restart': { ok: true, data: { active: false } } }))
  const store = createStore(ctx, { pollMs: 0 })
  store.setup.set({ ready: true, step: 'done', onboarded: true })
  await store.loadOnboarding?.()
  assert.ok(JSON.stringify(TasteView({ store })).includes('Re-run “Get to know me”'))
  assert.equal(await store.startOver(), true)
  assert.ok(ctx.calls.some(c => c.path === '/onboarding/restart' && c.method === 'POST'))
  assert.equal(store.flow.get().rerun, true)
  assert.ok(JSON.stringify(TasteView({ store })).includes('GET TO KNOW ME, AGAIN'))
  assert.equal(await store.flowStart(), true)
  assert.equal(store.flow.get().stage, 'portrait')
  store.closeRerun()
  assert.equal(store.flow.get().rerun, false)
})


test('a joined job is polled using the returned id', async () => {
  const ctx = fakeCtx(routes({
    '/flow/build': () => ({ ok: true, data: { job: { id: 'existing-job-1', state: 'running' } } }),
    '/flow/job': (_b, path) => path.endsWith('existing-job-1')
      ? { ok: true, data: { id: 'existing-job-1', state: 'done', result: BUILD_NAMED } }
      : { ok: true, data: { id: 'other', state: 'unknown' } }
  }))
  const store = await toArrange(ctx)
  assert.equal(await store.flowBuild(), true)
  assert.ok(ctx.calls.some(c => c.path === '/flow/job?id=existing-job-1'))
})

test('calibration navigation and build are disabled while saving', async () => {
  const store = await toArrange(fakeCtx(routes()))
  store.flowGo('calibration')
  store.busy.set(true)
  const calibration = JSON.stringify(GetToKnowMe({ store }))
  assert.match(calibration, /"disabled":true[^}]*"children":"(?:Next|Skip the rest)"/)
  assert.equal(store.flowGo('arrange'), false)
  store.busy.set(false)
  store.flowGo('arrange')
  store.busy.set(true)
  const arrangement = JSON.stringify(GetToKnowMe({ store }))
  assert.match(arrangement, /"disabled":true[^}]*"children":"Build my playlists"/)
})


test('tune in starts as a job and shows a friendly retry after failure', async () => {
  const ctx = fakeCtx(routes({
    '/tune-in': { ok: true, data: { eligible: true, available: true } },
    '/tune-in/start': body => ({ ok: true, data: { job: { id: body.job, state: 'running', progress: 'Finding picks' } } }),
    '/flow/job': () => ({ ok: true, data: { id: 'test-job', state: 'failed', message: 'signal is aborted' } })
  }))
  const store = createStore(ctx, { pollMs: 0 })
  await store.loadTuneIn()
  assert.equal(await store.startTuneIn(), false)
  const shown = JSON.stringify(TuneInStrip({ store }))
  assert.ok(shown.includes('Try again'))
  assert.ok(!shown.includes('signal is aborted'))
  assert.ok(ctx.calls.some(c => c.path.startsWith('/flow/job')))
})


test('successful tune in job polls and reloads the round', async () => {
  let started = false
  const ctx = fakeCtx(routes({
    '/tune-in': () => ({ ok: true, data: { eligible: true, available: !started } }),
    '/tune-in/start': body => ({ ok: true, data: { job: { id: body.job, state: 'running' } } }),
    '/flow/job': () => { started = true; return { ok: true, data: { id: 'job', state: 'done', result: { ok: true, data: { ok: true } } } } }
  }))
  const store = createStore(ctx, { pollMs: 0 })
  await store.loadTuneIn()
  assert.equal(await store.startTuneIn(), true)
  assert.ok(ctx.calls.some(c => c.path.startsWith('/flow/job')))
  assert.equal(ctx.calls.filter(c => c.path === '/tune-in').length, 2)
})
