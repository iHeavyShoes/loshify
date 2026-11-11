// Run: node --import ./tests/js/register.mjs --test 'tests/js/*.test.mjs'
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

globalThis.document = { createElement: () => ({ remove() {} }), head: { append() {} } }

const sdk = await import('@hermes/plugin-sdk')
const mod = await import('../../desktop/plugin.js')
const {
  cardFromPlayer, feedbackBody, describeDislike, createStore, fmtTime, playlistCount, setupView, SetupCard,
  REACTIONS, REACTION_ORDER, KEY_LEGEND, MEET_PROMPT, SETTINGS_ROUTE, SETUP_DEFAULTS, SETUP_POLL_MS, CSS,
  SETUP_STEPS, MANUAL_STEPS, SETUP_FIXES, SIGNIN_TROUBLE, CLIENT_ID_ERRORS, clientIdError,
  arrangePrompt, meetPrompt, rotationView, RotationSection, ArrangeBox, GetToKnowMe, ARRANGE_PLACEHOLDER, ARRANGE_MAX, REFRESH_PROMPT
} = mod
const plugin = mod.default
const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')

const TRACK = { track_uri: 'spotify:track:4uLU6hMCjMI75M1A2tKUQC', title: 'Song', artists: ['Artist'], album: null, progress_ms: 1234, is_playing: true }
const READY = { ready: true, step: 'done', onboarded: true }

function fakeCtx(routes, extra = {}) {
  const calls = []
  const stored = {}
  return {
    calls,
    stored,
    storage: { get: (k, d) => (k in stored ? stored[k] : d), set: (k, v) => { stored[k] = v } },
    os: { writeClipboard: async () => true, openExternal: async () => true },
    rest: async (path, opts = {}) => {
      calls.push({ path, ...opts })
      const key = Object.keys(routes).sort((a, b) => b.length - a.length).find(k => path.startsWith(k))
      if (key === undefined) throw new Error('no route ' + path)
      const r = routes[key]
      return typeof r === 'function' ? r(opts.body, path) : r
    },
    ...extra
  }
}

beforeEach(() => {
  sdk.calls.notify.length = 0
  sdk.calls.navigate.length = 0
  sdk.calls.submit.length = 0
  sdk.composer.accept = true
})

// ---------------------------------------------------------------------------
// Identity, genericity, SDK-only
// ---------------------------------------------------------------------------

test('identity: Loshify, id loshify, emoji reactions love/okay/down', () => {
  assert.equal(plugin.id, 'loshify')
  assert.equal(plugin.name, 'Loshify')
  assert.deepEqual(REACTION_ORDER, ['down', 'okay', 'love'])
  assert.deepEqual(REACTION_ORDER.map(r => REACTIONS[r].emoji), ['👎', '👌', '❤️'])
  assert.deepEqual(KEY_LEGEND.slice(0, 3).map(([icon]) => icon), ['👎', '👌', '❤️'])
  for (const old of ['heart', 'up', 'never']) assert.equal(old in REACTIONS, false)
})

