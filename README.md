# Loshify

<!-- demo: add video link and screenshot before submission -->

**A Spotify curator that knows you.** Loshify learns your taste from your listening history and from
one-tap reactions, builds playlists with a reason for every track, and remembers what you loved and
what you never want to hear again. It is a Hermes Agent plugin with an agent half (seven tools, a
taste brief, a skill) and a Hermes Desktop half (a status-bar Spotify remote with 👎 👌 ❤️ and a
first-run setup card).

> **Before you install: what you need**
> - **Your own Spotify developer app.** Spotify limits development-mode apps to 5 users, so Loshify
>   cannot ship a shared app. Creating yours takes about a minute; the setup card walks you through it.
> - **Spotify Premium** for the app owner. Spotify requires it for development-mode apps, and playback
>   control (play, skip, seek) needs Premium. Curating playlists works on any account that can sign in.
> - **Hermes Agent ≥ 0.23.0** (plugin `oauth:` sign-in). Hermes Desktop for the remote and the setup card.

## What it does

- **Get to know me (Desktop: all clicks, no chat).** After Connect, the setup card walks through
  the first run as a short stepper: portrait → 5 quick picks → arrangement + note → *Build my
  playlists* → three links. The words (portrait, picks, playlist candidates) come from your own
  Hermes main model through Hermes's plugin LLM facade (`ctx.llm` / `agent.plugin_llm.PluginLlm`),
  called from Loshify's Desktop backend; no chat window is involved. If that model is unavailable,
  every step still finishes from your own data (a plain portrait, familiar picks, your own library
  for *Then*); only discovery slots wait for the curator. *Do it in the chat instead* runs the same
  flow through the agent with the same tools. The long steps run in the background on Loshify's
  backend, and the card shows each stage ("Writing your playlists… 2/3"). A slow model or a dropped
  connection never turns into an error: the card keeps checking until the step is done. Only a step
  that really failed shows one plain line with *Try again*. *Taste → Start over* runs the whole flow
  again later; your reactions, anchors and note are kept.
- **A portrait that fits anyone.** Loshify computes the portrait facts; the writer only phrases
  them. Real listening (top artists/tracks for 4 weeks, 6 months, all time; recent plays) outweighs
  saves. Saves count per period with a cap, and a burst (a year with more than 3× the usual saves,
  a one-month library import, one album binged) is called *a phase*, never who you are. Lately leads;
  all-time and old saves are "where you came from". Every claim carries a confidence and is hedged
  when the evidence is thin. *Not quite* (with up to 3 optional anchors) rewrites it, and your
  calibration reactions feed every later portrait.
- **Meet your curator (chat).** In chat the agent reads the same facts, writes the portrait and
  makes a first playlist. In between come a few light steps, and you can skip any of them:
  1. **Anchors: "what did I miss?"** You name up to 3 artists or songs that are really you. Loshify
     finds them on Spotify and counts each as a ❤️, which fades like any ❤️. The portrait is then
     refreshed to mention them.
  2. **Quick calibration.** The curator proposes 5 picks: 2 familiar, 2 near, and 1 stretch, plus 5
     backups. Each goes through the same checks as any curated track, and no playlist is written.
     You react 👎 👌 ❤️ in the Desktop stepper, the *Taste* tab, or in chat. These are normal
     reactions, so the curator starts learning in minute one; a calibration 👎 only teaches (it never
     skips or removes anything). Playing the tracks is optional, so it works without Premium.
  3. **How do you like your music organized?** This sets up your rotation (below).
  4. **"Anything else I should take into account?"** One short line, such as a genre, a vibe inside
     a genre, or things to avoid (≤ 300 characters). It is kept as your note, never fades, and you
     can edit it any time in the *Taste* tab or by telling your curator. It goes with the taste
     brief and every playlist request, quoted as your words about taste, never as instructions.

  Then the first edition. At most that is 8 answers from you (1 anchors line, 5 taps, 1 pick,
  1 note line). Skip everything and it is 0.
- **Tune in (optional, recurring).** After the first run, a 5-pick round opens again every 7–14 days
  (`tune_in_days`, default 10). It is a small *Tune in* button on the Loshify card, never a popup;
  otherwise the card just says "Next tune-in available in N days". Its picks aim at what the
  curator is least sure about: new lanes, the edges of your taste, areas you 👎'd lately. Reactions
  are normal reactions, so the next rotation refresh already uses them.
