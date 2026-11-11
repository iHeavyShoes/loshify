// Taste tab: calibration round (👎 👌 ❤️ without playback), anchors, the open note.
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

globalThis.document = { createElement: () => ({ remove() {} }), head: { append() {} } }

const sdk = await import('@hermes/plugin-sdk')
const { createStore, onboardingView, noteError, TasteView, NoteBox, TABS, NOTE_MAX, NOTE_PLACEHOLDER } = await import('../../desktop/plugin.js')

const ONB = {
  ok: true,
  data: {
    active: true, started: true, finished: false, next_step: 'calibration', note: '', note_max: 300,
    anchors: [{ kind: 'artist', title: '', artist: 'Khruangbin' }, { kind: 'song', title: 'Kalimba', artist: 'Mr. Scruff' }],
    calibration: {
      reacted: 0, total: 2, complete: false,
      tracks: [
        { n: 1, title: 'Maria Tambien', artists: ['Khruangbin'], uri: 'spotify:track:k01', kind: 'familiar', reason: 'your summer', reaction: null },
        { n: 2, title: 'Song of the Stretch', artists: ['Moondog'], uri: 'spotify:track:s01', kind: 'stretch', reason: '', reaction: null }
      ]
    }
  }
}

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

beforeEach(() => { sdk.calls.notify.length = 0 })

test('Taste is a tab and the view maps the backend answer', () => {
  assert.deepEqual(TABS.map(t => t[0]), ['now', 'taste', 'playlists', 'search', 'queue'])
  const v = onboardingView(ONB)
  assert.equal(v.state, 'ready')
  assert.deepEqual(v.anchors, ['Khruangbin', 'Kalimba – Mr. Scruff'])
  assert.equal(v.tracks[1].kind, 'a stretch')
  assert.equal(v.tracks[0].artists, 'Khruangbin')
  assert.ok(v.pending && v.active)
  assert.equal(onboardingView({ ok: false, error: 'boom' }).state, 'error')
  assert.equal(onboardingView(null).state, 'loading')
})

test('selecting the Taste tab loads /onboarding and renders the round, anchors and note box', async () => {
  const ctx = fakeCtx({ '/setup': { ok: true, data: { ready: true, step: 'done', onboarded: true } }, '/onboarding': ONB })
  const store = createStore(ctx)
  store.setup.set({ ready: true, step: 'done', onboarded: true })
  store.selectView('taste')
  await new Promise(r => setTimeout(r, 5))
  assert.ok(ctx.calls.some(c => c.path === '/onboarding'))
  const json = JSON.stringify(TasteView({ store }))
  for (const s of ['QUICK CALIBRATION · 0/2', 'Maria Tambien', 'Song of the Stretch', 'a stretch', 'YOUR ANCHORS', 'Khruangbin · Kalimba – Mr. Scruff']) assert.ok(json.includes(s), s)
})

test('a calibration tap posts to /onboarding/react and needs no player', async () => {
  const ctx = fakeCtx({ '/onboarding/react': body => ({ ok: true, data: { id: 'r1', reaction: body.reaction, reacted: 1, total: 2, complete: false } }), '/onboarding': ONB })
  const store = createStore(ctx)
  await store.loadOnboarding()
  assert.equal(await store.reactCalibration(2, 'love'), true)
  const post = ctx.calls.find(c => c.path === '/onboarding/react')
  assert.deepEqual(post.body, { n: 2, reaction: 'love' })
  assert.equal(store.onboarding.get().tracks[1].reaction, 'love')
  assert.equal(store.onboarding.get().reacted, 1)
  assert.ok(!ctx.calls.some(c => c.path.startsWith('/player') || c.path.startsWith('/feedback')))
  assert.equal(await store.reactCalibration(1, 'bogus'), false)
})

test('a failed calibration tap is reported, not swallowed', async () => {
  const store = createStore(fakeCtx({ '/onboarding/react': { ok: false, error: 'no such calibration track' }, '/onboarding': ONB }))
  await store.loadOnboarding()
  assert.equal(await store.reactCalibration(1, 'down'), false)
  assert.match(sdk.calls.notify.at(-1).message, /no such calibration track/)
})

test('the note: validated client-side, saved through /note, editable and clearable', async () => {
  assert.equal(noteError('fine'), null)
  assert.match(noteError('x'.repeat(NOTE_MAX + 1)), /300/)
  assert.match(NOTE_PLACEHOLDER, /Anything else/)
  const ctx = fakeCtx({ '/note': body => ({ ok: true, data: { note: body.text } }), '/onboarding': ONB })
  const store = createStore(ctx)
  await store.loadOnboarding()
  store.noteDraft.set('x'.repeat(NOTE_MAX + 5))
  assert.equal(await store.saveNote(), false)
  assert.ok(!ctx.calls.some(c => c.path === '/note'))
  store.noteDraft.set('  more dub, less EDM ')
  assert.ok(JSON.stringify(NoteBox({ store })).includes('ANYTHING ELSE?'))
  assert.equal(await store.saveNote(), true)
  assert.deepEqual(ctx.calls.find(c => c.path === '/note').body, { text: 'more dub, less EDM' })
  assert.equal(store.onboarding.get().note, 'more dub, less EDM')
  assert.equal(store.noteDraft.get(), null)
  store.noteDraft.set('')
  assert.equal(await store.saveNote(), true)
  assert.match(sdk.calls.notify.at(-1).message, /cleared/)
})

test('skip posts the step and reloads', async () => {
  const ctx = fakeCtx({ '/onboarding/skip': { ok: true, data: { next_step: 'rotation' } }, '/onboarding': ONB })
  const store = createStore(ctx)
  assert.equal(await store.skipStep('calibration'), true)
  assert.deepEqual(ctx.calls[0].body, { step: 'calibration' })
  assert.equal(ctx.calls[1].path, '/onboarding')
})
