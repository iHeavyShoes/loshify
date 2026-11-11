/**
 * Loshify: Hermes Desktop half.
 *
 * A status-bar Spotify Connect remote (now playing, transport, seek, volume, devices, playlists,
 * search, queue) with one-tap reactions that teach the curator:
 *   👎 down  · 👌 okay  · ❤️ love (shown in that order, left to right; 👎 = "never again": skips, and removes the track ONLY from a playlist
 *   Loshify itself created, with Undo).
 * Audio keeps playing in the user's own Spotify apps; this drives them.
 *
 * Until Spotify is connected, the panel shows the setup wizard instead: one step at a time (open the
 * Spotify Dashboard, create the app with copyable values, add yourself under User Management, paste the
 * Client ID (saved into the plugin setting), connect, a real check, meet your curator).
 *
 * All Spotify traffic goes through this package's backend (dashboard/plugin_api.py) via
 * ctx.rest('/…') → /api/plugins/loshify/*, where Hermes attaches the sign-in. Plain ESM, no build step.
 * Imports only @hermes/plugin-sdk and react/jsx-runtime; SDK doors only (no app DOM, no bridge).
 */

import {
  atom,
  Button,
  host,
  icons,
  Input,
  Kbd,
  KEYBINDS_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  SearchField,
  STATUSBAR_AREAS,
  Textarea,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'loshify'
export const TICK_MS = 1000
export const SETUP_POLL_MS = 2000

export const REACTIONS = {
  love: { emoji: '❤️', tip: '❤️ Love it' },
  okay: { emoji: '👌', tip: '👌 Works for me' },
  down: { emoji: '👎', tip: '👎 Never again (skips; removes it from Loshify playlists)' }
}
export const REACTION_ORDER = ['down', 'okay', 'love']  // left → right: 👎 👌 ❤️

export const MODE_LABEL = {
  now: 'Now playing',
  paused: 'Paused',
  last: 'Last track (not playing now)',
  none: 'Nothing playing',
  error: 'Not connected to Spotify'
}

export const TABS = [['now', 'Player'], ['taste', 'Taste'], ['playlists', 'Playlists'], ['search', 'Search'], ['queue', 'Queue']]

/** Onboarding: the open "anything else?" note (Desktop box + chat), and the calibration round. */
export const NOTE_MAX = 300
export const NOTE_PLACEHOLDER = 'Anything else I should take into account? A genre, a vibe inside a genre, things to avoid…'
export const CALIBRATION_KIND = { familiar: 'familiar', near: 'near you', stretch: 'a stretch' }

/** Shortcut legend (rendered as a small grid). */
export const KEY_LEGEND = [['👎', 'Ctrl+Alt+↓'], ['👌', 'Ctrl+Alt+→'], ['❤️', 'Ctrl+Alt+↑'], ['🎵', 'Ctrl+Alt+Enter']]
export const POP_CLASS = 'lfy-pop'

/** The message step D puts into the open chat. */
export const MEET_PROMPT = 'Get to know my music and make my first playlist.'
/** "Get to know me": the click-through stepper (no chat). Each stage waits on one backend call. */
export const FLOW_STAGES = ['portrait', 'calibration', 'arrange', 'done']
export const FLOW_WORKING = {
  meet: 'Reading your listening history and writing your portrait…',
  verdict: 'Rewriting your portrait with what you told me…',
  build: 'Building your playlists… (about a minute)'
}
/** The only thing a failed step ever says (with a Try again button). Never a raw transport error. */
export const FLOW_FAILED = {
  meet: 'I couldn’t finish getting to know your music this time.',
  verdict: 'I couldn’t rewrite your portrait this time.',
  build: 'Your playlists couldn’t be built this time.',
  'tune-in': 'Tune in could not be started this time.'
}
/** Long steps run as backend jobs; the card polls this often. A poll that errors just polls again. */
export const JOB_POLL_MS = 1500
/** Give up on a job only after this long (the backend reports a hung job as failed at 10 min). */
export const JOB_GIVE_UP_MS = 11 * 60 * 1000
/** "unknown" this many polls in a row = the start never reached the backend: offer Try again. */
export const JOB_UNKNOWN_LIMIT = 4
/** Step 5: where the sign-in command goes, in plain words. */
export const CONNECT_WHERE = 'Where to paste it: on Windows press Start, type PowerShell and press Enter; on a Mac open Terminal. Paste with Ctrl+V (Mac: Cmd+V), then press Enter.'
/** Taste tab: re-run the first-run flow. */
export const START_OVER_TEXT = 'Run “Get to know me” again: a fresh portrait and five new picks. Your reactions, anchors and note stay.'
export const ANCHORS_PLACEHOLDER = 'Optional: up to 3 artists or songs that are really you, one per line'
/** Rotations: the user's words go to the curator, who turns them into a spec, shows it and asks to confirm. */
export const ARRANGE_PLACEHOLDER = 'Describe how you like your music arranged (e.g. “one for cooking, one for night drives, something new every week”)'
export const ARRANGE_MAX = 500
export const REFRESH_PROMPT = 'Refresh my Loshify rotation now.'
/** Values the setup card offers to copy (the backend's /setup answer wins when it has them). */
export const SETUP_DEFAULTS = {
  app_name: 'Loshify',
  api: 'Web API',
  redirect_uri: 'http://127.0.0.1:43828/callback',
  signin_command: 'hermes auth add loshify:spotify',
  dashboard_url: 'https://developer.spotify.com/dashboard',
  why: "This is your own key to Spotify; 127.0.0.1 means 'this computer', nothing leaves it.",
  users_why: 'New Spotify apps only let in the accounts listed there.'
}
/** Plain-language fixes (the backend's /setup `fixes` win; these keep the card useful if it is older). */
export const SETUP_FIXES = {
  redirect: 'Spotify says the redirect URI is wrong or "not secure". In your app, open Settings → Edit and make the Redirect URI exactly http://127.0.0.1:43828/callback (with :43828, no slash at the end, not "localhost"). Save, then connect again. Hermes chooses a free port on this computer; Spotify accepts a different port.',
  refused: 'Spotify refused this app. Check that the Client ID you pasted is the one on your app\'s Settings page, and that the Redirect URI is exactly http://127.0.0.1:43828/callback. Then connect again.',
  not_listed: 'Spotify said no to this account. In your app, open Settings → User Management and add your name and the email of your Spotify account, then Save and check again. The app\'s owner also needs Spotify Premium.',
  port_busy: 'The sign-in port on this computer is busy. Close any other sign-in that is still waiting (another terminal or browser tab), wait a minute, then run the command again.',
  no_premium: 'Connected, but this Spotify account has no Premium: playlists and your taste work, play, pause and skip don\'t.',
  no_answer: 'Spotify didn\'t answer. Check your internet connection, then check again in a minute.',
  hermes: 'Hermes blocked the request. Update Hermes, then reinstall Loshify.'
}
/** Settings → Plugins, scoped to this agent plugin's config_schema form. */
export const SETTINGS_ROUTE = `/settings?tab=plugins&agent=${ID}`

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

export const errText = err => String((err && err.message) || err)

export const fmtTime = ms => {
  if (!ms || ms < 0 || !Number.isFinite(ms)) return '0:00'
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

// -- seek / scrub math (time always flows left → right) --

export const SEEK_STEP_MS = 5000
export const KEY_SEEK_DEBOUNCE_MS = 300

/** A usable track length in ms, or 0 when unknown (seeking is then disabled). */
export const knownDuration = d => (typeof d === 'number' && Number.isFinite(d) && d > 0 ? Math.floor(d) : 0)

/** Clamp a seek target to [0, duration]; with an unknown/0 duration only the lower bound applies. */
export function clampMs(ms, duration) {
  const n = Number(ms)
  if (!Number.isFinite(n)) return 0
  const pos = Math.max(0, Math.round(n))
  const d = knownDuration(duration)
  return d ? Math.min(pos, d) : pos
}

/** Pointer x → track position (always LTR). null when the duration is unknown or the bar has no width. */
export function positionFromPointer(clientX, rect, duration) {
  const d = knownDuration(duration)
  if (!d || !rect || !(rect.width > 0) || !Number.isFinite(Number(clientX))) return null
  const frac = Math.min(1, Math.max(0, (Number(clientX) - rect.left) / rect.width))
  return Math.round(frac * d)
}

/** Progress percentage for the fill (0 when the duration is unknown). */
export function progressPct(ms, duration) {
  const d = knownDuration(duration)
  return d ? (clampMs(ms, d) / d) * 100 : 0
}

/** The ms delta a key press on the focused bar asks for, or 0 (not ours). */
export function keySeekDelta(e) {
  if (!e || e.ctrlKey || e.altKey || e.metaKey) return 0 // leave Ctrl+Alt+arrows to the reaction keybinds
  if (e.key === 'ArrowRight') return SEEK_STEP_MS
  if (e.key === 'ArrowLeft') return -SEEK_STEP_MS
  return 0
}

/** Backend /player response → reaction card state. */
export function cardFromPlayer(res) {
  if (res && res.ok && res.track) return { mode: res.track.is_playing ? 'now' : 'paused', track: res.track, error: null }
  const last = res && res.last ? res.last : null
  if (last) return { mode: 'last', track: last, error: res && !res.ok ? res.error || null : null }
  if (res && !res.ok) return { mode: 'error', track: null, error: res.error || 'error' }
  return { mode: 'none', track: null, error: null }
}

/** The POST body for a reaction on exactly the track the card shows. */
export function feedbackBody(card, reaction, note, amends = null) {
  if (!card || !card.track || !card.track.track_uri) return null
  if (!Object.prototype.hasOwnProperty.call(REACTIONS, reaction)) return null
  const text = typeof note === 'string' ? note.trim() : ''
  return { track_uri: card.track.track_uri, reaction, note: text ? text : null, amends: amends || null }
}

/** One line describing what a 👎 did. */
export function describeDislike(title, actions) {
  const parts = [`👎 Saved: ${title}`]
  const removal = (actions && actions.removal) || {}
  const skip = (actions && actions.skip) || {}
  const where = removal.playlist_name ? `“${removal.playlist_name}”` : 'the playlist'
  if (removal.status === 'removed') parts.push(`removed from ${where}`)
  else if (removal.status === 'not_managed') parts.push('kept in the playlist (Loshify only edits playlists it made)')
  else if (removal.status === 'not_in_playlist') parts.push(`not found in ${where}`)
  else if (removal.status === 'error') parts.push(`removal failed: ${removal.error || 'error'}`)
  if (skip.status === 'skipped') parts.push('skipped to the next track')
  else if (skip.status === 'track_changed') parts.push('the track had already changed, so no skip')
  else if (skip.status === 'error') parts.push(`skip failed: ${skip.error || 'error'}`)
  return parts.join(' · ')
}

export const playlistCount = pl => {
  const n = pl && ((pl.items && pl.items.total) ?? (pl.tracks && pl.tracks.total))
  return typeof n === 'number' ? n : null
}

// -- rotations --

const oneLine = text => String(text || '').replace(/\s+/g, ' ').trim().slice(0, ARRANGE_MAX)

/** Chat message for "arrange my music like this" (custom words or a preset name). Empty input → null. */
export function arrangePrompt(text, presetName = null) {
  if (presetName) return `Set up my Loshify rotation as “${oneLine(presetName)}”. Show it to me, and build the first edition once I confirm.`
  const words = oneLine(text)
  if (!words) return null
  return `This is how I like my music arranged: “${words}”. Turn it into a Loshify rotation, show it to me, and build the first edition once I confirm.`
}

/** Step D message: the first-run request, plus the arrangement if the user already described one. */
export function meetPrompt(text) {
  const words = oneLine(text)
  return words ? `${MEET_PROMPT} This is how I like my music arranged: “${words}”.` : MEET_PROMPT
}

/** Backend /rotation answer → what the Playlists tab shows. */
export function rotationView(res) {
  const d = res && res.ok && res.data ? res.data : null
  if (!d) return { state: res && res.error ? 'error' : 'loading', error: (res && res.error) || null, presets: [] }
  const presets = d.presets || []
  if (!d.configured) return { state: 'none', presets }
  const slots = (d.playlists || []).map(p => ({ name: p.slot, mode: p.mode, url: p.url || null }))
  const when = d.last_refresh ? `edition ${d.edition} · ${d.last_refresh}` : 'not built yet'
  return {
    state: 'set', presets, name: d.spec.name, cadence: d.spec.cadence, archive: d.spec.archive, slots,
    due: !!d.due, sub: `${d.spec.cadence} · ${when}${d.due ? ' · due' : ''}`, archiveUrl: d.archive_playlist_id ? `https://open.spotify.com/playlist/${d.archive_playlist_id}` : null
  }
}

// -- onboarding: calibration + note --

/** Backend /onboarding answer → what the Taste tab shows. */
export function onboardingView(res) {
  const d = res && res.ok && res.data ? res.data : null
  if (!d) return { state: res && res.error ? 'error' : 'loading', error: (res && res.error) || null, tracks: [], anchors: [], note: '' }
  const cal = d.calibration || null
  const tracks = cal ? cal.tracks.map(t => ({
    n: t.n, uri: t.uri, title: t.title, artists: (t.artists || []).join(', '), kind: CALIBRATION_KIND[t.kind] || t.kind,
    reason: t.reason || '', reaction: t.reaction || null
  })) : []
  const anchors = (d.anchors || []).map(a => (a.kind === 'song' && a.title ? `${a.title} – ${a.artist}` : a.artist))
  return {
    state: 'ready', tracks, anchors, note: d.note || '', noteMax: d.note_max || NOTE_MAX,
    reacted: cal ? cal.reacted : 0, total: cal ? cal.total : 0, pending: !!(cal && !cal.complete),
    active: !!d.active, nextStep: d.next_step || null
  }
}

/** Anchors box text → up to 3 trimmed lines. */
export function anchorsFromText(text) {
  return String(text || '').split(/\r?\n|,/).map(s => s.trim()).filter(Boolean).slice(0, 3)
}

/** A fresh job id for a long step (the card picks it, so a lost answer still knows what to poll). */
export function newJobId() {
  try { if (globalThis.crypto && globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID() } catch { /* fall through */ }
  return 'j' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12)
}

/** A backend reason worth showing as is: plain words, not an exception name or a transport string. */
const RAW_ERROR = /unexpectedly|\b[A-Z]\w*(Error|Exception)\b|abort|signal|timed? ?out|fetch|network|HTTP \d|status \d|Traceback/i

/** The one line a failed step shows: the step's own plain reason, else the kind FLOW_FAILED line. */
export function friendlyFailure(kind, res) {
  const said = res && ((res.data && typeof res.data === 'object' && res.data.error) || res.error)
  if (typeof said === 'string' && said.trim() && !RAW_ERROR.test(said)) return said.trim()
  return FLOW_FAILED[kind] || FLOW_FAILED.build
}

/** The job view inside a POST/poll answer, or null (an old backend answers the step directly). */
export function jobOf(res) {
  const d = res && res.ok && res.data
  if (!d || typeof d !== 'object') return null
  if (d.job && typeof d.job === 'object' && d.job.state) return d.job
  if (typeof d.state === 'string' && typeof d.id === 'string') return d
  return null
}

/**
 * Backend /flow/* answer → the next flow state (pure: easy to test). `res` is a step's own answer:
 * a success, or a failure the backend described. A failure shows only the step's plain-language reason
 * (or the kind FLOW_FAILED line) with a Try again button; transport errors never get here.
 */
export function flowNext(prev, kind, res, retry = null) {
  const p = prev || { stage: 'idle' }
  if (!res || !res.ok || (res.data && res.data.ok === false)) {
    return { ...p, working: null, progress: null, error: friendlyFailure(kind, res), retry }
  }
  const d = res.data || {}
  const timing = { ...(p.timing || {}), [kind]: d.timing || null }
  if (kind === 'meet') {
    return { stage: 'portrait', working: null, error: null, portrait: d.portrait || '', portraitBy: d.portrait_by,
      claims: Array.isArray(d.claims) ? d.claims : [], calibrationReady: !!d.calibration_ok, timing }
  }
  if (kind === 'verdict') {
    const next = d.portrait ? { portrait: d.portrait, portraitBy: d.portrait_by, revised: true } : {}
    return { ...p, ...next, stage: d.portrait ? 'portrait' : (p.calibrationReady ? 'calibration' : 'arrange'),
      verdict: d.verdict, working: null, error: null, unresolved: d.unresolved || [], timing }
  }
  if (kind === 'build') {
    const links = (d.playlists || []).filter(x => x && x.url)
    const short = (d.slots || []).filter(s => s && (s.short || s.skipped)).map(s => s.slot)
    return { ...p, stage: 'done', working: null, error: null, links, short, timing }
  }
  return p
}

/** Backend /tune-in answer → the small card badge (never a nag: nothing at all when not eligible). */
export function tuneInView(res) {
  const d = res && res.ok && res.data
  if (!d || !d.eligible) return { show: false }
  const round = d.round && Array.isArray(d.round.tracks) ? d.round : null
  return { show: true, available: !!d.available, line: d.line || '', round,
    reacted: round ? round.tracks.filter(t => t.reaction).length : 0 }
}

/**
 * Backend /demo answer → the demo strip (demo_mode, for recordings/testing). Real values only: no
 * line for a part that has no data yet, and nothing at all when the setting is off.
 */
export function demoView(res) {
  const d = res && res.ok && res.data
  if (!d || !d.on) return { show: false, lines: [] }
  const a = d.archive
  const lines = []
  if (a) {
    const keepers = Number(a.keepers) || 0
    lines.push(`Last Archive refresh: ${Number(a.added) || 0} added · ${Number(a.expired) || 0} aged out · ${keepers} keeper${keepers === 1 ? '' : 's'}`)
  }
  if (d.taste && d.taste.line) lines.push(String(d.taste.line))
  return { show: lines.length > 0, lines }
}

/** Client-side note check (the backend validates again). null = fine. */
export function noteError(text) {
  const t = String(text || '').trim()
  return t.length > NOTE_MAX ? `Keep it to ${NOTE_MAX} characters (now ${t.length}).` : null
}

// -- setup card --

/** The guided wizard, one step open at a time. [id, number, title]. */
export const SETUP_STEPS = [
  ['dashboard', '1', 'Open the Spotify Dashboard'],
  ['app', '2', 'Create the app'],
  ['users', '3', 'Add yourself under User Management'],
  ['client_id', '4', 'Paste the Client ID'],
  ['connect', '5', 'Connect'],
  ['check', '6', 'Check the connection'],
  ['meet', '7', 'Get to know me']
]
/** Steps only the user can confirm (Spotify's site is not ours to read); ticks are kept in ctx.storage. */
export const MANUAL_STEPS = ['dashboard', 'app', 'users']
export const STATUS_LABEL = { done: 'Done', current: 'Now', todo: 'Next', error: 'Needs a fix', warn: 'Done, with a note' }
/** Problems the probe cannot see (they happen in the terminal or on Spotify's page): offered under step 5. */
export const SIGNIN_TROUBLE = [
  ['redirect', 'Spotify says "This redirect URI is not secure" or "Invalid redirect URI"'],
  ['port_busy', 'The terminal says the port is in use (or "could not bind")'],
  ['refused', 'Spotify says "INVALID_CLIENT" or the sign-in failed']
]
export const CLIENT_ID_ERRORS = {
  empty: 'Paste the Client ID first.',
  shape: "That doesn't look like a Client ID: it is 32 letters and digits, on your app's Settings page under Basic Information. Not the Client secret."
}

/** Local check before saving (the backend checks again): null when fine, else the plain-language error. */
export function clientIdError(text) {
  const t = String(text || '').trim()
  if (!t) return CLIENT_ID_ERRORS.empty
  return /^[0-9A-Fa-f]{32}$/.test(t) ? null : CLIENT_ID_ERRORS.shape
}

/**
 * The wizard's state machine: backend /setup answer + the user's own ticks → one status per step.
 * Statuses: 'done' | 'current' | 'todo' | 'error' | 'warn'. Exactly one step is open (`current`; an
 * `error` step is open too). The backend is the truth for 4–6: a saved Client ID means 1–4 are done,
 * whatever was ticked; a working connection means 1–6 are done. `show` says whether the card replaces
 * the remote: until Spotify answers, and once more for step 7 until the user has onboarded or dismissed it.
 */
export function setupView(state, dismissed = false, marks = {}) {
  const s = state || {}
  const step = s.step || 'loading'
  const fixes = { ...SETUP_FIXES, ...(s.fixes || {}) }
  const order = SETUP_STEPS.map(([id]) => id)
  const status = Object.fromEntries(order.map(id => [id, 'todo']))
  let problem = null
  let problemKind = null
  const doneUpTo = id => { for (const x of order.slice(0, order.indexOf(id))) status[x] = 'done' }
  if (step === 'client_id') {
    const next = MANUAL_STEPS.find(id => !marks[id]) || 'client_id'
    for (const id of MANUAL_STEPS) if (marks[id]) status[id] = 'done'
    status[next] = 'current'
  } else if (step === 'signin') {
    doneUpTo('connect')
    status.connect = 'current'
  } else if (step === 'done') {
    doneUpTo('meet')
    status.check = s.problem === 'no_premium' ? 'warn' : 'done'
    status.meet = s.onboarded ? 'done' : 'current'
    if (s.problem === 'no_premium') { problemKind = 'no_premium'; problem = s.note || fixes.no_premium }
  } else if (step === 'offline') { // Loshify's own backend did not answer: nothing is known yet
    problemKind = 'offline'
    problem = s.next_step || "Loshify's backend did not answer. Is the Loshify plugin enabled for this profile?"
  } else if (step !== 'loading') { // app / premium_or_users / internal / retry: the check failed
    doneUpTo('check')
    status.check = 'error'
    problemKind = s.problem || ({ app: 'refused', premium_or_users: 'not_listed', internal: 'hermes' }[step] || 'no_answer')
    problem = s.fix || fixes[problemKind] || s.next_step || fixes.no_answer
  }
  const open = order.find(id => status[id] === 'current' || status[id] === 'error') || null
  const ready = !!s.ready
  const show = !ready || (!s.onboarded && !dismissed)
  const doneCount = order.filter(id => status[id] === 'done' || status[id] === 'warn').length
  return { step, open, current: open, status, problem, problemKind, ready, show, note: s.note || null, fixes, doneCount, total: order.length }
}

export const CSS = `
.lfy-chip{display:flex;align-items:center;gap:1px;height:100%;color:var(--ui-text-tertiary)}
.lfy-chip button[disabled]{opacity:.4}
.lfy-label{display:block;max-width:190px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:11px}
.lfy-label[data-mode=last],.lfy-label[data-mode=none],.lfy-label[data-mode=error],.lfy-label[data-mode=setup]{font-style:italic;color:var(--ui-text-tertiary)}
.lfy-emoji{font-size:13px;line-height:1;font-family:"Segoe UI Emoji","Apple Color Emoji","Noto Color Emoji",sans-serif}
.lfy-big .lfy-emoji{font-size:20px}
.lfy-action{display:inline-flex;align-items:center;justify-content:center;color:inherit}
.lfy-action[data-on=true]{color:var(--ui-accent)}
/* The SDK PopoverContent is a fixed narrow box. The doubled class (our own element, our own class)
   outranks the utility layer, so the card owns its width here; everything inside is border-box. */
.lfy-pop.lfy-pop{width:360px;max-width:calc(100vw - 16px);padding:0;overflow:hidden}
.lfy-panel,.lfy-panel *{box-sizing:border-box}
.lfy-panel{width:100%;min-width:0;display:flex;flex-direction:column}
.lfy-panel>*{min-width:0;max-width:100%}
.lfy-tabs{display:flex;gap:2px;padding:6px 8px 0}
.lfy-tab{flex:1;min-width:0;border:1px solid transparent;background:transparent;color:var(--ui-text-tertiary);font-size:11px;padding:3px 0;cursor:pointer;border-radius:4px}
.lfy-tab:hover{color:var(--ui-text-primary)}
.lfy-tab[data-active=true]{color:var(--ui-text-primary);border-color:var(--ui-border);background:var(--ui-bg-tertiary,rgba(127,127,127,.08))}
.lfy-body{max-height:470px;overflow-y:auto;overflow-x:hidden;padding:10px}
.lfy-now{display:flex;gap:10px;align-items:center;min-width:0}
.lfy-art{width:56px;height:56px;border-radius:4px;background:rgba(127,127,127,.14);display:grid;place-items:center;font-size:20px;flex-shrink:0;overflow:hidden}
.lfy-art img{width:100%;height:100%;object-fit:cover;display:block}
.lfy-meta{min-width:0;flex:1}
.lfy-mode{font-size:10px;color:var(--ui-text-tertiary)}
.lfy-mode[data-mode=last]{color:var(--ui-accent);font-weight:600}
.lfy-title{font-size:12.5px;font-weight:600;color:var(--ui-text-primary);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-sub{font-size:11px;color:var(--ui-text-secondary);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-device{font-size:10px;color:var(--ui-text-tertiary);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-managed{font-size:10px;color:var(--ui-accent)}
.lfy-progress{display:flex;align-items:center;gap:6px;margin-top:8px;direction:ltr}
.lfy-time{font-size:9.5px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;flex-shrink:0}
.lfy-seek{flex:1;min-width:0;height:14px;display:flex;align-items:center;position:relative;cursor:pointer;touch-action:none;direction:ltr;outline:none;border-radius:3px}
.lfy-seek[aria-disabled=true]{cursor:default;opacity:.6}
.lfy-seek:focus-visible{box-shadow:0 0 0 2px var(--ui-accent)}
.lfy-bar{flex:1;height:3px;background:rgba(127,127,127,.25);overflow:hidden;border-radius:2px;pointer-events:none;transition:height .1s}
.lfy-seek:not([aria-disabled=true]):hover .lfy-bar,.lfy-seek[data-scrubbing=true] .lfy-bar{height:5px}
.lfy-bar>span{display:block;height:100%;background:var(--ui-accent)}
.lfy-thumb{position:absolute;top:50%;width:10px;height:10px;margin-left:-5px;margin-top:-5px;border-radius:50%;background:var(--ui-accent);pointer-events:none;opacity:0;transition:opacity .1s}
.lfy-seek:not([aria-disabled=true]):hover .lfy-thumb,.lfy-seek:focus-visible .lfy-thumb,.lfy-seek[data-scrubbing=true] .lfy-thumb{opacity:1}
.lfy-transport{display:flex;align-items:center;justify-content:center;gap:4px;margin-top:6px;direction:ltr}
.lfy-reactions{display:flex;justify-content:center;gap:14px;margin-top:10px;direction:ltr}
.lfy-note{margin-top:8px;font-size:12px;width:100%;min-height:44px;max-height:140px;resize:vertical}
.lfy-row{display:flex;gap:6px;margin-top:6px;width:100%}
.lfy-row>*{flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;justify-content:center}
.lfy-saved{margin-top:6px;font-size:10.5px;color:var(--ui-text-secondary);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-volume{display:flex;align-items:center;gap:6px;margin-top:8px;color:var(--ui-text-tertiary);direction:ltr}
.lfy-volume input{flex:1;min-width:0;accent-color:var(--ui-accent);height:3px}
.lfy-section{margin-top:10px}
.lfy-heading{font-size:9.5px;letter-spacing:.04em;color:var(--ui-text-tertiary);margin-bottom:4px}
.lfy-list{display:flex;flex-direction:column}
.lfy-item{display:flex;align-items:center;gap:6px;width:100%;min-width:0;border:0;background:transparent;color:var(--ui-text-secondary);font-size:11px;padding:4px 6px;cursor:pointer;text-align:start;border-radius:4px}
.lfy-item:hover{background:var(--ui-bg-tertiary,rgba(127,127,127,.08));color:var(--ui-text-primary)}
.lfy-item[data-active=true]{color:var(--ui-accent)}
.lfy-item-main{min-width:0;flex:1;display:flex;flex-direction:column}
.lfy-item-title{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-item-sub{font-size:10px;color:var(--ui-text-tertiary);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-item-actions{display:flex;gap:2px;flex-shrink:0;opacity:.35}
.lfy-item:hover .lfy-item-actions{opacity:1}
.lfy-status{font-size:10px;color:var(--ui-text-tertiary);padding:4px 10px;border-top:1px solid var(--ui-border);min-height:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.lfy-keys{margin-top:10px;padding-top:8px;border-top:1px solid var(--ui-stroke-tertiary,var(--ui-border));display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;font-size:10px;color:var(--ui-text-tertiary)}
.lfy-key{display:flex;align-items:center;gap:6px;min-width:0;white-space:nowrap}
.lfy-setup{display:flex;flex-direction:column;gap:8px}
.lfy-setup-intro{font-size:11px;color:var(--ui-text-secondary)}
.lfy-step{border:1px solid var(--ui-border);border-radius:6px;padding:8px;display:flex;flex-direction:column;gap:6px}
.lfy-step[data-state=current]{border-color:var(--ui-accent)}
.lfy-step[data-state=error]{border-color:var(--ui-danger,#d33)}
.lfy-step[data-state=todo]{opacity:.55}
.lfy-step-head{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--ui-text-primary)}
.lfy-step-badge{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;font-size:10px;background:rgba(127,127,127,.18);flex-shrink:0}
.lfy-step[data-state=done] .lfy-step-badge{background:var(--ui-accent);color:var(--ui-bg-primary,#fff)}
.lfy-step-text{font-size:11px;color:var(--ui-text-secondary)}
.lfy-step-problem{font-size:11px;color:var(--ui-danger,#d33)}
.lfy-step-why{font-size:10.5px;color:var(--ui-text-tertiary);font-style:italic}
.lfy-step-title{flex:1;min-width:0}
.lfy-step-status{font-size:10px;font-weight:400;color:var(--ui-text-tertiary);flex-shrink:0}
.lfy-step[data-state=error] .lfy-step-status{color:var(--ui-danger,#d33)}
.lfy-step[data-state=warn] .lfy-step-badge{background:var(--ui-warning,#c90);color:var(--ui-bg-primary,#fff)}
.lfy-trouble{display:flex;flex-direction:column;gap:2px}
.lfy-trouble .lfy-item{font-size:11px;text-align:left}
.lfy-trouble .lfy-item[data-on=true]{outline:1px solid var(--ui-accent)}
.lfy-client-id{font-family:var(--ui-font-mono,monospace);font-size:11px}
.lfy-copy{display:flex;align-items:center;gap:6px;font-size:11px;min-width:0}
.lfy-copy-label{color:var(--ui-text-tertiary);flex-shrink:0;width:82px}
.lfy-arrange{min-height:0}
.lfy-cal-actions{display:flex;gap:2px;flex-shrink:0;direction:ltr}
.lfy-portrait{font-size:13px;line-height:1.5;padding:6px 0;unicode-bidi:plaintext}
.lfy-flow .lfy-item[data-on=true]{outline:1px solid var(--ui-accent)}
.lfy-tunein-badge{display:flex;align-items:center;gap:6px;padding:2px 8px;justify-content:flex-end}
.lfy-demo{display:flex;flex-direction:column;align-items:flex-end;gap:1px;padding:0 8px 2px}
.lfy-cal-btn[data-on=true]{background:var(--ui-bg-tertiary,rgba(127,127,127,.16));outline:1px solid var(--ui-accent)}
.lfy-rotation{margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--ui-border)}
.lfy-copy code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10.5px;padding:2px 4px;border-radius:3px;background:rgba(127,127,127,.12)}
`

const svgProps = { 'aria-hidden': true, fill: 'none', strokeLinecap: 'round', strokeLinejoin: 'round', stroke: 'currentColor', strokeWidth: 2, viewBox: '0 0 24 24' }
const IcNext = () => jsxs('svg', { ...svgProps, style: { width: 14, height: 14 }, children: [jsx('path', { d: 'M6 5l8.5 7L6 19z', fill: 'currentColor', stroke: 'none' }), jsx('path', { d: 'M18 5v14' })] })
const IcPrev = () => jsxs('svg', { ...svgProps, style: { width: 14, height: 14 }, children: [jsx('path', { d: 'M18 5l-8.5 7L18 19z', fill: 'currentColor', stroke: 'none' }), jsx('path', { d: 'M6 5v14' })] })
const IcShuffle = () => jsxs('svg', { ...svgProps, style: { width: 13, height: 13 }, children: [jsx('path', { d: 'M16 3h5v5' }), jsx('path', { d: 'M4 20L21 3' }), jsx('path', { d: 'M21 16v5h-5' }), jsx('path', { d: 'M15 15l6 6' }), jsx('path', { d: 'M4 4l5 5' })] })
const IcRepeat = () => jsxs('svg', { ...svgProps, style: { width: 13, height: 13 }, children: [jsx('path', { d: 'm17 2 4 4-4 4' }), jsx('path', { d: 'M3 11v-1a4 4 0 0 1 4-4h14' }), jsx('path', { d: 'm7 22-4-4 4-4' }), jsx('path', { d: 'M21 13v1a4 4 0 0 1-4 4H3' })] })

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export function createStore(ctx, { pollMs = JOB_POLL_MS } = {}) {
  const player = atom(null)
  const card = atom({ mode: 'none', track: null, error: null })
  const open = atom(false)
  const view = atom('now')
  const draft = atom('')
  const saved = atom(null) // { id, reaction, title, track_uri }
  const busy = atom(false)
  const status = atom('')
  const devices = atom([])
  const playlists = atom({ loading: false, items: [], error: null })
  const queue = atom({ loading: false, items: [], current: null, error: null })
  const searchText = atom('')
  const results = atom({ loading: false, items: [], query: '', error: null })
  const scrub = atom(null) // { ms } while the bar is being dragged; nothing is sent until release
  const setup = atom(null) // the last /setup answer (null = not asked yet)
  const stored = (key, fallback) => { try { return ctx.storage && ctx.storage.get ? ctx.storage.get(key, fallback) : fallback } catch { return fallback } }
  const meetDismissed = atom(!!stored('meetDismissed', false))
  const setupMarks = atom({ ...(stored('setupMarks', {}) || {}) }) // wizard steps 1–3 the user ticked
  const clientIdDraft = atom('')
  const clientIdState = atom({ busy: false, error: null }) // step 4
  const checking = atom(false) // step 6 "Check now"
  const trouble = atom(null) // step 5: which sign-in problem the user picked (SIGNIN_TROUBLE id)
  const arrange = atom('') // "Describe how you like your music arranged" (setup card + Playlists tab)
  const rotation = atom({ state: 'loading', presets: [] })
  const onboarding = atom({ state: 'loading', tracks: [], anchors: [], note: '' })
  const noteDraft = atom(null) // null = not edited (show the saved note)
  const flow = atom({ stage: 'idle', working: null, error: null }) // the click-through "Get to know me"
  const anchorsDraft = atom('')
  const flowPreset = atom(null) // the preset card the user picked (or null = their own words)
  const tuneIn = atom({ show: false })
  const tuneJob = atom({ working: false, progress: null, error: null })
  const demo = atom({ show: false, lines: [] }) // demo_mode: last Archive counts + taste line

  let pollOpenMs = 3000
  let pollClosedMs = 5000
  let lastFetch = 0
  let lastSetup = 0
  let lastTick = Date.now()
  let inFlight = false
  let searchTimer = null
  let volumeTimer = null
  let keyTarget = null
  let keyTimer = null
  let seekMark = null // { ms, at, uri }: a poll that left before the seek landed must not snap the bar back

  /** Keep the optimistic position for a moment after a seek (Spotify reports the old one briefly). */
  function reconcileSeek(data) {
    if (!seekMark || !data || !data.item) return data
    const age = Date.now() - seekMark.at
    if (age > 2500 || data.item.uri !== seekMark.uri) { seekMark = null; return data }
    const expected = clampMs(seekMark.ms + (data.is_playing ? age : 0), data.item.duration_ms)
    if (Math.abs((data.progress_ms || 0) - expected) <= 2000) { seekMark = null; return data }
    return { ...data, progress_ms: expected }
  }

  // -- setup ---------------------------------------------------------------

  async function refreshSetup() {
    lastSetup = Date.now()
    try {
      const res = await ctx.rest('/setup')
      setup.set(res && typeof res === 'object' ? res : { ready: false, step: 'retry' })
    } catch (err) {
      setup.set({ ready: false, step: 'offline', next_step: `Loshify's backend did not answer (${errText(err)}). Is the Loshify agent plugin enabled for this profile?` })
    }
    return setup.get()
  }

  /** Wizard steps 1–3 happen on Spotify's site: the user ticks them ("Done"), and can untick. */
  function markStep(id, done = true) {
    if (!MANUAL_STEPS.includes(id)) return false
    const next = { ...setupMarks.get(), [id]: !!done }
    if (!done) for (const later of MANUAL_STEPS.slice(MANUAL_STEPS.indexOf(id))) next[later] = false
    setupMarks.set(next)
    try { if (ctx.storage && ctx.storage.set) ctx.storage.set('setupMarks', next) } catch { /* best effort */ }
    return true
  }

  /** Step 4: check the pasted Client ID, save it into the plugin setting through the backend, re-check. */
  async function saveClientId() {
    const text = clientIdDraft.get().trim()
    const local = clientIdError(text)
    if (local) { clientIdState.set({ busy: false, error: local }); return false }
    clientIdState.set({ busy: true, error: null })
    let res
    try {
      res = await ctx.rest('/setup/client-id', { method: 'POST', body: { client_id: text } })
    } catch (err) {
      res = { ok: false, error: `Couldn't reach Loshify (${errText(err)}). Paste it in Settings → Plugins → Loshify instead.` }
    }
    if (!res || !res.ok) {
      clientIdState.set({ busy: false, error: (res && res.error) || 'Not saved.' })
      return false
    }
    clientIdDraft.set('')
    clientIdState.set({ busy: false, error: null })
    if (res.setup && typeof res.setup === 'object') setup.set(res.setup)
    else await refreshSetup()
    host.notify({ kind: 'success', message: 'Client ID saved' })
    return true
  }

  /** Step 6: one real call to Spotify now, instead of waiting for the next poll. */
  async function checkNow() {
    checking.set(true)
    try {
      const s = await refreshSetup()
      if (s && s.ready) await refresh()
      return !!(s && s.ready)
    } finally {
      checking.set(false)
    }
  }

  function setupReady() {
    const s = setup.get()
    return !!(s && s.ready)
  }

  async function copy(text, what) {
    let ok = false
    try {
      ok = !!(ctx.os && ctx.os.writeClipboard && (await ctx.os.writeClipboard(text)))
    } catch {
      ok = false
    }
    host.notify(ok ? { kind: 'success', message: `Copied: ${what}` } : { kind: 'warning', message: `Could not copy. ${what}: ${text}` })
    return ok
  }

  async function openExternal(url) {
    try {
      if (ctx.os && ctx.os.openExternal && (await ctx.os.openExternal(url))) return true
    } catch {
      // fall through to the toast
    }
    host.notify({ kind: 'info', message: `Open ${url} in your browser.` })
    return false
  }

  function openSettings() {
    try {
      host.navigate(SETTINGS_ROUTE)
      open.set(false)
      return true
    } catch {
      host.notify({ kind: 'info', message: 'Open Settings → Plugins → Loshify and paste the Client ID there.' })
      return false
    }
  }

  /** Put *text* into the focused chat exactly as if the user typed it; false (and a hint) without a chat. */
  function sendToChat(text) {
    const sent = !!(host.composer && host.composer.submit && host.composer.submit(null, text))
    if (!sent) host.notify({ kind: 'info', message: `Open a chat, then ask: “${text}”` })
    else open.set(false)
    return sent
  }

  /** Step D: the first-run request (plus the arrangement, if the user described one) into the open chat. */
  function meet() {
    if (!sendToChat(meetPrompt(arrange.get()))) return false
    arrange.set('')
    dismissMeet()
    return true
  }

  /** Rotation from the user's own words (or a preset name): the curator previews it and asks to confirm. */
  function sendArrangement(presetName = null) {
    const text = arrangePrompt(arrange.get(), presetName)
    if (!text) {
      host.notify({ kind: 'info', message: 'Describe how you like your music arranged first, or pick a preset.' })
      return false
    }
    if (!sendToChat(text)) return false
    arrange.set('')
    return true
  }

  /** "Refresh now": the curator writes candidates per slot, so the refresh goes through the chat. */
  function refreshRotation() {
    return sendToChat(REFRESH_PROMPT)
  }

  async function loadRotation() {
    const res = await ctx.rest('/rotation').catch(err => ({ ok: false, error: errText(err) }))
    rotation.set(rotationView(res))
    return rotation.get()
  }

  // -- onboarding: calibration round + note --------------------------------

  async function loadOnboarding() {
    const res = await ctx.rest('/onboarding').catch(err => ({ ok: false, error: errText(err) }))
    onboarding.set(onboardingView(res))
    return onboarding.get()
  }

  /** 👎/👌/❤️ on calibration track n: the normal reactions path; works without playback. */
  async function reactCalibration(n, reaction) {
    if (busy.get() || !Object.prototype.hasOwnProperty.call(REACTIONS, reaction)) return false
    busy.set(true)
    try {
      const res = await ctx.rest('/onboarding/react', { method: 'POST', body: { n, reaction } }).catch(err => ({ ok: false, error: errText(err) }))
      if (!res || !res.ok) {
        host.notify({ kind: 'error', message: `Not saved: ${(res && res.error) || 'error'}` })
        return false
      }
      const d = res.data || {}
      const cur = onboarding.get()
      onboarding.set({ ...cur, tracks: cur.tracks.map(t => (t.n === n ? { ...t, reaction } : t)), reacted: d.reacted ?? cur.reacted, pending: !d.complete })
      status.set(d.complete ? 'Calibration done: your curator learned from all five' : `${REACTIONS[reaction].emoji} saved · ${d.reacted || 0}/${d.total || 0}`)
      return true
    } finally {
      busy.set(false)
    }
  }

  async function saveNote() {
    const draftText = noteDraft.get()
    const text = (draftText === null ? onboarding.get().note : draftText).trim()
    const problem = noteError(text)
    if (problem) { host.notify({ kind: 'warning', message: problem }); return false }
    const res = await ctx.rest('/note', { method: 'POST', body: { text } }).catch(err => ({ ok: false, error: errText(err) }))
    if (!res || !res.ok) {
      host.notify({ kind: 'error', message: `Note not saved: ${(res && res.error) || 'error'}` })
      return false
    }
    noteDraft.set(null)
    onboarding.set({ ...onboarding.get(), note: (res.data && res.data.note) || '' })
    host.notify({ kind: 'success', message: text ? 'Saved: your curator will keep this in mind' : 'Note cleared' })
    return true
  }

  async function skipStep(step) {
    const res = await ctx.rest('/onboarding/skip', { method: 'POST', body: { step } }).catch(err => ({ ok: false, error: errText(err) }))
    if (res && res.ok) await loadOnboarding()
    return !!(res && res.ok)
  }

  // -- "Get to know me": click-through, no chat ----------------------------

  // ctx.setTimeout is disposed with the plugin; a disposed timer simply never fires (the card is gone).
  const sleep = ms => new Promise(resolve => { if (ctx.setTimeout) ctx.setTimeout(resolve, ms); else setTimeout(resolve, ms) })

  /**
   * A long step as a backend job: POST with a job id we chose, then poll /flow/job until it is done.
   * A transport error, abort or client timeout on either call only means "poll again"; the card keeps
   * its progress line. Only a failure the backend reports (or a job that never arrived) shows the
   * friendly line with Try again. An older backend that answers the step directly still works.
   */
  async function runJob(kind, path, body, onProgress = null) {
    let id = newJobId()
    const started = Date.now()
    const answer = await ctx.rest(path, { method: 'POST', body: { ...(body || {}), job: id } }).catch(() => null)
    if (answer && answer.ok && answer.data && !jobOf(answer)) return answer // an older backend: the step itself
    if (answer && !answer.ok && !jobOf(answer)) return answer // refused before starting (bad input): its reason
    let job = jobOf(answer) // null when the POST's answer was lost: the job may still be running
    if (job && job.id) id = job.id
    let unknown = 0
    for (;;) {
      if (job && job.state === 'done') return job.result || { ok: true, data: {} }
      if (job && job.state === 'failed') return { ok: false, data: { ok: false, error: job.message || null } }
      // "unknown" a few polls in a row: the start never reached the backend (nothing is running).
      if (job && job.state === 'unknown') { if (++unknown >= JOB_UNKNOWN_LIMIT) return { ok: false } } else unknown = 0
      if (job && job.progress) {
        if (onProgress) onProgress(job.progress)
        else flow.set({ ...flow.get(), progress: job.progress })
      }
      if (Date.now() - started > JOB_GIVE_UP_MS) return { ok: false }
      await sleep(pollMs)
      const polled = await ctx.rest(`/flow/job?id=${encodeURIComponent(id)}`).catch(() => null)
      job = jobOf(polled) || job // a failed poll keeps the last view and polls again
    }
  }

  async function flowCall(kind, path, body) {
    if (flow.get().working) return false
    const retry = { kind, path, body: body || {} }
    flow.set({ ...flow.get(), working: kind, progress: null, error: null, retry: null })
    const res = await runJob(kind, path, body).catch(() => ({ ok: false }))
    flow.set(flowNext(flow.get(), kind, res, retry))
    return !flow.get().error
  }

  /** "Try again" on the friendly failure line: the same step with the same input. */
  async function flowRetry() {
    const r = flow.get().retry
    if (!r) return false
    if (r.kind === 'meet') return flowStart()
    if (r.kind === 'build') return flowBuildWith(r.body)
    return flowCall(r.kind, r.path, r.body)
  }

  /** Start: portrait + calibration picks in one backend call. */
  async function flowStart() {
    const ok = await flowCall('meet', '/flow/meet')
    if (ok) await Promise.all([loadOnboarding(), loadRotation()]) // the round + the preset cards
    return ok
  }

  /** "Sounds like me" / "Not quite" (+ the optional anchors box). */
  async function flowVerdict(verdict) {
    const anchors = verdict === 'not_quite' ? anchorsFromText(anchorsDraft.get()) : []
    const ok = await flowCall('verdict', '/flow/verdict', { verdict, anchors })
    if (ok) anchorsDraft.set('')
    return ok
  }

  /** Portrait accepted after a rewrite, or calibration done: move on without a backend call. */
  function flowGo(stage) {
    if (busy.get() || !FLOW_STAGES.includes(stage)) return false
    flow.set({ ...flow.get(), stage, error: null })
    return true
  }

  /** "Build my playlists": the picked preset card or the user's own words, plus the note. */
  async function flowBuild() {
    if (busy.get()) return false
    const preset = flowPreset.get()
    const words = arrange.get().trim()
    if (!preset && !words) {
      host.notify({ kind: 'info', message: 'Pick an arrangement card, or describe your own.' })
      return false
    }
    const draftText = noteDraft.get()
    const note = (draftText === null ? onboarding.get().note || '' : draftText).trim()
    const problem = noteError(note)
    if (problem) { host.notify({ kind: 'warning', message: problem }); return false }
    return flowBuildWith(preset ? { preset, note } : { words, note })
  }

  async function flowBuildWith(body) {
    const ok = await flowCall('build', '/flow/build', body)
    if (ok) {
      arrange.set('')
      noteDraft.set(null)
      await Promise.all([refreshSetup(), loadRotation(), loadTuneIn()])
    }
    return ok
  }

  // -- Tune in: the optional recurring calibration round --------------------

  async function loadTuneIn() {
    const res = await ctx.rest('/tune-in').catch(err => ({ ok: false, error: errText(err) }))
    tuneIn.set(tuneInView(res))
    void loadDemo()
    return tuneIn.get()
  }

  /** demo_mode only: the last Archive refresh counts and the taste line (hidden when off or empty). */
  async function loadDemo() {
    const res = await ctx.rest('/demo').catch(() => null)
    demo.set(demoView(res))
    return demo.get()
  }

  async function startTuneIn() {
    if (busy.get()) return false
    busy.set(true)
    tuneJob.set({ working: true, progress: 'Finding your picks…', error: null })
    try {
      const res = await runJob('tune-in', '/tune-in/start', {}, progress => tuneJob.set({ working: true, progress, error: null })).catch(() => ({ ok: false }))
      if (!res || !res.ok || !res.data || !res.data.ok) {
        tuneJob.set({ working: false, progress: null, error: friendlyFailure('tune-in', res) })
        return false
      }
      tuneJob.set({ working: false, progress: null, error: null })
      await loadTuneIn()
      return true
    } finally {
      busy.set(false)
    }
  }

  async function reactTuneIn(n, reaction) {
    if (busy.get() || !Object.prototype.hasOwnProperty.call(REACTIONS, reaction)) return false
    busy.set(true)
    try {
      const res = await ctx.rest('/tune-in/react', { method: 'POST', body: { n, reaction } }).catch(err => ({ ok: false, error: errText(err) }))
      if (!res || !res.ok) {
        host.notify({ kind: 'error', message: `Not saved: ${(res && res.error) || 'error'}` })
        return false
      }
      const cur = tuneIn.get()
      if (cur.round) {
        const tracks = cur.round.tracks.map(t => (t.n === n ? { ...t, reaction } : t))
        tuneIn.set({ ...cur, round: { ...cur.round, tracks }, reacted: tracks.filter(t => t.reaction).length })
      }
      status.set(`${REACTIONS[reaction].emoji} saved: your next refresh learns from it`)
      return true
    } finally {
      busy.set(false)
    }
  }

  async function finishTuneIn() {
    const res = await ctx.rest('/tune-in/finish', { method: 'POST', body: {} }).catch(err => ({ ok: false, error: errText(err) }))
    await loadTuneIn()
    return !!(res && res.ok)
  }

  function dismissMeet() {
    meetDismissed.set(true)
    try { if (ctx.storage && ctx.storage.set) ctx.storage.set('meetDismissed', true) } catch { /* best effort */ }
  }

  /**
   * Taste tab → "Start over": reset the first-run state on the backend (reactions, anchors and the note
   * stay) and open the "Get to know me" flow from the top, right here in the Taste tab.
   */
  async function startOver() {
    if (flow.get().working || busy.get()) return false
    busy.set(true)
    try {
      const res = await ctx.rest('/onboarding/restart', { method: 'POST', body: {} }).catch(() => null)
      if (!res || !res.ok) {
        host.notify({ kind: 'info', message: 'Couldn’t start over right now. Try again in a moment.' })
        return false
      }
      flow.set({ stage: 'idle', working: null, progress: null, error: null, retry: null, rerun: true })
      flowPreset.set(null)
      anchorsDraft.set('')
      await loadOnboarding()
      return true
    } finally {
      busy.set(false)
    }
  }

  /** Close a re-run flow shown in the Taste tab (the first run closes via dismissMeet). */
  function closeRerun() {
    flow.set({ stage: 'idle', working: null, progress: null, error: null, retry: null, rerun: false })
    void loadOnboarding()
  }

  // -- polling -------------------------------------------------------------

  async function refresh() {
    lastFetch = Date.now()
    try {
      const res = await ctx.rest('/player')
      const s = res && res.settings
      if (s && Number(s.poll_interval_open) > 0) pollOpenMs = Number(s.poll_interval_open) * 1000
      if (s && Number(s.poll_interval_closed) > 0) pollClosedMs = Number(s.poll_interval_closed) * 1000
      card.set(cardFromPlayer(res))
      player.set(res && res.ok ? reconcileSeek(res.data) : null)
      // A setup-class error means the connection went away (revoked grant, Client ID removed).
      if (res && !res.ok && res.setup) void refreshSetup()
    } catch (err) {
      const prev = card.get()
      card.set(prev.track ? { ...prev, mode: 'last' } : { mode: 'error', track: null, error: errText(err) })
    }
  }

  /** 1s heartbeat: move the progress bar locally; fetch on the poll cadence (or poll setup until ready). */
  function tick() {
    const now = Date.now()
    if (!setupReady()) {
      // Setup card: re-poll every 2s while open (so it turns green on its own), slowly while closed.
      if (inFlight || now - lastSetup < (open.get() ? SETUP_POLL_MS : pollClosedMs * 6)) return
      inFlight = true
      void refreshSetup().then(s => (s && s.ready ? refresh() : null)).finally(() => { inFlight = false })
      return
    }
    const p = player.get()
    if (p && p.is_playing && p.item && p.item.duration_ms) {
      player.set({ ...p, progress_ms: Math.min(p.item.duration_ms, (p.progress_ms || 0) + (now - lastTick)) })
    }
    lastTick = now
    if (inFlight || now - lastFetch < (open.get() ? pollOpenMs : pollClosedMs)) return
    inFlight = true
    void refresh().finally(() => { inFlight = false })
  }

  async function start() {
    const s = await refreshSetup()
    if (s && s.ready) await refresh()
  }

  // -- reactions -----------------------------------------------------------

  async function undo(eventId) {
    const res = await ctx.rest('/undo', { method: 'POST', body: { event_id: eventId } }).catch(err => ({ ok: false, error: errText(err) }))
    if (!res || !res.ok) {
      host.notify({ kind: 'error', message: `Undo failed: ${(res && res.error) || 'error'}` })
      return false
    }
    const back = res.data && res.data.restored && res.data.restored.status === 'restored'
    host.notify({ kind: 'success', message: back ? 'Undone: the track is back in its playlist and the 👎 is gone' : 'Undone: the 👎 is gone' })
    const last = saved.get()
    if (last && last.id === eventId) saved.set(null)
    return true
  }

  /** Record a reaction for the track the card shows right now. */
  async function react(reaction, noteOverride, amends = null) {
    if (busy.get()) return false
    const current = card.get()
    const note = typeof noteOverride === 'string' ? noteOverride : draft.get()
    const body = feedbackBody(current, reaction, note, amends)
    if (!body) {
      host.notify({ kind: 'warning', message: 'Nothing to react to right now' })
      return false
    }
    busy.set(true)
    try {
      const res = await ctx.rest('/feedback', { method: 'POST', body })
      if (!res || !res.ok) {
        host.notify({ kind: 'error', message: `Not saved: ${(res && res.error) || 'error'}` })
        if (res && /not shown/.test(res.error || '')) void refresh()
        return false
      }
      // A late note keeps pointing at the ORIGINAL reaction, so further notes amend it too.
      saved.set({ id: amends || res.data.id, reaction, title: current.track.title, track_uri: body.track_uri })
      draft.set('')
      if (reaction === 'down' && !amends) {
        host.notify({
          kind: 'info',
          message: describeDislike(current.track.title, res.data.actions),
          durationMs: 10000,
          action: res.data.undoable ? { label: 'Undo', onClick: () => { void undo(res.data.id) } } : undefined
        })
        setTimeout(() => { void refresh() }, 800)
      } else {
        host.notify({ kind: 'success', message: `${REACTIONS[reaction].emoji} ${amends ? 'Note added' : 'Saved'} · ${current.track.title}` })
      }
      return true
    } catch (err) {
      host.notify({ kind: 'error', message: `Not saved: ${errText(err)}` })
      return false
    } finally {
      busy.set(false)
    }
  }

  /** A note typed after the reaction: amends that reaction. */
  async function saveLateNote() {
    const last = saved.get()
    const current = card.get()
    if (!last || !current.track || current.track.track_uri !== last.track_uri || !draft.get().trim()) return false
    return react(last.reaction, undefined, last.id)
  }

  // -- remote ----------------------------------------------------------------

  async function refreshDevices() {
    try {
      const res = await ctx.rest('/devices')
      if (res && res.ok) {
        devices.set(((res.data && res.data.devices) || []).map(d => ({ active: !!d.is_active, id: d.id, name: d.name, type: d.type, volume: d.volume_percent })))
      }
    } catch {
      // best effort
    }
  }

  /** Active device first, then any reported device, then the last known one. */
  async function targetDevice() {
    const p = player.get()
    if (p && p.device && p.device.id && p.device.is_active) return p.device.id
    await refreshDevices()
    const list = devices.get()
    const active = list.find(d => d.active)
    if (active) return active.id
    if (list.length) return list[0].id
    return p && p.device ? p.device.id : undefined
  }

  async function command(path, method, body, failText) {
    status.set('')
    try {
      const res = await ctx.rest(path, { method, body })
      if (!res || !res.ok) status.set((res && res.error) || failText)
      return res
    } catch (err) {
      status.set(errText(err))
      return null
    }
  }

  async function transport(action) {
    const device_id = await targetDevice()
    await command('/' + action, 'POST', { device_id }, 'The command failed')
    setTimeout(() => { void refresh() }, 350)
  }

  async function playPayload(payload) {
    const device_id = await targetDevice()
    const res = await command('/play', 'POST', { ...payload, device_id }, 'Could not start playback')
    if (res && res.ok) {
      await new Promise(r => setTimeout(r, 1200))
      await refresh()
      const p = player.get()
      if (!p || !p.is_playing) status.set('Spotify accepted the command but the device did not start. Reopen the Spotify app and try again.')
    }
  }

  // -- seek (never a reaction, never a skip: only /seek is called) -----------

  function duration() {
    const p = player.get()
    return knownDuration(p && p.item && p.item.duration_ms)
  }

  /** The position the bar shows right now (scrub > pending key target > playback). */
  function shownMs() {
    const s = scrub.get()
    if (s) return s.ms
    if (keyTarget !== null) return keyTarget
    const p = player.get()
    return clampMs((p && p.progress_ms) || 0, duration())
  }

  /** Commit one seek. Clamped to [0, duration]; refused while the duration is unknown. */
  async function seekTo(ms) {
    const p = player.get()
    const d = duration()
    if (!p || !p.item || !d) return false
    const target = clampMs(ms, d)
    player.set({ ...p, progress_ms: target })
    seekMark = { ms: target, at: Date.now(), uri: p.item.uri }
    lastTick = Date.now()
    const device_id = p.device && p.device.id ? p.device.id : undefined
    const res = await command('/seek', 'POST', { position_ms: target, track_uri: p.item.uri || undefined, device_id }, 'Seek failed')
    if (!res || !res.ok) seekMark = null
    setTimeout(() => { void refresh() }, 700)
    return !!(res && res.ok)
  }

  function beginScrub(ms) {
    if (!duration() || ms === null || ms === undefined) return false
    scrub.set({ ms: clampMs(ms, duration()) })
    return true
  }

  function moveScrub(ms) {
    if (!scrub.get() || ms === null || ms === undefined) return
    scrub.set({ ms: clampMs(ms, duration()) })
  }

  /** Release: the single commit point of a click or a drag. */
  function endScrub(ms) {
    const s = scrub.get()
    if (!s) return Promise.resolve(false)
    const target = ms === null || ms === undefined ? s.ms : clampMs(ms, duration())
    scrub.set(null)
    return seekTo(target)
  }

  function cancelScrub() {
    scrub.set(null)
  }

  /** Arrow keys: ±5s per press; rapid presses accumulate and commit once. */
  function nudge(delta) {
    if (!delta || !duration()) return false
    keyTarget = clampMs(shownMs() + delta, duration())
    const p = player.get()
    if (p) player.set({ ...p, progress_ms: keyTarget })
    clearTimeout(keyTimer)
    keyTimer = setTimeout(() => {
      const target = keyTarget
      keyTarget = null
      if (target !== null) void seekTo(target)
    }, KEY_SEEK_DEBOUNCE_MS)
    return true
  }

  function setVolume(pct) {
    const p = player.get()
    if (p && p.device) player.set({ ...p, device: { ...p.device, volume_percent: pct } })
    clearTimeout(volumeTimer)
    volumeTimer = setTimeout(async () => {
      const device_id = await targetDevice()
      await command('/volume', 'PUT', { volume_percent: pct, device_id }, 'Volume change failed')
    }, 200)
  }

  async function toggleShuffle() {
    const p = player.get()
    await command('/shuffle', 'PUT', { state: !(p && p.shuffle) }, 'Shuffle failed')
    await refresh()
  }

  async function cycleRepeat() {
    const order = ['off', 'context', 'track']
    const p = player.get()
    await command('/repeat', 'PUT', { state: order[(order.indexOf((p && p.repeat) || 'off') + 1) % 3] }, 'Repeat failed')
    await refresh()
  }

  async function transfer(deviceId) {
    await command('/transfer', 'PUT', { device_id: deviceId, play: true }, 'Could not move playback')
    await Promise.all([refresh(), refreshDevices()])
  }

  async function addToQueue(uri) {
    const device_id = await targetDevice()
    const res = await command('/queue', 'POST', { uri, device_id }, 'Could not add to the queue')
    if (res && res.ok) status.set('Added to the queue')
  }

  async function loadPlaylists() {
    playlists.set({ ...playlists.get(), loading: true })
    const res = await ctx.rest('/playlists').catch(err => ({ ok: false, error: errText(err) }))
    playlists.set({ loading: false, items: res && res.ok ? (res.data && res.data.items) || [] : [], error: res && !res.ok ? res.error : null })
  }

  async function loadQueue() {
    queue.set({ ...queue.get(), loading: true })
    const res = await ctx.rest('/queue').catch(err => ({ ok: false, error: errText(err) }))
    const d = res && res.ok ? res.data || {} : {}
    queue.set({ loading: false, items: d.queue || [], current: d.currently_playing || null, error: res && !res.ok ? res.error : null })
  }

  async function runSearch(q) {
    if (q.length < 2) { results.set({ loading: false, items: [], query: q, error: null }); return }
    results.set({ ...results.get(), loading: true, query: q })
    const res = await ctx.rest(`/search?q=${encodeURIComponent(q)}&limit=10`).catch(err => ({ ok: false, error: errText(err) }))
    if (searchText.get().trim() !== q) return // a newer query is on its way
    const items = res && res.ok && res.data && res.data.tracks ? res.data.tracks.items || [] : []
    results.set({ loading: false, items, query: q, error: res && !res.ok ? res.error : null })
  }

  function setSearch(text) {
    searchText.set(text)
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => { void runSearch(text.trim()) }, 350)
  }

  function selectView(v) {
    view.set(v)
    if (!setupReady()) return
    if (v === 'playlists') { void loadPlaylists(); void loadRotation() }
    if (v === 'taste') void loadOnboarding()
    if (v === 'queue') void loadQueue()
    if (v === 'now') void refreshDevices()
  }

  function setOpen(v) {
    open.set(v)
    if (!v) return
    if (!setupReady()) { void refreshSetup(); return }
    void refresh()
    selectView(view.get())
    const s = setup.get()
    if (s && s.onboarded) void loadTuneIn()
  }

  return {
    player, card, open, view, draft, saved, busy, status, devices, playlists, queue, searchText, results, scrub,
    setup, meetDismissed, arrange, rotation, onboarding, noteDraft, flow, anchorsDraft, flowPreset, tuneIn, tuneJob, demo, loadDemo,
    setupMarks, clientIdDraft, clientIdState, checking, trouble, markStep, saveClientId, checkNow,
    loadOnboarding, reactCalibration, saveNote, skipStep,
    flowStart, flowVerdict, flowGo, flowBuild, flowRetry, startOver, closeRerun, loadTuneIn, startTuneIn, reactTuneIn, finishTuneIn,
    start, refresh, refreshSetup, tick, react, saveLateNote, undo, refreshDevices, transport, playPayload, setVolume,
    toggleShuffle, cycleRepeat, transfer, addToQueue, loadPlaylists, loadQueue, runSearch, setSearch, selectView,
    setOpen, duration, shownMs, seekTo, beginScrub, moveScrub, endScrub, cancelScrub, nudge,
    copy, openExternal, openSettings, meet, dismissMeet, sendToChat, sendArrangement, refreshRotation, loadRotation
  }
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

function SmallAction({ label, icon, onClick, on = false, disabled = false }) {
  return jsx(Tip, { label, children: jsx(Button, {
    'aria-label': label, className: 'lfy-action', 'data-on': on, disabled, onClick, size: 'icon-xs', variant: 'ghost',
    children: jsx(icon, {})
  }) })
}

function EmojiButton({ reaction, store, disabled, big = false }) {
  const r = REACTIONS[reaction]
  return jsx(Tip, { label: r.tip, children: jsx(Button, {
    'aria-label': r.tip, 'data-reaction': reaction, className: big ? 'lfy-big' : undefined, disabled,
    size: big ? 'icon-sm' : 'micro', variant: 'ghost', onClick: () => { void store.react(reaction, big ? undefined : '') },
    children: jsx('span', { className: 'lfy-emoji', children: r.emoji })
  }) })
}

export const SEEK_LABELS = {
  bar: 'Position in track',
  of: (pos, total) => `${pos} of ${total}`,
  unknown: 'Track length unknown; seeking is off',
  hint: 'Click or drag to jump · ←/→ move 5 seconds'
}

/** Click = jump; drag = scrub with ONE seek on release; ←/→ on the focused bar = ±5s. */
function SeekBar({ store }) {
  const p = useValue(store.player)
  const s = useValue(store.scrub)
  const item = p && p.item
  const d = knownDuration(item && item.duration_ms)
  const shown = s ? s.ms : clampMs((p && p.progress_ms) || 0, d)
  const pct = progressPct(shown, d)
  const disabled = !d
  const at = e => positionFromPointer(e.clientX, e.currentTarget.getBoundingClientRect(), d)

  return jsxs('div', { className: 'lfy-progress', dir: 'ltr', children: [
    jsx('span', { className: 'lfy-time', children: fmtTime(shown) }),
    jsxs('div', {
      className: 'lfy-seek',
      role: 'slider',
      tabIndex: disabled ? -1 : 0,
      dir: 'ltr',
      title: disabled ? SEEK_LABELS.unknown : SEEK_LABELS.hint,
      'aria-label': SEEK_LABELS.bar,
      'aria-orientation': 'horizontal',
      'aria-disabled': disabled,
      'aria-valuemin': 0,
      'aria-valuemax': Math.round(d / 1000),
      'aria-valuenow': Math.round(shown / 1000),
      'aria-valuetext': SEEK_LABELS.of(fmtTime(shown), fmtTime(d)),
      'data-scrubbing': !!s,
      onPointerDown: e => {
        if (disabled || (e.button !== undefined && e.button !== 0)) return
        if (!store.beginScrub(at(e))) return
        e.preventDefault()
        e.currentTarget.focus && e.currentTarget.focus()
        try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* capture is best effort */ }
      },
      onPointerMove: e => { if (store.scrub.get()) store.moveScrub(at(e)) },
      onPointerUp: e => {
        if (!store.scrub.get()) return
        try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* already released */ }
        void store.endScrub(at(e))
      },
      onPointerCancel: () => store.cancelScrub(),
      onKeyDown: e => {
        if (e.key === 'Escape' && store.scrub.get()) { e.preventDefault(); store.cancelScrub(); return }
        const delta = keySeekDelta(e)
        if (!delta || disabled) return
        e.preventDefault()
        e.stopPropagation()
        store.nudge(delta)
      },
      children: [
        jsx('div', { className: 'lfy-bar', children: jsx('span', { style: { width: pct + '%' } }) }),
        jsx('span', { className: 'lfy-thumb', style: { left: pct + '%' } })
      ]
    }),
    jsx('span', { className: 'lfy-time', children: fmtTime(d) })
  ] })
}