- **Your music, arranged your way (rotations).** Right after your taste portrait the curator asks
  one question: *how do you like your music organized?* Pick an arrangement or describe your own
  ("one for cooking, one for night drives, something new every week"). The curator turns it into a
  *rotation*: up to six Loshify playlists, each with a one-line intent, refreshed daily, weekly or
  when you ask. With the archive policy on, the previous edition's tracks move into one *Archive*
  playlist instead of disappearing. The Archive keeps what earned its place and lets the rest go:

  - ❤️ tracks (three 👌 on one track count as a ❤️) are *keepers* and stay for good.
  - Tracks that finished an edition with 👌 or no reaction enter on *probation* and leave after
    `archive_probation_days` (90), unless you ❤️ them before then.
  - 👎 tracks never enter, and are taken out if you 👎 them later.
  - Above `archive_cap` (200 tracks) the oldest non-keepers leave first. Keepers are never evicted;
    if keepers alone pass the cap, all of them stay and the curator tells you.

  This happens only when the rotation refreshes (no background job), and only to tracks Loshify put
  in the Archive itself; a track you added there by hand is never removed. After a refresh the
  curator sums it up, e.g. *"Archive: 14 added, 9 aged out, 3 keepers"*. Built-in arrangements:

  | Arrangement | Slots | Cadence | Old editions |
  |---|---|---|---|
  | Rhythmic / Quiet / Mixed | by energy | weekly | Archive |
  | Morning / Focus / Night | by time of day | weekly | Archive |
  | Comfort / Edge / Wild | by how far from home (familiar → new) | weekly | Archive |
  | Then / Now / Next | by era | when you ask | replaced |

  You always see the arrangement before it is saved (in the stepper, picking the card *is* seeing
  it). A slot that comes back short is topped up by a retry within a few hours: the tracks already
  there stay, with their reasons, and only the gap is filled. *Next*-style discovery slots skip
  artists you already play without spending searches on them. The *Playlists* tab keeps a
  "Describe how you like your music arranged" box and a *Refresh now* button that hand the request
  to your curator in the open conversation.
- **Playlists with reasons.** `loshify_curate` builds *Time Capsule* (your forgotten favourites) and
  *Deep Dig* (new music next to what you love). Every track comes with a one-line reason. Loshify
  writes only to playlists it created itself.
- **Reactions that stick.** 👎 never again · 👌 works for me · ❤️ love. React from the Desktop
  status bar (or `Ctrl+Alt+↑ / → / ↓`) or just tell the agent. A 👎 skips the track and, **only if
  it is playing from a Loshify playlist**, removes it from that playlist. Undo puts it back in the same
  position. Your own playlists are never edited.