test('generic: no personal names, playlist ids or paths left from v0.3', () => {
  for (const banned of [/feedback\.jsonl/, /profiles\//, /plugins\.spotify/, /[\u0590-\u05FF]/]) {
    assert.doesNotMatch(SOURCE, banned, String(banned))
  }
})

test('SDK surface only: imports, no app markup, no bridge, no dynamic code', () => {
  const imports = [...SOURCE.matchAll(/^import[^'"]*['"]([^'"]+)['"]/gm)].map(m => m[1])
  assert.deepEqual([...new Set(imports)].sort(), ['@hermes/plugin-sdk', 'react/jsx-runtime'])
  assert.doesNotMatch(SOURCE, /\[data-(?:slot|tour|sidebar|testid|tree-|pane-host|titlebar-cluster)/)
  assert.doesNotMatch(SOURCE, /hermesDesktop|querySelector|MutationObserver|\beval\(|new Function\(|\bimport\(/)
  assert.doesNotMatch(CSS, /data-slot/)
})

test('register: status-bar chip + four keybinds, all namespaced loshify', () => {
  const regs = []
  const ctx = { ...fakeCtx({ '/setup': READY, '/player': { ok: true, track: null } }), onDispose() {}, setInterval() {}, register: r => regs.push(r) }
  plugin.register(ctx)
  assert.equal(regs.filter(r => r.area === 'statusBar.right').length, 1)
  const keys = regs.filter(r => r.area === 'keybinds').map(r => r.data.id)
  assert.deepEqual(keys, ['loshify.love', 'loshify.okay', 'loshify.down', 'loshify.open'])
})

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

test('cardFromPlayer modes', () => {
  assert.equal(cardFromPlayer({ ok: true, track: TRACK }).mode, 'now')
  assert.equal(cardFromPlayer({ ok: true, track: { ...TRACK, is_playing: false } }).mode, 'paused')
  assert.equal(cardFromPlayer({ ok: true, track: null, last: TRACK }).mode, 'last')
  assert.equal(cardFromPlayer({ ok: false, error: 'x' }).mode, 'error')
  assert.equal(cardFromPlayer({ ok: true, track: null }).mode, 'none')
})

test('feedbackBody sends only the shown track uri', () => {
  const b = feedbackBody({ track: TRACK }, 'love', '  a note  ')
  assert.deepEqual(b, { track_uri: TRACK.track_uri, reaction: 'love', note: 'a note', amends: null })
  assert.equal(feedbackBody({ track: TRACK }, 'heart', ''), null)
  assert.equal(feedbackBody({ track: TRACK }, 'never', ''), null)
  assert.equal(feedbackBody({ track: null }, 'love', ''), null)
})

test('describeDislike covers managed-only outcomes', () => {
  assert.match(describeDislike('S', { removal: { status: 'removed', playlist_name: 'Deep Dig' }, skip: { status: 'skipped' } }), /removed from “Deep Dig”.*skipped/)
  assert.match(describeDislike('S', { removal: { status: 'not_managed' }, skip: { status: 'track_changed' } }), /only edits playlists it made.*already changed/)
  assert.match(describeDislike('S', { removal: { status: 'error', error: '503' }, skip: { status: 'skipped' } }), /removal failed: 503/)
  assert.match(describeDislike('S', { removal: { status: 'not_playlist' }, skip: { status: 'off' } }), /^👎 Saved: S$/)
  assert.match(describeDislike('S', { removal: { status: 'off' }, skip: { status: 'off' } }), /^👎 Saved: S$/)
})

test('helpers', () => {
  assert.equal(fmtTime(61234), '1:01')
  assert.equal(fmtTime(0), '0:00')
  assert.equal(playlistCount({ items: { total: 7 } }), 7)
  assert.equal(playlistCount({ tracks: { total: 3 } }), 3)
  assert.equal(playlistCount({}), null)
})

// ---------------------------------------------------------------------------
// Setup wizard (7 steps, one open at a time)
// ---------------------------------------------------------------------------

const ORDER = ['dashboard', 'app', 'users', 'client_id', 'connect', 'check', 'meet']
const statuses = v => ORDER.map(id => v.status[id]).join(' ')
const openSteps = v => ORDER.filter(id => v.status[id] === 'current' || v.status[id] === 'error')

test('wizard: steps, numbering, user-ticked steps', () => {
  assert.deepEqual(SETUP_STEPS.map(([id]) => id), ORDER)
  assert.deepEqual(SETUP_STEPS.map(([, n]) => n), ['1', '2', '3', '4', '5', '6', '7'])
  assert.deepEqual(MANUAL_STEPS, ['dashboard', 'app', 'users'])
})

test('wizard state machine: no Client ID yet → steps 1–3 follow the user\'s ticks, then step 4', () => {
  const s = { ready: false, step: 'client_id' }
  assert.equal(statuses(setupView(s)), 'current todo todo todo todo todo todo')
  assert.equal(statuses(setupView(s, false, { dashboard: true })), 'done current todo todo todo todo todo')
  assert.equal(statuses(setupView(s, false, { dashboard: true, app: true })), 'done done current todo todo todo todo')
  const v = setupView(s, false, { dashboard: true, app: true, users: true })
  assert.equal(statuses(v), 'done done done current todo todo todo')
  assert.equal(v.open, 'client_id')
  assert.equal(v.doneCount, 3)
  assert.equal(v.total, 7)
  assert.equal(v.show, true)
  assert.equal(setupView(s, false, { users: true }).open, 'dashboard') // earliest unticked step opens
})

test('wizard state machine: the backend wins — a saved Client ID means 1–4 are done, whatever was ticked', () => {
  const v = setupView({ ready: false, step: 'signin' }, false, {})
  assert.equal(statuses(v), 'done done done done current todo todo')
  assert.equal(v.open, 'connect')
  assert.equal(v.problem, null)
})

test('wizard state machine: connected → 1–6 done, step 7 open until onboarded or "Later"', () => {
  const v = setupView({ ready: true, step: 'done', onboarded: false })
  assert.equal(statuses(v), 'done done done done done done current')
  assert.equal(v.show, true)
  assert.equal(setupView({ ready: true, step: 'done', onboarded: false }, true).show, false) // "Later"
  const all = setupView(READY)
  assert.equal(statuses(all), 'done done done done done done done')
  assert.equal(all.show, false)
  assert.equal(all.open, null)
})

test('wizard state machine: exactly one step is open in every state', () => {
  const cases = [
    [{ ready: false, step: 'client_id' }, {}], [{ ready: false, step: 'client_id' }, { dashboard: true, app: true, users: true }],
    [{ ready: false, step: 'signin' }, {}], [{ ready: false, step: 'app' }, {}], [{ ready: false, step: 'premium_or_users' }, {}],
    [{ ready: false, step: 'internal' }, {}], [{ ready: false, step: 'retry' }, {}], [{ ready: true, step: 'done', onboarded: false }, {}]
  ]
  for (const [s, marks] of cases) assert.equal(openSteps(setupView(s, false, marks)).length, 1, JSON.stringify(s))
  assert.equal(openSteps(setupView(null)).length, 0)
  assert.equal(setupView(null).step, 'loading')
})

test('wizard: each failed check opens step 6 with its plain-language fix', () => {
  const cases = [['app', 'refused'], ['premium_or_users', 'not_listed'], ['internal', 'hermes'], ['retry', 'no_answer']]
  for (const [step, kind] of cases) {
    const v = setupView({ ready: false, step })
    assert.equal(statuses(v), 'done done done done done error todo', step)
    assert.equal(v.problemKind, kind, step)
    assert.equal(v.problem, SETUP_FIXES[kind], step)
  }
  // the backend's own copy wins over the built-in one
  assert.equal(setupView({ ready: false, step: 'app', problem: 'refused', fix: 'Server says X' }).problem, 'Server says X')
  assert.equal(setupView({ ready: false, step: 'app', fixes: { refused: 'Newer copy' } }).problem, 'Newer copy')
})

test('wizard: connected without Premium → step 6 done with a note about playback', () => {
  const v = setupView({ ready: true, step: 'done', onboarded: false, problem: 'no_premium', note: SETUP_FIXES.no_premium })
  assert.equal(v.status.check, 'warn')
  assert.equal(v.problemKind, 'no_premium')
  assert.match(v.problem, /no Premium/)
  assert.match(v.problem, /play, pause and skip don't/)
})

test('wizard: Loshify backend offline → says so, opens nothing', () => {
  const v = setupView({ ready: false, step: 'offline', next_step: "Loshify's backend did not answer (x)." })
  assert.equal(v.problemKind, 'offline')
  assert.match(v.problem, /backend did not answer/)
  assert.equal(v.open, null)
})

test('wizard error copy: a plain fix for each known failure, no jargon', () => {
  assert.match(SETUP_FIXES.redirect, /exactly http:\/\/127\.0\.0\.1:43828\/callback/)
  assert.match(SETUP_FIXES.redirect, /not "localhost"/)
  assert.match(SETUP_FIXES.redirect, /not secure/)
  assert.match(SETUP_FIXES.not_listed, /User Management/)
  assert.match(SETUP_FIXES.port_busy, /port on this computer is busy/)
  assert.match(SETUP_FIXES.no_premium, /no Premium/)
  assert.match(SETUP_FIXES.refused, /Client ID/)
  assert.match(SETUP_FIXES.no_answer, /internet/)
  assert.match(SETUP_FIXES.hermes, /Update Hermes/)
  assert.deepEqual(SIGNIN_TROUBLE.map(([id]) => id), ['redirect', 'port_busy', 'refused'])
  for (const [id] of SIGNIN_TROUBLE) assert.ok(SETUP_FIXES[id], id)
  for (const text of Object.values(SETUP_FIXES)) assert.doesNotMatch(text, /\b(PKCE|OAuth|loopback|grant|token|403|401|env)\b/i, text)
})

test('Client ID check: empty, wrong shape, a real-looking one', () => {
  assert.equal(clientIdError(''), CLIENT_ID_ERRORS.empty)
  assert.equal(clientIdError('   '), CLIENT_ID_ERRORS.empty)
  assert.equal(clientIdError('not-a-client-id'), CLIENT_ID_ERRORS.shape)
  assert.equal(clientIdError('0123456789abcdef0123456789abcdeg'), CLIENT_ID_ERRORS.shape) // g is not hex
  assert.equal(clientIdError(' 0123456789abcdef0123456789ABCDEF '), null)
  assert.match(CLIENT_ID_ERRORS.shape, /Not the Client secret/)
})

test('setup card: seven titles, copy values and why lines, one step open at a time', () => {
  const store = createStore(fakeCtx({}))
  store.setup.set({ ready: false, step: 'client_id' })
  const json = JSON.stringify(SetupCard({ store }))
  for (const [, , title] of SETUP_STEPS) assert.ok(json.includes(title), title)
  assert.ok(json.includes('Open Spotify Dashboard'))
  assert.ok(json.includes('0 of 7 done'))
  assert.ok(!json.includes(SETUP_DEFAULTS.redirect_uri)) // step 2 stays closed until step 1 is ticked
  store.markStep('dashboard')
  const json2 = JSON.stringify(SetupCard({ store }))
  assert.ok(json2.includes('"value":"Loshify"'))
  assert.ok(json2.includes('http://127.0.0.1:43828/callback'))
  assert.ok(json2.includes('tick only “Web API”'))
  assert.ok(json2.includes("127.0.0.1 means 'this computer', nothing leaves it"))
  store.markStep('app')
  const json3 = JSON.stringify(SetupCard({ store }))
  assert.ok(json3.includes('Settings → User Management'))
  assert.ok(json3.includes('only let in the accounts listed there'))
  store.markStep('users')
  const json4 = JSON.stringify(SetupCard({ store }))
  assert.ok(json4.includes('Spotify Client ID')) // the paste field
  assert.ok(json4.includes('3 of 7 done'))
})

test('setup card step 5: the exact sign-in command with a copy button, and the trouble picker', () => {
  const store = createStore(fakeCtx({}))
  store.setup.set({ ready: false, step: 'signin' })
  let json = JSON.stringify(SetupCard({ store }))
  assert.ok(json.includes('hermes auth add loshify:spotify'))
  assert.ok(!json.includes(SETUP_FIXES.port_busy))
  store.trouble.set('port_busy')
  json = JSON.stringify(SetupCard({ store }))
  assert.ok(json.includes(SETUP_FIXES.port_busy))
  store.trouble.set('redirect')
  json = JSON.stringify(SetupCard({ store }))
  assert.ok(json.includes(JSON.stringify(SETUP_FIXES.redirect).slice(1, 40)))
})

test('setup card step 6: the failure fix and a "Check again" button', () => {
  const store = createStore(fakeCtx({}))
  store.setup.set({ ready: false, step: 'premium_or_users' })
  const json = JSON.stringify(SetupCard({ store }))
  assert.ok(json.includes('Check again'))
  assert.ok(json.includes('Needs a fix'))
  assert.ok(json.includes('add your name and the email of your Spotify account'))
})

test('markStep: ticks persist in ctx.storage; "Back" unticks that step and every later one', () => {
  const ctx = fakeCtx({})
  const store = createStore(ctx)
  assert.equal(store.markStep('dashboard'), true)
  assert.equal(store.markStep('app'), true)
  assert.equal(store.markStep('users'), true)
  assert.deepEqual(ctx.stored.setupMarks, { dashboard: true, app: true, users: true })
  store.markStep('app', false)
  assert.deepEqual(store.setupMarks.get(), { dashboard: true, app: false, users: false })
  assert.equal(store.markStep('connect'), false) // only steps 1–3 are ticked by the user
  assert.deepEqual(createStore(ctx).setupMarks.get(), { dashboard: true, app: false, users: false }) // survives reopening
})

test('saveClientId: a bad paste is refused locally (no request); a good one is saved and step 5 opens', async () => {
  const bodies = []
  const store = createStore(fakeCtx({
    '/setup/client-id': body => { bodies.push(body); return { ok: true, saved: true, setup: { ready: false, step: 'signin' } } }
  }))
  store.clientIdDraft.set('my secret')
  assert.equal(await store.saveClientId(), false)
  assert.equal(store.clientIdState.get().error, CLIENT_ID_ERRORS.shape)
  assert.equal(bodies.length, 0)
  store.clientIdDraft.set(' 0123456789abcdef0123456789abcdef ')
  assert.equal(await store.saveClientId(), true)
  assert.deepEqual(bodies, [{ client_id: '0123456789abcdef0123456789abcdef' }])
  assert.equal(store.clientIdDraft.get(), '')
  assert.equal(setupView(store.setup.get()).open, 'connect')
  assert.equal(sdk.calls.notify.at(-1).kind, 'success')
})

test('saveClientId: a backend refusal shows in the field and nothing moves', async () => {
  const store = createStore(fakeCtx({ '/setup/client-id': { ok: false, error: "Couldn't save the Client ID (locked)." } }))
  store.setup.set({ ready: false, step: 'client_id' })
  store.clientIdDraft.set('0123456789abcdef0123456789abcdef')
  assert.equal(await store.saveClientId(), false)
  assert.match(store.clientIdState.get().error, /Couldn't save/)
  assert.equal(store.setup.get().step, 'client_id')
})

test('checkNow: one real probe; green → the player starts', async () => {
  const ctx = fakeCtx({ '/setup': READY, '/player': { ok: true, track: TRACK, data: {}, settings: {} } })
  const store = createStore(ctx)
  store.setup.set({ ready: false, step: 'premium_or_users' })
  assert.equal(await store.checkNow(), true)
  assert.equal(store.checking.get(), false)
  assert.equal(ctx.calls.filter(c => c.path === '/setup').length, 1)
  assert.equal(ctx.calls.filter(c => c.path === '/player').length, 1)
})

test('until ready, tick polls /setup (every 2 s while open) and never /player', async () => {
  let ready = false
  const ctx = fakeCtx({
    '/setup': () => (ready ? READY : { ready: false, step: 'signin' }),
    '/player': { ok: true, track: TRACK, data: {}, settings: {} }
  })
  const store = createStore(ctx)
  await store.start()
  assert.equal(ctx.calls.filter(c => c.path === '/player').length, 0)
  store.setOpen(true)
  await new Promise(r => setTimeout(r, 5))
  const before = ctx.calls.filter(c => c.path === '/setup').length
  store.tick() // < 2 s since the last poll: nothing
  assert.equal(ctx.calls.filter(c => c.path === '/setup').length, before)
  await new Promise(r => setTimeout(r, SETUP_POLL_MS + 50))
  ready = true
  store.tick()
  await new Promise(r => setTimeout(r, 5))
  assert.equal(ctx.calls.filter(c => c.path === '/setup').length, before + 1)
  assert.equal(store.setup.get().ready, true)
  assert.equal(ctx.calls.filter(c => c.path === '/player').length, 1) // turned green → first player poll
})

test('step 2 copy buttons go through ctx.os.writeClipboard; step 1 opens the dashboard through openExternal', async () => {
  const copied = []
  const opened = []
  const ctx = fakeCtx({}, { os: { writeClipboard: async t => { copied.push(t); return true }, openExternal: async u => { opened.push(u); return true } } })
  const store = createStore(ctx)
  assert.equal(await store.copy(SETUP_DEFAULTS.redirect_uri, 'Redirect URI'), true)
  assert.deepEqual(copied, ['http://127.0.0.1:43828/callback'])
  assert.equal(sdk.calls.notify.at(-1).kind, 'success')
  assert.equal(await store.openExternal(SETUP_DEFAULTS.dashboard_url), true)
  assert.deepEqual(opened, ['https://developer.spotify.com/dashboard'])
})

test('copy without a clipboard door shows the value instead of failing silently', async () => {
  const store = createStore(fakeCtx({}, { os: {} }))
  assert.equal(await store.copy('hermes auth add loshify:spotify', 'Command'), false)
  assert.match(sdk.calls.notify.at(-1).message, /hermes auth add loshify:spotify/)
})

test('step B opens the agent plugin settings page through host.navigate', () => {
  const store = createStore(fakeCtx({}))
  store.open.set(true)
  assert.equal(store.openSettings(), true)
  assert.deepEqual(sdk.calls.navigate, [SETTINGS_ROUTE])
  assert.equal(SETTINGS_ROUTE, '/settings?tab=plugins&agent=loshify')
  assert.equal(store.open.get(), false)
})

test('step D submits the first-run prompt into the focused chat, and remembers it', () => {
  const ctx = fakeCtx({})
  const store = createStore(ctx)
  assert.equal(store.meet(), true)
  assert.deepEqual(sdk.calls.submit, [{ sid: null, text: MEET_PROMPT }])
  assert.equal(store.meetDismissed.get(), true)
  assert.equal(ctx.stored.meetDismissed, true)
})

test('step D with no open chat: tells the user what to ask, keeps the card', () => {
  sdk.composer.accept = false
  const store = createStore(fakeCtx({}))
  assert.equal(store.meet(), false)
  assert.match(sdk.calls.notify.at(-1).message, /Open a chat/)
  assert.equal(store.meetDismissed.get(), false)
})

test('a setup-class error while playing sends the card back to setup', async () => {
  const ctx = fakeCtx({
    '/setup': () => ({ ready: false, step: 'signin' }),
    '/player': { ok: false, error: 'Not signed in', setup: 'signin' }
  })
  const store = createStore(ctx)
  store.setup.set(READY)
  await store.refresh()
  await new Promise(r => setTimeout(r, 5))
  assert.equal(store.setup.get().step, 'signin')
})

test('backend unreachable: the card says so instead of hanging', async () => {
  const ctx = { ...fakeCtx({}), rest: async () => { throw new Error('404') } }
  const store = createStore(ctx)
  await store.start()
  assert.equal(store.setup.get().step, 'offline')
  assert.match(store.setup.get().next_step, /backend did not answer/)
  assert.equal(setupView(store.setup.get()).problemKind, 'offline')
})

// ---------------------------------------------------------------------------
// Reactions
// ---------------------------------------------------------------------------

function readyStore(routes) {
  const ctx = fakeCtx({ '/setup': READY, ...routes })
  const store = createStore(ctx)
  store.setup.set(READY)
  return { ctx, store }
}

test('three 👌 clicks post three reactions (no client-side dedupe)', async () => {
  let n = 0
  const { ctx, store } = readyStore({
    '/player': { ok: true, track: TRACK, data: {}, settings: {} },
    '/feedback': () => ({ ok: true, data: { id: 'u' + (++n) } })
  })
  await store.refresh()
  for (let i = 0; i < 3; i++) assert.equal(await store.react('okay', ''), true)
  const posts = ctx.calls.filter(c => c.path === '/feedback')
  assert.equal(posts.length, 3)
  assert.ok(posts.every(p => p.body.reaction === 'okay' && p.body.track_uri === TRACK.track_uri && p.body.amends === null))
})

test('react(down) on a Loshify playlist: toast names it, Undo restores via /undo', async () => {
  const { ctx, store } = readyStore({
    '/player': { ok: true, track: { ...TRACK, managed: true }, data: { is_playing: true }, settings: {} },
    '/feedback': { ok: true, data: { id: 'e1', undoable: true, actions: { removal: { status: 'removed', playlist_name: 'Deep Dig · Nov' }, skip: { status: 'skipped' } } } },
    '/undo': { ok: true, data: { ok: true, restored: { status: 'restored' } } }
  })
  await store.refresh()
  assert.equal(await store.react('down'), true)
  assert.equal(ctx.calls.find(c => c.path === '/feedback').body.reaction, 'down')
  const n = sdk.calls.notify.at(-1)
  assert.match(n.message, /removed from “Deep Dig · Nov”/)
  assert.equal(n.action.label, 'Undo')
  n.action.onClick()
  await new Promise(r => setTimeout(r, 10))
  assert.deepEqual(ctx.calls.find(c => c.path === '/undo').body, { event_id: 'e1' })
  assert.match(sdk.calls.notify.at(-1).message, /back in its playlist/)
})

test('react(down) on someone else\'s playlist: kept, and the toast says why', async () => {
  const { store } = readyStore({
    '/player': { ok: true, track: TRACK, data: { is_playing: true }, settings: {} },
    '/feedback': { ok: true, data: { id: 'e2', undoable: true, actions: { removal: { status: 'not_managed' }, skip: { status: 'skipped' } } } }
  })
  await store.refresh()
  await store.react('down')
  assert.match(sdk.calls.notify.at(-1).message, /Loshify only edits playlists it made/)
})

test('failed undo reports the error', async () => {
  const { store } = readyStore({ '/undo': { ok: false, error: 'too old' } })
  assert.equal(await store.undo('e9'), false)
  assert.match(sdk.calls.notify.at(-1).message, /Undo failed: too old/)
})

test('late note amends the original reaction id', async () => {
  let n = 0
  const { ctx, store } = readyStore({
    '/player': { ok: true, track: TRACK, data: {}, settings: {} },
    '/feedback': () => ({ ok: true, data: { id: 'e' + (++n) } })
  })
  await store.refresh()
  await store.react('love', '')
  store.draft.set('the chorus')
  assert.equal(await store.saveLateNote(), true)
  const posts = ctx.calls.filter(c => c.path === '/feedback')
  assert.equal(posts[1].body.amends, 'e1')
  assert.equal(posts[1].body.reaction, 'love')
  store.draft.set('and the bridge')
  await store.saveLateNote()
  assert.equal(ctx.calls.filter(c => c.path === '/feedback')[2].body.amends, 'e1')
})

test('no track: warns and posts nothing', async () => {
  const { ctx, store } = readyStore({ '/player': { ok: true, track: null, data: null, settings: {} } })
  await store.refresh()
  assert.equal(await store.react('okay'), false)
  assert.equal(ctx.calls.some(c => c.path === '/feedback'), false)
  assert.equal(sdk.calls.notify.at(-1).kind, 'warning')
})

test('transport targets the active device', async () => {
  const { ctx, store } = readyStore({
    '/player': { ok: true, track: TRACK, data: { is_playing: true, device: { id: 'd1', is_active: true } }, settings: {} },
    '/pause': { ok: true }
  })
  await store.refresh()
  await store.transport('pause')
  assert.deepEqual(ctx.calls.find(c => c.path === '/pause').body, { device_id: 'd1' })
})

test('search asks for at most 10 results (Development Mode cap)', async () => {
  const { ctx, store } = readyStore({ '/search': { ok: true, data: { tracks: { items: [] } } } })
  store.searchText.set('khruangbin')
  await store.runSearch('khruangbin')
  assert.match(ctx.calls.find(c => c.path.startsWith('/search')).path, /limit=10$/)
})

// ---------------------------------------------------------------------------
// Seek / scrub (carried over from v0.3 unchanged in behaviour)
// ---------------------------------------------------------------------------

const { clampMs, positionFromPointer, progressPct, keySeekDelta, knownDuration, SEEK_STEP_MS, KEY_SEEK_DEBOUNCE_MS } = mod
const ITEM = { uri: TRACK.track_uri, duration_ms: 200000 }
const PLAYING = { is_playing: true, progress_ms: 60000, item: ITEM, device: { id: 'd1', is_active: true } }
const RECT = { left: 100, width: 200 }

function seekStore(data = PLAYING) {
  return readyStore({
    '/player': { ok: true, track: TRACK, data, settings: {} },
    '/seek': body => ({ ok: true, data: { position_ms: body.position_ms } })
  })
}

test('clampMs: clamps to [0, duration]; unknown/0 duration keeps only the lower bound', () => {
  assert.equal(clampMs(-500, 200000), 0)
  assert.equal(clampMs(250000, 200000), 200000)
  assert.equal(clampMs(1234.6, 200000), 1235)
  assert.equal(clampMs(90000, 0), 90000)
  assert.equal(clampMs(90000, undefined), 90000)
  assert.equal(clampMs(-1, null), 0)
  assert.equal(clampMs(NaN, 200000), 0)
  assert.equal(clampMs('abc', 200000), 0)
  assert.equal(knownDuration(0), 0)
  assert.equal(knownDuration(-5), 0)
  assert.equal(knownDuration(Infinity), 0)
  assert.equal(knownDuration(200000.9), 200000)
})

test('positionFromPointer: LTR mapping, clamped at both edges, null without a duration or width', () => {
  assert.equal(positionFromPointer(100, RECT, 200000), 0)
  assert.equal(positionFromPointer(200, RECT, 200000), 100000)
  assert.equal(positionFromPointer(300, RECT, 200000), 200000)
  assert.equal(positionFromPointer(40, RECT, 200000), 0)
  assert.equal(positionFromPointer(999, RECT, 200000), 200000)
  assert.equal(positionFromPointer(150, RECT, 0), null)
  assert.equal(positionFromPointer(150, RECT, undefined), null)
  assert.equal(positionFromPointer(150, { left: 0, width: 0 }, 200000), null)
  assert.equal(positionFromPointer(150, null, 200000), null)
})

test('progressPct and fmtTime', () => {
  assert.equal(progressPct(50000, 200000), 25)
  assert.equal(progressPct(999999, 200000), 100)
  assert.equal(progressPct(-1, 200000), 0)
  assert.equal(progressPct(50000, 0), 0)
  assert.equal(fmtTime(200000), '3:20')
  assert.equal(fmtTime(3725000), '1:02:05')
  assert.equal(fmtTime(NaN), '0:00')
})

test('keySeekDelta: plain arrows only (Ctrl+Alt+arrows stay with the reaction keybinds)', () => {
  assert.equal(keySeekDelta({ key: 'ArrowRight' }), SEEK_STEP_MS)
  assert.equal(keySeekDelta({ key: 'ArrowLeft' }), -SEEK_STEP_MS)
  assert.equal(SEEK_STEP_MS, 5000)
  assert.equal(keySeekDelta({ key: 'ArrowRight', ctrlKey: true, altKey: true }), 0)
  assert.equal(keySeekDelta({ key: 'ArrowUp' }), 0)
  assert.equal(keySeekDelta(null), 0)
})

test('click: one seek to the clicked position, never a reaction or a skip', async () => {
  const { ctx, store } = seekStore()
  await store.refresh()
  assert.equal(store.beginScrub(positionFromPointer(150, RECT, 200000)), true)
  assert.equal(await store.endScrub(positionFromPointer(150, RECT, 200000)), true)
  const seeks = ctx.calls.filter(c => c.path === '/seek')
  assert.equal(seeks.length, 1)
  assert.deepEqual(seeks[0].body, { position_ms: 50000, track_uri: TRACK.track_uri, device_id: 'd1' })
  assert.equal(seeks[0].method, 'POST')
  assert.equal(ctx.calls.some(c => c.path === '/feedback' || c.path === '/next' || c.path === '/previous'), false)
  assert.equal(store.player.get().progress_ms, 50000)
})

test('drag: moves only update the preview; exactly one seek on release, at the release point', async () => {
  const { ctx, store } = seekStore()
  await store.refresh()
  store.beginScrub(positionFromPointer(120, RECT, 200000))
  for (const x of [130, 160, 190, 240, 260, 999]) store.moveScrub(positionFromPointer(x, RECT, 200000))
  assert.equal(ctx.calls.filter(c => c.path === '/seek').length, 0)
  assert.equal(store.scrub.get().ms, 200000)
  assert.equal(store.shownMs(), 200000)
  await store.endScrub(positionFromPointer(260, RECT, 200000))
  const seeks = ctx.calls.filter(c => c.path === '/seek')
  assert.equal(seeks.length, 1)
  assert.equal(seeks[0].body.position_ms, 160000)
  assert.equal(store.scrub.get(), null)
})

test('drag released with no usable pointer position commits the last preview', async () => {
  const { ctx, store } = seekStore()
  await store.refresh()
  store.beginScrub(40000)
  store.moveScrub(70000)
  await store.endScrub(null)
  assert.equal(ctx.calls.find(c => c.path === '/seek').body.position_ms, 70000)
})

test('cancelled drag (Escape / pointercancel) sends nothing', async () => {
  const { ctx, store } = seekStore()
  await store.refresh()
  store.beginScrub(40000)
  store.moveScrub(90000)
  store.cancelScrub()
  assert.equal(await store.endScrub(120000), false)
  assert.equal(ctx.calls.some(c => c.path === '/seek'), false)
})

test('unknown or 0 duration: no scrub, no key seek, no request', async () => {
  for (const d of [0, undefined, null]) {
    const { ctx, store } = seekStore({ ...PLAYING, item: { uri: TRACK.track_uri, duration_ms: d } })
    await store.refresh()
    assert.equal(store.beginScrub(positionFromPointer(150, RECT, d)), false)
    assert.equal(await store.endScrub(50000), false)
    assert.equal(store.nudge(5000), false)
    assert.equal(await store.seekTo(50000), false)
    assert.equal(ctx.calls.some(c => c.path === '/seek'), false)
  }
})

test('nothing playing: seekTo refuses', async () => {
  const { ctx, store } = readyStore({ '/player': { ok: true, track: null, data: null, settings: {} } })
  await store.refresh()
  assert.equal(await store.seekTo(1000), false)
  assert.equal(ctx.calls.some(c => c.path === '/seek'), false)
})

test('arrow keys: ±5s each, rapid presses accumulate into ONE clamped seek', async () => {
  const { ctx, store } = seekStore({ ...PLAYING, is_playing: false, progress_ms: 190000 })
  await store.refresh()
  assert.equal(store.nudge(5000), true)
  assert.equal(store.shownMs(), 195000)
  store.nudge(5000)
  store.nudge(5000)
  assert.equal(store.shownMs(), 200000)
  store.nudge(-5000)
  assert.equal(ctx.calls.filter(c => c.path === '/seek').length, 0)
  await new Promise(r => setTimeout(r, KEY_SEEK_DEBOUNCE_MS + 60))
  const seeks = ctx.calls.filter(c => c.path === '/seek')
  assert.equal(seeks.length, 1)
  assert.equal(seeks[0].body.position_ms, 195000)
})

test('arrow left at the start clamps to 0', async () => {
  const { ctx, store } = seekStore({ ...PLAYING, is_playing: false, progress_ms: 2000 })
  await store.refresh()
  store.nudge(-5000)
  await new Promise(r => setTimeout(r, KEY_SEEK_DEBOUNCE_MS + 60))
  assert.equal(ctx.calls.find(c => c.path === '/seek').body.position_ms, 0)
})

test('a stale poll right after a seek does not snap the bar back', async () => {
  let data = { ...PLAYING, is_playing: false }
  const { store } = readyStore({
    '/player': () => ({ ok: true, track: TRACK, data, settings: {} }),
    '/seek': { ok: true }
  })
  await store.refresh()
  await store.seekTo(150000)
  await store.refresh()
  assert.equal(store.player.get().progress_ms, 150000)
  data = { ...PLAYING, is_playing: false, progress_ms: 150300 }
  await store.refresh()
  assert.equal(store.player.get().progress_ms, 150300)
})

test('failed seek reports in the status line and does not pin the position', async () => {
  const { store } = readyStore({
    '/player': { ok: true, track: TRACK, data: { ...PLAYING, is_playing: false }, settings: {} },
    '/seek': { ok: false, error: 'Restriction violated' }
  })
  await store.refresh()
  assert.equal(await store.seekTo(150000), false)
  assert.equal(store.status.get(), 'Restriction violated')
  await store.refresh()
  assert.equal(store.player.get().progress_ms, 60000)
})

test('poll cadence comes from backend settings', async () => {
  const { ctx, store } = readyStore({ '/player': { ok: true, track: TRACK, data: {}, settings: { poll_interval_open: 2, poll_interval_closed: 9 } } })
  await store.refresh()
  const before = ctx.calls.length
  store.tick()
  assert.equal(ctx.calls.length, before)
})


// -- rotations -------------------------------------------------------------

const PRESETS = [
  { id: 'rhythm_quiet_mixed', name: 'Rhythmic / Quiet / Mixed', intent: 'By energy', cadence: 'weekly', archive: 'archive', slots: [] },
  { id: 'day_parts', name: 'Morning / Focus / Night', intent: 'By time of day', cadence: 'weekly', archive: 'archive', slots: [] }
]
const ROTATION_SET = {
  ok: true,
  data: {
    configured: true, spec: { name: 'Rhythmic / Quiet / Mixed', cadence: 'weekly', archive: 'archive' }, edition: 2,
    last_refresh: '2027-01-01 10:00 UTC', due: true, archive_playlist_id: 'arch1',
    playlists: [{ slot: 'Rhythmic', mode: 'mixed', url: 'https://open.spotify.com/playlist/p1' }, { slot: 'Quiet', mode: 'mixed', url: null }],
    presets: PRESETS
  }
}

test('arrangePrompt / meetPrompt: the user words go to the curator on one line; empty input sends nothing', () => {
  assert.equal(arrangePrompt('   '), null)
  const msg = arrangePrompt('one for cooking,\n  one for night drives')
  assert.match(msg, /“one for cooking, one for night drives”/)
  assert.match(msg, /show it to me/i)
  assert.match(arrangePrompt('', 'Morning / Focus / Night'), /“Morning \/ Focus \/ Night”/)
  assert.ok(arrangePrompt('x'.repeat(2000)).length < ARRANGE_MAX + 200)
  assert.equal(meetPrompt(''), MEET_PROMPT)
  assert.ok(meetPrompt('by energy').startsWith(MEET_PROMPT) && meetPrompt('by energy').includes('“by energy”'))
})

test('rotationView maps the backend answer (none / set / error)', () => {
  assert.equal(rotationView(null).state, 'loading')
  assert.equal(rotationView({ ok: false, error: 'boom' }).state, 'error')
  const none = rotationView({ ok: true, data: { configured: false, presets: PRESETS } })
  assert.equal(none.state, 'none')
  assert.equal(none.presets.length, 2)
  const set = rotationView(ROTATION_SET)
  assert.equal(set.state, 'set')
  assert.deepEqual(set.slots.map(s => s.name), ['Rhythmic', 'Quiet'])
  assert.match(set.sub, /weekly · edition 2 .* · due/)
  assert.equal(set.archiveUrl, 'https://open.spotify.com/playlist/arch1')
})

test('setup card step D is the click-through flow once Spotify is ready; the chat route stays available', () => {
  const store = createStore(fakeCtx({}))
  const has = (node, type) => !!node && typeof node === 'object' &&
    (node.type === type || Object.values(node.props || node).some(v => Array.isArray(v) ? v.some(x => has(x, type)) : has(v, type)))
  store.setup.set({ ready: true, step: 'done', onboarded: false })
  assert.ok(has(SetupCard({ store }), GetToKnowMe))
  assert.ok(JSON.stringify(GetToKnowMe({ store })).includes('Do it in the chat instead'))
  assert.equal(ArrangeBox({ store }).props.placeholder, ARRANGE_PLACEHOLDER)
  assert.equal(ArrangeBox({ store }).props.maxLength, ARRANGE_MAX)
  store.setup.set({ ready: false, step: 'signin' })
  assert.ok(!has(SetupCard({ store }), GetToKnowMe))
  store.arrange.set('rhythmic, quiet and mixed, new every week')
  assert.equal(store.meet(), true)
  assert.equal(sdk.calls.submit.at(-1).text, `${MEET_PROMPT} This is how I like my music arranged: “rhythmic, quiet and mixed, new every week”.`)
  assert.equal(store.arrange.get(), '')
})

test('Playlists tab loads /rotation; presets and own words go to the chat; refresh goes to the chat', async () => {
  const ctx = fakeCtx({ '/rotation': { ok: true, data: { configured: false, presets: PRESETS } }, '/playlists': { ok: true, data: { items: [] } } })
  const store = createStore(ctx)
  store.setup.set(READY)
  store.selectView('playlists')
  await new Promise(r => setTimeout(r, 5))
  assert.ok(ctx.calls.some(c => c.path === '/rotation'))
  assert.equal(store.rotation.get().state, 'none')
  const json = JSON.stringify(RotationSection({ store }))
  assert.ok(json.includes('Rhythmic / Quiet / Mixed') && json.includes('How do you like your music organized?'))

  assert.equal(store.sendArrangement(), false) // nothing typed
  assert.match(sdk.calls.notify.at(-1).message, /Describe how you like your music arranged/)
  assert.equal(store.sendArrangement('Morning / Focus / Night'), true)
  assert.match(sdk.calls.submit.at(-1).text, /Morning \/ Focus \/ Night/)
  store.arrange.set('one playlist per mood')
  assert.equal(store.sendArrangement(), true)
  assert.match(sdk.calls.submit.at(-1).text, /“one playlist per mood”/)
  assert.equal(store.refreshRotation(), true)
  assert.equal(sdk.calls.submit.at(-1).text, REFRESH_PROMPT)
  // The Desktop never writes a rotation itself: no POST/PUT to /rotation.
  assert.ok(!ctx.calls.some(c => c.path.startsWith('/rotation') && c.method && c.method !== 'GET'))
})

test('a configured rotation shows its playlists, the archive and Refresh now', async () => {
  const store = createStore(fakeCtx({ '/rotation': ROTATION_SET }))
  await store.loadRotation()
  const json = JSON.stringify(RotationSection({ store }))
  for (const s of ['YOUR ROTATION', 'Rhythmic / Quiet / Mixed', '✦ Rhythmic', '✦ Quiet', 'not built yet', 'Archive (earlier editions)', 'Refresh now (due)']) assert.ok(json.includes(s), s)
})

test('without an open chat, a rotation request is shown as text and the words are kept', () => {
  sdk.composer.accept = false
  const store = createStore(fakeCtx({}))
  store.arrange.set('quiet mornings')
  assert.equal(store.sendArrangement(), false)
  assert.match(sdk.calls.notify.at(-1).message, /Open a chat/)
  assert.equal(store.arrange.get(), 'quiet mornings')
})