function NowView({ store }) {
  const p = useValue(store.player)
  const c = useValue(store.card)
  const draft = useValue(store.draft)
  const saved = useValue(store.saved)
  const busy = useValue(store.busy)
  const t = c.track
  const item = p && p.item
  const playing = !!(p && p.is_playing)
  const lateNote = saved && t && saved.track_uri === t.track_uri && draft.trim()
  const vol = p && p.device && typeof p.device.volume_percent === 'number' ? p.device.volume_percent : 50

  return jsxs('div', { children: [
    jsxs('div', { className: 'lfy-now', children: [
      jsx('div', { className: 'lfy-art', children: item && item.art ? jsx('img', { alt: '', src: item.art }) : '♪' }),
      jsxs('div', { className: 'lfy-meta', children: [
        jsx('div', { className: 'lfy-mode', 'data-mode': c.mode, children: MODE_LABEL[c.mode] || '' }),
        jsx('div', { className: 'lfy-title', dir: 'auto', title: t ? t.title : '', children: t ? t.title : (item && item.name) || 'Nothing is playing' }),
        jsx('div', { className: 'lfy-sub', dir: 'auto', children: t ? [t.artists.join(', '), t.album].filter(Boolean).join(' · ') : c.error || 'Open Spotify on one of your devices and press Play' }),
        jsx('div', { className: 'lfy-device', dir: 'auto', children: p && p.device ? `${playing ? '▶' : '⏸'} ${p.device.name}` : '' }),
        t && t.managed ? jsx('div', { className: 'lfy-managed', children: 'From a Loshify playlist: 👎 also removes it' }) : null
      ] })
    ] }),
    jsx(SeekBar, { store }),
    jsxs('div', { className: 'lfy-transport', children: [
      jsx(SmallAction, { icon: IcShuffle, label: 'Shuffle', on: !!(p && p.shuffle), onClick: () => store.toggleShuffle() }),
      jsx(SmallAction, { icon: IcPrev, label: 'Previous', onClick: () => store.transport('previous') }),
      jsx(SmallAction, { icon: playing ? icons.Pause : icons.Play, label: playing ? 'Pause' : 'Play', onClick: () => store.transport(playing ? 'pause' : 'play') }),
      jsx(SmallAction, { icon: IcNext, label: 'Next', onClick: () => store.transport('next') }),
      jsx(SmallAction, { icon: IcRepeat, label: `Repeat: ${(p && p.repeat) || 'off'}`, on: !!(p && p.repeat && p.repeat !== 'off'), onClick: () => store.cycleRepeat() })
    ] }),
    jsx('div', { className: 'lfy-reactions', children: REACTION_ORDER.map(r => jsx(EmojiButton, { reaction: r, store, big: true, disabled: !t || busy }, r)) }),
    jsx(Textarea, {
      className: 'lfy-note', dir: 'auto', value: draft, disabled: !t,
      placeholder: 'Optional note, saved with your reaction (the curator reads it)',
      onChange: e => store.draft.set(e.target.value),
      onKeyDown: e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && lateNote) { e.preventDefault(); void store.saveLateNote() } }
    }),
    lateNote
      ? jsx('div', { className: 'lfy-row', children: jsx(Button, { size: 'sm', disabled: busy, onClick: () => store.saveLateNote(), children: `Add note to ${REACTIONS[saved.reaction].emoji}` }) })
      : null,
    saved ? jsx('div', { className: 'lfy-saved', dir: 'auto', children: `Saved: ${REACTIONS[saved.reaction].emoji} · ${saved.title}` }) : null,
    jsxs('div', { className: 'lfy-volume', children: [
      jsx(icons.Volume2, { style: { width: 13, height: 13, flexShrink: 0 } }),
      jsx('input', { 'aria-label': 'Volume', max: 100, min: 0, type: 'range', value: vol, onChange: e => store.setVolume(Number(e.target.value)) })
    ] }),
    jsx(Devices, { store }),
    jsx('div', { className: 'lfy-keys', children: KEY_LEGEND.map(([icon, keys]) =>
      jsxs('span', { className: 'lfy-key', children: [jsx('span', { className: 'lfy-emoji', children: icon }), jsx(Kbd, { children: keys })] }, keys)) })
  ] })
}