- **Three 👌 make a ❤️, whenever they happen.** Three 👌 on the same track count as a ❤️, with
  everything a ❤️ does (the loved list, an Archive keeper, a ❤️'s weight in your taste). They don't
  have to be close together: a song that comes round in your rotations every few weeks gets there
  too. The per-track count is kept for good (it does not fade) and a 👎 starts it over, so 👌, 👌,
  then 👎 is a 👎. The thinking behind it: 👎 and ❤️ are deliberate, so you're careful with them;
  👌 depends on the mood. If you keep giving the same track a 👌 you don't remember giving, you
  like it more than you think.
- **Taste brief.** When you talk about music, the agent gets a short (≤ 700 character) summary of
  your taste, including your anchors, so suggestions in any chat fit you. If you left a note, it
  follows the summary in quotes. Turn it off with the *Taste brief* setting.
- **Remote.** Now playing, seek, transport, volume, devices, your playlists, search and queue, in the
  Desktop status bar. Sound keeps playing in your own Spotify apps; Loshify drives them.

Tools: `loshify_setup`, `loshify_taste`, `loshify_reactions`, `loshify_curate`, `loshify_react`,
`loshify_rotation`, `loshify_onboard` (verdict, anchors, calibration, note, skip, tune-in).
Hook: `pre_llm_call` (the taste brief). Bundled skill: `skills/loshify-curator`.

## Quick start (about two minutes)

1. Install and enable the agent plugin: `hermes plugins install iHeavyShoes/loshify --enable`.
   Once listed in the catalog, install by name with `hermes plugins install loshify`.
2. In Hermes Desktop, open **Capabilities → Plugins**, find *Loshify* and turn on its **Desktop** switch.
   If the row is missing, press **Rescan**.

Then open the Loshify chip in the Desktop status bar. The setup card walks you through these steps
one at a time, with copy buttons, and turns green on its own. No Desktop? Ask the agent to "set up
Loshify" and it walks you through the same steps in chat.

1. **Open the Spotify Dashboard:** <https://developer.spotify.com/dashboard>. Log in, press *Create app*.
2. **Create the app.** App name `Loshify`. Redirect URI, exactly:

   ```
   http://127.0.0.1:43828/callback
   ```

   Press *Add*, tick only **Web API**, accept, *Save*.
   *Why:* this is your own key to Spotify; 127.0.0.1 means "this computer", nothing leaves it.
3. **Add yourself under User Management** (app *Settings → User Management*: your name and your
   Spotify email). *Why:* new Spotify apps only let in the accounts listed there.
4. **Paste the Client ID** (app *Settings → Basic Information*; not the Client secret) into the card's
   field. Loshify saves it in its own setting. No Desktop: *Settings → Plugins → Loshify*. Never into
   a chat.
5. **Connect.** Run once in a terminal (Desktop's terminal pane works), then log in and press *Agree*:

   ```
   hermes auth add loshify:spotify
   ```

   Where to paste it: on Windows press *Start*, type *PowerShell* and press Enter; on a Mac open
   *Terminal*. Only you can run this; Hermes signs plugins in from your own terminal, never from an
   agent or a plugin, so the card cannot do it for you with one click.
6. **Check.** The card checks with one real call to Spotify and turns green.
7. **Get to know me.** Press *Get to know me* and click through, or ask in any chat: "Get to know
   my music and make my first playlist." Building the playlists takes about a minute; the card shows
   what it is doing and then your playlists. Want to do it again later? Open *Taste → Start over*.

### Keeping a rotation fresh on its own (optional)

Loshify runs no background process. Ask "refresh my rotation" when you like, press *Refresh now*, or
let the Hermes scheduler do it, e.g. every Friday at 17:00:

```
hermes cron create "0 17 * * 5" "Refresh my Loshify rotation: loshify_rotation get; if due, read loshify_reactions, then refresh with candidates for each slot." --skill loshify:loshify-curator
```

### Troubleshooting

| What you see | Fix |
|---|---|
| "This redirect URI is not secure" / "Invalid redirect URI" | Register `http://127.0.0.1:43828/callback` in the app settings (not `localhost`, no trailing slash). Hermes chooses a free loopback port for sign-in; Spotify accepts a different port. Save and run step 5 again. |
| "INVALID_CLIENT" or the sign-in failed | The Client ID must be the one from this app's *Basic Information*; paste it again in step 4. |
| Spotify said no to this account | Add your name and Spotify email under *User Management* (step 3). The app owner also needs Premium. |
| The terminal says the port is in use | Another sign-in is still waiting. Close it (other terminal or browser tab), wait a minute, run step 5 again. |
| Connected, but play/skip don't work | That Spotify account has no Premium: playlists and your taste work, playback control doesn't. |
| Spotify didn't answer | Check the internet connection, then press *Check again*. |
| Nothing playing | Start Spotify on any of your devices and press Play once; the remote then takes over. |

Why a port in the address: register the example loopback URI with Spotify. Hermes binds a free
loopback port for sign-in; Spotify accepts the redirect when only the port differs.

## Settings

*Settings → Plugins → Loshify* (stored under `plugins.entries.loshify.settings`):

| Setting | Default | Meaning |
|---|---|---|
| Spotify Client ID | — | Your app's Client ID (secret field, written to `.env`). |
| Taste brief | on | Add the taste brief to music conversations. |
| `dislike_skips` | on | 👎 also skips to the next track. |
| `dislike_removes_from_playlist` | on | 👎 also removes the track, only from playlists Loshify created. |
| `love_saves_to_liked` | off | ❤️ also saves the track to your Liked Songs. |
| `playlist_size` | 25 | Tracks per curated playlist. |
| `archive_probation_days` | 90 | Days a non-❤️ rotation track stays in the Archive (1–3650). |
| `archive_cap` | 200 | Most tracks kept in a rotation's Archive; ❤️ tracks are never evicted (10–2000). |
| `tune_in_days` | 10 | Days between optional Tune-in rounds (7–14). |
| `demo_mode` | off | **Demo/testing switch** (see below). Leave it off for everyday use. |
| `poll_interval_open` / `poll_interval_closed` | 3 / 5 | Seconds between playback polls while the Desktop remote is open / closed. |

**`demo_mode` (for recording a demo or testing).** It changes no data and adds nothing invented.
With it on:

- Tune in is available right away (and again right after a round), with no 7–14-day wait. It
  still opens only after your first run, because the picks aim at a taste Loshify already knows.
- The Loshify card shows the counts of the last refresh that curated the Archive, e.g.
  *"Last Archive refresh: 14 added · 9 aged out · 3 keepers"*, and your taste in one line (top
  artists, ❤️ and 👎 counts). These are the real stored values. A line with no data yet (no
  Archive refresh so far) is left out.

Turn it on in *Settings → Plugins → Loshify*, or in `config.yaml`:
`plugins.entries.loshify.settings.demo_mode: true`.

## Privacy and disclosures

Loshify stores its data on your computer. There is no Loshify server, no telemetry and no
usage reporting.

- **Network.** Loshify talks only to the Spotify Web API (`api.spotify.com`), through Hermes's
  `credentialed_provider_request`. Your browser talks to `accounts.spotify.com` once, during
  `hermes auth add`. Album art in the Desktop remote loads from Spotify's image CDN. When Loshify
  asks the model to write a portrait or picks, Hermes sends taste facts (artist names, track titles,
  and your notes) to your configured model provider.
- **Credentials.** Hermes holds and refreshes the Spotify sign-in; Loshify never sees, stores or logs
  a token. The Client ID is read by Hermes from this profile's settings. Loshify reads no `.env`,
  `auth.json` or other credential store, and does not use another plugin's Spotify login.
- **What it changes on Spotify.** It creates and replaces tracks in playlists it made (private by
  default), moves a rotation's previous edition into the Archive playlist it made and prunes that Archive on refresh (only tracks it put there), removes a 👎'd track only from those playlists, controls playback on your devices when you
  press a button or ask, and saves to Liked Songs only if you turn `love_saves_to_liked` on.
- **Local data.** One SQLite database per Hermes profile at
  `<HERMES_HOME>/plugin-data/loshify/data.db`: your reactions and notes, a decaying per-artist taste
  score, the portrait text, your anchors (≤ 3), your note (≤ 300 characters; edit or clear it in
  the *Taste* tab), the calibration round (5 tracks and your reactions), the last result of each
  Desktop step (so the card can show it after a restart), loved/blocked track lists, playlists Loshify made, recently served
  tracks (to avoid repeats), and your rotation (its slots, your own words if you described it, and
  one record per track Loshify archived: keeper or probation, when it was added and when it
  expires; bounded by `archive_cap`; plus the counts of the last Archive refresh). Retention is bounded: raw reactions are folded into the scores after
  30 days (or once more than 1,000 are waiting); a per-track 👌 count is kept for the 3×👌 = ❤️
  rule (capped at 5,000 tracks); the loved list is capped at 500 and the served list
  at 2,000 rows / 120 days. Blocked (👎) tracks stay blocked until you undo or unblock them. Delete
  that file to forget everything. Desktop keeps one flag (setup card dismissed) in its plugin storage.
- **What the model sees.** The taste brief and tool results (artist names, track titles, your notes)
  go to the language model your Hermes profile uses, like anything else in the chat. Turn off *Taste
  brief* to keep it out of conversations that did not ask for it.
- **No shell commands, no background processes.** The Desktop remote polls playback every few
  seconds while Hermes Desktop is open (configurable above); nothing runs when Hermes is closed.
- **Reads outside its own data:** none. (The only external read is the Spotify API, as your user.)

## Development

```
python -m pytest -q                                              # offline, fake Spotify transport
node --import ./tests/js/register.mjs --test 'tests/js/*.test.mjs'  # Desktop half, SDK stubbed
hermes plugins validate . --install-deps                         # the catalog's own check
```

Layout: `core/` (Spotify client, store, taste, curate, rotations, reactions, setup; no Hermes imports except
the storage and request helpers), `tools/` (tool schemas and handlers), `dashboard/plugin_api.py`
(the Desktop backend router, a thin shell over `core/`), `desktop/plugin.js` (plain ESM, imports
only `@hermes/plugin-sdk` and `react/jsx-runtime`), `skills/loshify-curator/SKILL.md`.

### Sign-in port

Register `http://127.0.0.1:43828/callback` with Spotify. Hermes binds a free loopback port for
the sign-in callback; Spotify accepts a loopback redirect that differs only in port.

## License

MIT, see [LICENSE](LICENSE).