function Devices({ store }) {
  const devices = useValue(store.devices)
  const p = useValue(store.player)
  const list = devices.length === 0 && p && p.device && p.device.id
    ? [{ active: true, id: p.device.id, name: p.device.name, type: p.device.type, volume: p.device.volume_percent }]
    : devices
  return jsxs('div', { className: 'lfy-section', children: [
    jsx('div', { className: 'lfy-heading', children: 'DEVICES' }),
    jsx('div', { className: 'lfy-list', children: list.length === 0
      ? jsx('div', { className: 'lfy-item', style: { cursor: 'default' }, children: 'No Spotify Connect devices found' })
      : list.map(d => jsx('button', { className: 'lfy-item', 'data-active': d.active, onClick: () => store.transfer(d.id),
          children: jsxs('span', { className: 'lfy-item-main', children: [
            jsx('span', { className: 'lfy-item-title', dir: 'auto', children: `${d.name} · ${d.type || ''}` }),
            jsx('span', { className: 'lfy-item-sub', children: d.active ? 'Active device' : `Volume ${d.volume ?? '?'}%` })
          ] }) }, d.id || d.name)) })
  ] })
}

function TrackRow({ track, store, queueable = true }) {
  return jsxs('div', { className: 'lfy-item', style: { cursor: 'default' }, children: [
    jsxs('span', { className: 'lfy-item-main', children: [
      jsx('span', { className: 'lfy-item-title', dir: 'auto', children: track.name }),
      jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: (track.artists || []).map(a => a.name).join(', ') })
    ] }),
    jsxs('span', { className: 'lfy-item-actions', children: [
      jsx(SmallAction, { icon: icons.Play, label: `Play: ${track.name}`, onClick: () => store.playPayload({ uri: track.uri, context_uri: track.album && track.album.uri }) }),
      queueable ? jsx(SmallAction, { icon: icons.Plus, label: `Add to queue: ${track.name}`, onClick: () => store.addToQueue(track.uri) }) : null
    ] })
  ] })
}

export function ArrangeBox({ store, rows = 2 }) {
  const text = useValue(store.arrange)
  return jsx(Textarea, {
    className: 'lfy-note lfy-arrange', dir: 'auto', value: text, rows, maxLength: ARRANGE_MAX,
    placeholder: ARRANGE_PLACEHOLDER, 'aria-label': 'Describe how you like your music arranged',
    onChange: e => store.arrange.set(e.target.value)
  })
}

/** "Your rotation": the standing arrangement, its playlists, Refresh now, or the presets + your own words. */
export function RotationSection({ store }) {
  const r = useValue(store.rotation)
  const text = useValue(store.arrange)
  const head = jsx('div', { className: 'lfy-heading', children: 'YOUR ROTATION' })
  if (r.state === 'loading') return jsxs('div', { className: 'lfy-section lfy-rotation', style: { marginTop: 0 }, children: [head, jsx('div', { className: 'lfy-heading', children: 'Loading…' })] })
  if (r.state === 'error') return jsxs('div', { className: 'lfy-section lfy-rotation', style: { marginTop: 0 }, children: [head, jsx('div', { className: 'lfy-heading', children: r.error })] })
  if (r.state === 'set') {
    return jsxs('div', { className: 'lfy-section lfy-rotation', style: { marginTop: 0 }, children: [
      head,
      jsx('div', { className: 'lfy-item-title', dir: 'auto', children: r.name }),
      jsx('div', { className: 'lfy-item-sub', dir: 'auto', children: r.sub }),
      jsx('div', { className: 'lfy-list', children: [
        ...r.slots.map(s => jsx('button', { className: 'lfy-item', disabled: !s.url, onClick: () => s.url && store.openExternal(s.url),
          children: jsxs('span', { className: 'lfy-item-main', children: [
            jsx('span', { className: 'lfy-item-title', dir: 'auto', children: '✦ ' + s.name }),
            jsx('span', { className: 'lfy-item-sub', children: s.url ? s.mode : `${s.mode} · not built yet` })
          ] }) }, s.name)),
        r.archiveUrl ? jsx('button', { className: 'lfy-item', onClick: () => store.openExternal(r.archiveUrl),
          children: jsx('span', { className: 'lfy-item-main', children: jsx('span', { className: 'lfy-item-title', children: '✦ Archive (earlier editions)' }) }) }, 'archive') : null
      ] }),
      jsx('div', { className: 'lfy-row', children: jsx(Button, { size: 'sm', onClick: () => store.refreshRotation(), children: r.due ? 'Refresh now (due)' : 'Refresh now' }) }),
      jsx(ArrangeBox, { store, rows: 1 }),
      text.trim() ? jsx('div', { className: 'lfy-row', children: jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.sendArrangement(), children: 'Rearrange: send to my curator' }) }) : null
    ] })
  }
  return jsxs('div', { className: 'lfy-section lfy-rotation', style: { marginTop: 0 }, children: [
    head,
    jsx('div', { className: 'lfy-step-text', children: 'How do you like your music organized? Pick an arrangement your curator keeps fresh, or say it in your own words.' }),
    jsx('div', { className: 'lfy-list', children: r.presets.map(p => jsx('button', { className: 'lfy-item', onClick: () => store.sendArrangement(p.name),
      children: jsxs('span', { className: 'lfy-item-main', children: [
        jsx('span', { className: 'lfy-item-title', dir: 'auto', children: p.name }),
        jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: `${p.cadence} · ${p.intent}` })
      ] }) }, p.id)) }),
    jsx(ArrangeBox, { store }),
    jsx('div', { className: 'lfy-row', children: jsx(Button, { size: 'sm', disabled: !text.trim(), onClick: () => store.sendArrangement(), children: 'Send to my curator' }) })
  ] })
}

/** One calibration track: title, why, and 👎 👌 ❤️ (no playback needed; works without Premium). */
function CalibrationRow({ track, store, busy }) {
  return jsxs('div', { className: 'lfy-item lfy-cal', style: { cursor: 'default' }, 'data-reaction': track.reaction || undefined, children: [
    jsxs('span', { className: 'lfy-item-main', children: [
      jsx('span', { className: 'lfy-item-title', dir: 'auto', children: `${track.n}. ${track.title} – ${track.artists}` }),
      jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: [track.kind, track.reason].filter(Boolean).join(' · ') })
    ] }),
    jsx('span', { className: 'lfy-cal-actions', children: REACTION_ORDER.map(r => jsx(Tip, { label: REACTIONS[r].tip, children: jsx(Button, {
      'aria-label': `${REACTIONS[r].tip}: ${track.title}`, 'aria-pressed': track.reaction === r, 'data-on': track.reaction === r,
      className: 'lfy-cal-btn', disabled: busy, size: 'micro', variant: 'ghost', onClick: () => { void store.reactCalibration(track.n, r) },
      children: jsx('span', { className: 'lfy-emoji', children: REACTIONS[r].emoji })
    }) }, r)) })
  ] })
}

/** "Get to know me" (no chat): portrait → 5 picks → arrangement + note → playlists. */
export function GetToKnowMe({ store }) {
  const f = useValue(store.flow)
  const o = useValue(store.onboarding)
  const r = useValue(store.rotation)
  const busy = useValue(store.busy)
  const anchors = useValue(store.anchorsDraft)
  const preset = useValue(store.flowPreset)
  const words = useValue(store.arrange)
  const noteDraftText = useValue(store.noteDraft)
  const note = noteDraftText === null ? o.note || '' : noteDraftText
  const working = f.working ? jsx('div', { className: 'lfy-step-text', role: 'status', 'aria-live': 'polite',
    children: f.progress || FLOW_WORKING[f.working] || 'Working…' }) : null
  // A failure is one friendly line and Try again; never a raw error string.
  const error = f.error && !f.working ? jsxs('div', { className: 'lfy-setup', children: [
    Problem(f.error),
    f.retry ? Row(jsx(Button, { size: 'sm', onClick: () => store.flowRetry(), children: 'Try again' })) : null
  ] }) : null
  const chatLink = jsx(Button, { size: 'sm', variant: 'ghost', disabled: !!f.working, onClick: () => store.meet(), children: 'Do it in the chat instead' })

  if (f.stage === 'idle') {
    return jsxs('div', { className: 'lfy-setup lfy-flow', 'data-stage': 'idle', children: [
      Text('Your curator reads your listening history, describes your taste, shows five quick picks to react to, then builds your first playlists. All clicks; every question can be skipped.'),
      working, error,
      Row([jsx(Button, { size: 'sm', disabled: !!f.working, onClick: () => store.flowStart(), children: 'Get to know me' }, 'go'), chatLink])
    ] })
  }
  if (f.stage === 'portrait') {
    return jsxs('div', { className: 'lfy-setup lfy-flow', 'data-stage': 'portrait', children: [
      jsx('div', { className: 'lfy-heading', children: f.revised ? 'YOUR PORTRAIT, REVISED' : 'YOUR PORTRAIT' }),
      jsx('div', { className: 'lfy-portrait', dir: 'auto', children: f.portrait }),
      f.unresolved && f.unresolved.length ? Text(`Couldn't find: ${f.unresolved.join(', ')}`) : null,
      working, error,
      f.revised
        ? Row(jsx(Button, { size: 'sm', onClick: () => store.flowGo(f.calibrationReady ? 'calibration' : 'arrange'), children: 'Continue' }))
        : jsxs('div', { className: 'lfy-setup', children: [
            Row([
              jsx(Button, { size: 'sm', disabled: !!f.working, onClick: () => store.flowVerdict('sounds_like_me'), children: 'Sounds like me' }, 'yes'),
              jsx(Button, { size: 'sm', variant: 'ghost', disabled: !!f.working, onClick: () => store.flowVerdict('not_quite'), children: 'Not quite' }, 'no')
            ]),
            jsx(Textarea, { className: 'lfy-note', dir: 'auto', rows: 2, value: anchors, placeholder: ANCHORS_PLACEHOLDER,
              'aria-label': 'Artists or songs that are really you (optional)', onChange: e => store.anchorsDraft.set(e.target.value) }),
            jsx('div', { className: 'lfy-item-sub', children: 'Optional: fill this in before “Not quite” and the portrait is rewritten around it.' })
          ] })
    ] })
  }
  if (f.stage === 'calibration') {
    return jsxs('div', { className: 'lfy-setup lfy-flow', 'data-stage': 'calibration', children: [
      jsx('div', { className: 'lfy-heading', children: `QUICK CALIBRATION · ${o.reacted || 0}/${o.total || 0}` }),
      Text('React to each; you don’t need to play them. 👎 here only teaches: it never skips or removes anything.'),
      jsx('div', { className: 'lfy-list', children: (o.tracks || []).map(t => jsx(CalibrationRow, { track: t, store, busy }, t.uri)) }),
      Row(jsx(Button, { size: 'sm', disabled: busy, onClick: () => store.flowGo('arrange'), children: o.pending ? 'Skip the rest' : 'Next' }))
    ] })
  }
  if (f.stage === 'arrange') {
    return jsxs('div', { className: 'lfy-setup lfy-flow', 'data-stage': 'arrange', children: [
      jsx('div', { className: 'lfy-heading', children: 'HOW SHOULD YOUR MUSIC BE ARRANGED?' }),
      jsx('div', { className: 'lfy-list', children: (r.presets || []).map(p => jsx('button', {
        className: 'lfy-item', 'data-on': preset === p.id, 'aria-pressed': preset === p.id, disabled: !!f.working,
        onClick: () => { store.flowPreset.set(preset === p.id ? null : p.id); store.arrange.set('') },
        children: jsxs('span', { className: 'lfy-item-main', children: [
          jsx('span', { className: 'lfy-item-title', dir: 'auto', children: p.name }),
          jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: `${p.cadence} · ${p.intent}` })
        ] }) }, p.id)) }),
      jsx(Textarea, { className: 'lfy-note lfy-arrange', dir: 'auto', rows: 2, maxLength: ARRANGE_MAX, value: words,
        placeholder: 'Or in your own words: ' + ARRANGE_PLACEHOLDER, 'aria-label': 'Describe your own arrangement',
        onChange: e => { store.arrange.set(e.target.value); if (e.target.value.trim()) store.flowPreset.set(null) } }),
      jsx('div', { className: 'lfy-heading', children: 'ANYTHING ELSE? (OPTIONAL)' }),
      jsx(Textarea, { className: 'lfy-note', dir: 'auto', rows: 2, maxLength: NOTE_MAX, value: note, placeholder: NOTE_PLACEHOLDER,
        'aria-label': 'Anything else your curator should take into account', onChange: e => store.noteDraft.set(e.target.value) }),
      working, error,
      Row(jsx(Button, { size: 'sm', disabled: busy || !!f.working || (!preset && !words.trim()), onClick: () => store.flowBuild(), children: 'Build my playlists' }))
    ] })
  }
  return jsxs('div', { className: 'lfy-setup lfy-flow', 'data-stage': 'done', children: [
    jsx('div', { className: 'lfy-heading', children: 'YOUR PLAYLISTS ARE READY' }),
    jsx('div', { className: 'lfy-list', children: (f.links || []).map(l => jsx('button', { className: 'lfy-item', 'data-playlist': l.slot,
      onClick: () => store.openExternal(l.url),
      children: jsxs('span', { className: 'lfy-item-main', children: [
        jsx('span', { className: 'lfy-item-title', dir: 'auto', children: '✦ ' + (l.name || l.slot) }),
        jsx('span', { className: 'lfy-item-sub', children: `${l.tracks} tracks · open in Spotify` })
      ] }) }, l.slot)) }),
    f.short && f.short.length ? Text(`Still filling: ${f.short.join(', ')}. “Refresh now” in Playlists tops them up.`) : null,
    Row(jsx(Button, { size: 'sm', onClick: () => (f.rerun ? store.closeRerun() : store.dismissMeet()), children: 'Done' }))
  ] })
}

/** Tune in: a small, optional strip on the card. Never shown before the first run; never a popup. */
export function TuneInStrip({ store }) {
  const t = useValue(store.tuneIn)
  const busy = useValue(store.busy)
  const job = useValue(store.tuneJob)
  if (!t.show) return null
  if (t.round) {
    return jsxs('div', { className: 'lfy-section lfy-tunein', children: [
      jsx('div', { className: 'lfy-heading', children: `TUNE IN · ${t.reacted}/${t.round.total}` }),
      jsx('div', { className: 'lfy-list', children: t.round.tracks.map(tr => jsxs('div', { className: 'lfy-item lfy-cal', style: { cursor: 'default' }, children: [
        jsxs('span', { className: 'lfy-item-main', children: [
          jsx('span', { className: 'lfy-item-title', dir: 'auto', children: `${tr.n}. ${tr.title} – ${tr.artists}` }),
          jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: tr.reason || '' })
        ] }),
        jsx('span', { className: 'lfy-cal-actions', children: REACTION_ORDER.map(r => jsx(Button, {
          'aria-label': `${REACTIONS[r].tip}: ${tr.title}`, 'aria-pressed': tr.reaction === r, 'data-on': tr.reaction === r,
          className: 'lfy-cal-btn', disabled: busy, size: 'micro', variant: 'ghost', onClick: () => { void store.reactTuneIn(tr.n, r) },
          children: jsx('span', { className: 'lfy-emoji', children: REACTIONS[r].emoji }) }, r)) })
      ] }, tr.uri)) }),
      Row(jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.finishTuneIn(), children: 'Done' }))
    ] })
  }
  return jsxs('div', { className: 'lfy-tunein-badge', children: [
    job.working ? Text(job.progress) : null,
    job.error ? Problem(job.error) : null,
    job.error ? jsx(Button, { size: 'micro', disabled: busy, onClick: () => store.startTuneIn(), children: 'Try again' }) : null,
    t.available && !job.error ? jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy, onClick: () => store.startTuneIn(), children: '✦ Tune in · 5 quick picks' }) : null,
    t.available ? null : jsx('span', { className: 'lfy-item-sub', children: t.line })
  ] })
}

/** demo_mode: the last Archive refresh counts and the taste line, under the Tune-in strip. */
export function DemoStrip({ store }) {
  const d = useValue(store.demo)
  if (!d || !d.show) return null
  return jsx('div', { className: 'lfy-demo', 'data-demo': 'on', children: d.lines.map(line =>
    jsx('div', { className: 'lfy-item-sub', dir: 'auto', children: line }, line)) })
}

/** The open note: one line the curator keeps in mind (editable any time). */
export function NoteBox({ store }) {
  const o = useValue(store.onboarding)
  const draftText = useValue(store.noteDraft)
  const text = draftText === null ? o.note || '' : draftText
  const problem = noteError(text)
  const changed = draftText !== null && draftText.trim() !== (o.note || '')
  return jsxs('div', { className: 'lfy-section', children: [
    jsx('div', { className: 'lfy-heading', children: 'ANYTHING ELSE?' }),
    jsx(Textarea, {
      className: 'lfy-note', dir: 'auto', value: text, rows: 2, maxLength: NOTE_MAX, placeholder: NOTE_PLACEHOLDER,
      'aria-label': 'Anything else your curator should take into account', onChange: e => store.noteDraft.set(e.target.value)
    }),
    jsx('div', { className: 'lfy-item-sub', children: problem || `${text.trim().length}/${NOTE_MAX} · taste only; your curator reads it as a preference` }),
    jsxs('div', { className: 'lfy-row', children: [
      jsx(Button, { size: 'sm', disabled: !!problem || !changed, onClick: () => store.saveNote(), children: 'Save note' }),
      o.active && o.nextStep === 'note' ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.skipStep('note'), children: 'Skip' }) : null
    ] })
  ] })
}

/** Taste tab: the calibration round (react 👎 👌 ❤️), your anchors, and your note. */
export function TasteView({ store }) {
  const o = useValue(store.onboarding)
  const busy = useValue(store.busy)
  const f = useValue(store.flow)
  // "Start over" opens the click-through flow here, in the Taste tab, until its Done.
  if (f.rerun) {
    return jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
      jsx('div', { className: 'lfy-heading', children: 'GET TO KNOW ME, AGAIN' }),
      jsx(GetToKnowMe, { store }),
      f.stage === 'idle' && !f.working ? Row(jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.closeRerun(), children: 'Cancel' })) : null
    ] })
  }
  if (o.state === 'loading') return jsx('div', { className: 'lfy-heading', children: 'Loading…' })
  if (o.state === 'error') return jsx('div', { className: 'lfy-heading', children: o.error })
  return jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
    o.tracks.length ? jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
      jsx('div', { className: 'lfy-heading', children: `QUICK CALIBRATION · ${o.reacted}/${o.total}` }),
      jsx('div', { className: 'lfy-step-text', children: 'Five picks from your curator. React to each; you don’t need to play them.' }),
      jsx('div', { className: 'lfy-list', children: o.tracks.map(t => jsx(CalibrationRow, { track: t, store, busy }, t.uri)) }),
      o.pending && o.active ? jsx('div', { className: 'lfy-row', children: jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.skipStep('calibration'), children: 'Done for now' }) }) : null
    ] }) : jsx('div', { className: 'lfy-step-text', children: 'No calibration round yet. Press Start over below, or ask your curator: “Get to know my music.”' }),
    o.anchors.length ? jsxs('div', { className: 'lfy-section', children: [
      jsx('div', { className: 'lfy-heading', children: 'YOUR ANCHORS' }),
      jsx('div', { className: 'lfy-item-sub', dir: 'auto', children: o.anchors.join(' · ') })
    ] }) : null,
    jsx(NoteBox, { store }),
    jsxs('div', { className: 'lfy-section', children: [
      jsx('div', { className: 'lfy-heading', children: 'START OVER' }),
      jsx('div', { className: 'lfy-item-sub', children: START_OVER_TEXT }),
      Row(jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: () => store.startOver(), children: 'Re-run “Get to know me”' }))
    ] })
  ] })
}

function PlaylistsView({ store }) {
  const pl = useValue(store.playlists)
  return jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
    jsx(RotationSection, { store }),
    jsx('div', { className: 'lfy-heading', children: 'YOUR PLAYLISTS' }),
    jsx('div', { className: 'lfy-list', children: pl.items.map(item => {
      const n = playlistCount(item)
      return jsx('button', { className: 'lfy-item', onClick: () => store.playPayload({ context_uri: item.uri }),
        children: jsxs('span', { className: 'lfy-item-main', children: [
          jsx('span', { className: 'lfy-item-title', dir: 'auto', children: (item.loshify ? '✦ ' : '') + item.name }),
          jsx('span', { className: 'lfy-item-sub', dir: 'auto', children: `${n === null ? '?' : n} tracks · ${item.loshify ? 'made by Loshify' : (item.owner && item.owner.display_name) || ''}` })
        ] }) }, item.id)
    }) }),
    pl.loading ? jsx('div', { className: 'lfy-heading', children: 'Loading…' }) : null,
    pl.error ? jsx('div', { className: 'lfy-heading', children: pl.error }) : null
  ] })
}

function SearchView({ store }) {
  const text = useValue(store.searchText)
  const r = useValue(store.results)
  return jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
    jsx('div', { style: { paddingBottom: 6 }, children: jsx(SearchField, { onChange: v => store.setSearch(typeof v === 'string' ? v : (v && v.target ? v.target.value : '')), placeholder: 'Track, artist, album…', value: text }) }),
    jsx('div', { className: 'lfy-list', children: r.items.map(t => jsx(TrackRow, { track: t, store }, t.id || t.uri)) }),
    r.loading ? jsx('div', { className: 'lfy-heading', children: 'Searching…' }) : null,
    r.error ? jsx('div', { className: 'lfy-heading', children: r.error }) : null,
    !r.loading && r.query.length >= 2 && r.items.length === 0 && !r.error ? jsx('div', { className: 'lfy-heading', children: 'No tracks found' }) : null
  ] })
}

function QueueView({ store }) {
  const q = useValue(store.queue)
  return jsxs('div', { className: 'lfy-section', style: { marginTop: 0 }, children: [
    q.current ? jsx('div', { className: 'lfy-heading', dir: 'auto', children: `Now playing: ${q.current.name}` }) : null,
    jsx('div', { className: 'lfy-heading', children: 'UP NEXT' }),
    jsx('div', { className: 'lfy-list', children: q.items.length === 0 && !q.loading
      ? jsx('div', { className: 'lfy-item', style: { cursor: 'default' }, children: 'The queue is empty' })
      : q.items.map((t, i) => jsx(TrackRow, { track: t, store, queueable: false }, (t.id || t.uri) + '-' + i)) }),
    q.loading ? jsx('div', { className: 'lfy-heading', children: 'Loading…' }) : null,
    q.error ? jsx('div', { className: 'lfy-heading', children: q.error }) : null
  ] })
}

function CopyRow({ store, label, value }) {
  return jsxs('div', { className: 'lfy-copy', children: [
    jsx('span', { className: 'lfy-copy-label', children: label }),
    jsx('code', { title: value, children: value }),
    jsx(SmallAction, { icon: icons.Copy || icons.Clipboard || icons.Plus, label: `Copy ${label}`, onClick: () => store.copy(value, label) })
  ] })
}

function Step({ id, num, title, state, children }) {
  const open = state === 'current' || state === 'error'
  return jsxs('div', { className: 'lfy-step', 'data-step': id, 'data-state': state, 'aria-current': open ? 'step' : undefined, children: [
    jsxs('div', { className: 'lfy-step-head', children: [
      jsx('span', { className: 'lfy-step-badge', children: state === 'done' || state === 'warn' ? '✓' : state === 'error' ? '!' : num }),
      jsx('span', { className: 'lfy-step-title', children: title }),
      jsx('span', { className: 'lfy-step-status', children: STATUS_LABEL[state] || '' })
    ] }),
    open ? children : null
  ] })
}

const Text = (children, extra = {}) => jsx('div', { className: 'lfy-step-text', ...extra, children })
const Why = children => jsx('div', { className: 'lfy-step-why', children })
const Problem = children => jsx('div', { className: 'lfy-step-problem', role: 'alert', dir: 'auto', children })
const Row = children => jsx('div', { className: 'lfy-row', children })
const DoneButton = (store, id, label = 'Done') => jsx(Button, { size: 'sm', onClick: () => store.markStep(id, true), children: label })

/** First-run card: a guided wizard, one step open at a time (setupView). Re-polls /setup every 2 s while open. */
export function SetupCard({ store }) {
  const s = useValue(store.setup)
  const dismissed = useValue(store.meetDismissed)
  const marks = useValue(store.setupMarks)
  const draft = useValue(store.clientIdDraft)
  const idState = useValue(store.clientIdState)
  const checking = useValue(store.checking)
  const trouble = useValue(store.trouble)
  const flowStage = useValue(store.flow).stage
  const v = setupView(s, dismissed, marks)
  const val = { ...SETUP_DEFAULTS, ...(s || {}) }
  const titles = Object.fromEntries(SETUP_STEPS.map(([id, num, title]) => [id, [num, title]]))
  // Step has no hooks: called directly so closed steps never even build their contents.
  const step = (id, children) => Step({ id, num: titles[id][0], title: titles[id][1], state: v.status[id], children })
  const back = id => jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.markStep(id, false), children: 'Back' })

  return jsxs('div', { className: 'lfy-setup', children: [
    jsx('div', { className: 'lfy-setup-intro', children: v.ready
      ? 'Spotify is connected. One last step.'
      : `Connect Loshify to Spotify with your own free Spotify app. About two minutes. ${v.doneCount} of ${v.total} done.` }),
    v.step === 'loading' ? Text('Checking…') : null,
    v.step === 'offline' && v.problem ? Problem(v.problem) : null,
    step('dashboard', jsxs('div', { className: 'lfy-setup', children: [
      Text('Log in with your Spotify account, then press Create app.'),
      Row([
        jsx(Button, { size: 'sm', onClick: () => store.openExternal(val.dashboard_url), children: 'Open Spotify Dashboard' }, 'open'),
        jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.markStep('dashboard', true), children: "I'm on the Create app form" }, 'done')
      ])
    ] })),
    step('app', jsxs('div', { className: 'lfy-setup', children: [
      Text('Copy each value into the form:'),
      jsx(CopyRow, { store, label: 'App name', value: val.app_name }),
      jsx(CopyRow, { store, label: 'Redirect URI', value: val.redirect_uri }),
      Text('Paste the Redirect URI, press Add, then tick only “Web API”. Accept the terms and Save.'),
      Why(val.why),
      Row([DoneButton(store, 'app', 'Saved'), back('dashboard')])
    ] })),
    step('users', jsxs('div', { className: 'lfy-setup', children: [
      Text('In your new app: Settings → User Management. Add your name and the email of your Spotify account, then Save.'),
      Why(val.users_why),
      Row([DoneButton(store, 'users', 'Added'), back('app')])
    ] })),
    step('client_id', jsxs('div', { className: 'lfy-setup', children: [
      Text('Copy the Client ID from Settings → Basic Information (not the Client secret) and paste it here. Loshify saves it in its settings for you.'),
      jsx(Input, {
        className: 'lfy-client-id', value: draft, placeholder: 'Client ID (32 letters and digits)', 'aria-label': 'Spotify Client ID',
        autoComplete: 'off', spellCheck: false, disabled: idState.busy,
        onChange: e => store.clientIdDraft.set(e.target.value),
        onKeyDown: e => { if (e.key === 'Enter') void store.saveClientId() }
      }),
      idState.error ? Problem(idState.error) : null,
      Row([
        jsx(Button, { size: 'sm', disabled: idState.busy, onClick: () => store.saveClientId(), children: idState.busy ? 'Saving…' : 'Save' }, 'save'),
        back('users')
      ])
    ] })),
    step('connect', jsxs('div', { className: 'lfy-setup', children: [
      Text('Copy this command and run it once in a terminal (Desktop’s terminal pane works). Your browser opens: log in and press Agree. Hermes keeps the sign-in; no agent or plugin can run this step for you.'),
      jsx(CopyRow, { store, label: 'Command', value: val.signin_command }),
      Why(val.connect_where || CONNECT_WHERE),
      Text('This card turns green by itself a few seconds after you press Agree.'),
      Text('Something went wrong?'),
      jsx('div', { className: 'lfy-trouble', children: SIGNIN_TROUBLE.map(([id, label]) =>
        jsx('button', { className: 'lfy-item', 'data-on': trouble === id, onClick: () => store.trouble.set(trouble === id ? null : id), children: label }, id)) }),
      trouble ? Problem(v.fixes[trouble]) : null
    ] })),
    step('check', jsxs('div', { className: 'lfy-setup', children: [
      v.problem ? Problem(v.problem) : null,
      Row(jsx(Button, { size: 'sm', disabled: checking, onClick: () => store.checkNow(), children: checking ? 'Checking…' : 'Check again' }))
    ] })),
    step('meet', jsxs('div', { className: 'lfy-setup', children: [
      v.problemKind === 'no_premium' ? Problem(v.problem) : null,
      v.note ? jsx('div', { className: 'lfy-step-text', children: v.note }) : null,
      v.ready ? jsx(GetToKnowMe, { store }) : Text('Your curator gets to know your music right after Spotify is connected.'),
      v.ready && flowStage === 'idle' ? Row(jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => store.dismissMeet(), children: 'Later' })) : null
    ] })),
    // After the build the first run counts as finished (meet = done, so its step closes): keep the links on screen.
    v.ready && flowStage === 'done' ? jsx(GetToKnowMe, { store }, 'flow-done') : null
  ] })
}

function Panel({ store }) {
  const view = useValue(store.view)
  const status = useValue(store.status)
  const s = useValue(store.setup)
  const dismissed = useValue(store.meetDismissed)
  const f = useValue(store.flow)
  // The flow keeps the card until the user presses Done on the links (the build marks onboarding finished).
  // A re-run from the Taste tab ("Start over") stays in the Taste tab instead.
  const setupOn = setupView(s, dismissed).show || (f.stage === 'done' && !dismissed && !f.rerun)
  return jsxs('div', { className: 'lfy-panel', children: [
    setupOn ? null : jsx(TuneInStrip, { store }),
    setupOn ? null : jsx(DemoStrip, { store }),
    setupOn ? null : jsx('div', { className: 'lfy-tabs', role: 'tablist', children: TABS.map(([id, label]) =>
      jsx('button', { className: 'lfy-tab', role: 'tab', 'data-active': view === id, 'aria-selected': view === id, onClick: () => store.selectView(id), children: label }, id)) }),
    jsxs('div', { className: 'lfy-body', children: setupOn
      ? [jsx(SetupCard, { store }, 'setup')]
      : [
          view === 'now' ? jsx(NowView, { store }, 'now') : null,
          view === 'taste' ? jsx(TasteView, { store }, 'taste') : null,
          view === 'playlists' ? jsx(PlaylistsView, { store }, 'playlists') : null,
          view === 'search' ? jsx(SearchView, { store }, 'search') : null,
          view === 'queue' ? jsx(QueueView, { store }, 'queue') : null
        ] }),
    jsx('div', { className: 'lfy-status', role: 'status', dir: 'auto', children: status || (setupOn ? 'Loshify setup' : 'Remote control: sound plays in your Spotify app') })
  ] })
}

function Chip({ store }) {
  const c = useValue(store.card)
  const p = useValue(store.player)
  const open = useValue(store.open)
  const busy = useValue(store.busy)
  const s = useValue(store.setup)
  const ready = !!(s && s.ready)
  const t = ready ? c.track : null
  const playing = !!(p && p.is_playing)
  const prefix = c.mode === 'last' ? '(last) ' : ''
  const label = !ready ? (s ? 'Loshify · set up' : 'Loshify')
    : t ? `${prefix}${t.title} — ${t.artists.join(', ')}` : (p && p.item && p.item.name) || 'Loshify'
  const disabled = !t || busy

  return jsxs('div', { className: 'lfy-chip', children: [
    jsxs(Popover, { open, onOpenChange: v => store.setOpen(v), children: [
      jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
        'aria-label': `Loshify: ${label}`, size: 'micro', variant: 'ghost', style: { maxWidth: 200 },
        children: jsx('span', { className: 'lfy-label', 'data-mode': ready ? c.mode : 'setup', dir: 'auto', title: label, children: label })
      }) }),
      jsx(PopoverContent, { className: POP_CLASS, side: 'top', align: 'end', 'aria-label': 'Loshify', children: jsx(Panel, { store }) })
    ] }),
    ready ? jsx(SmallAction, { icon: playing ? icons.Pause : icons.Play, label: playing ? 'Pause' : 'Play', onClick: () => store.transport(playing ? 'pause' : 'play') }) : null,
    ready ? jsx(SmallAction, { icon: IcNext, label: 'Next', onClick: () => store.transport('next') }) : null,
    ...(ready ? REACTION_ORDER.map(r => jsx(EmojiButton, { reaction: r, store, disabled }, r)) : [])
  ] })
}

export default {
  id: ID,
  name: 'Loshify',
  description: 'Spotify remote with one-tap 👎 👌 ❤️ reactions that teach your Loshify curator. 👎 skips and removes the track only from playlists Loshify made, with Undo. Includes the first-run setup card.',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())

    const store = createStore(ctx)
    void store.start()
    ctx.setInterval(() => store.tick(), TICK_MS)

    ctx.register({ area: STATUSBAR_AREAS.right, id: 'chip', order: 12, render: () => jsx(Chip, { store }) })

    const keybind = (id, label, defaults, run) =>
      ctx.register({ area: KEYBINDS_AREA, id, data: { id: `${ID}.${id}`, category: 'Loshify', defaults, label, run } })
    keybind('love', 'Loshify: ❤️ love it', ['mod+alt+up'], () => { void store.react('love') })
    keybind('okay', 'Loshify: 👌 works for me', ['mod+alt+right'], () => { void store.react('okay') })
    keybind('down', 'Loshify: 👎 never again (skip; remove from Loshify playlists)', ['mod+alt+down'], () => { void store.react('down') })
    keybind('open', 'Loshify: open player', ['mod+alt+enter'], () => { store.setOpen(true) })
  }
}
