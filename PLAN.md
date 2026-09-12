# GNOME AI Tracker implementation plan

Status: SUBMITTED to extensions.gnome.org 12 September 2026 (version 1, `Unreviewed`, Shell 50). New UUID `ai-usage-tracker@sinasappel.github.io`; public repo `SinasAppel/gnome-ai-tracker` (main at `81fb17d`). Milestones 1–4 and 6 (code side) are done; milestone 5 (Fireworks) is open. Awaiting reviewer feedback by email (days–weeks). NOTE: the host desktop still runs the OLD build (`ai-usage-tracker@gnome.local`); only the `arch-gnome-test` VM runs the submitted build.

## Objective and scope

Top-panel GNOME Shell indicator opening a compact, scrollable popup with provider tabs, plan name, usage limits and reset countdowns, seven days of token usage, and a model breakdown. GNOME styling, keyboard navigation, and scaling; not a pixel copy of the Omarchy screenshot.

First release (done): Claude Code and Codex, local installation, read-only collection, configurable refresh, preferences, clear failure states. Fireworks remains a subsequent milestone. Cross-device sync, other agent clients, spending estimates for subscriptions, and notifications remain deferred.

Distribution goal (new): publish as a public GNOME Shell extension on extensions.gnome.org (milestone 6), with GitHub releases and possibly an AUR package as unreviewed parallel tracks. Publication requires pre-submission fixes (license, UUID rename, GTK-in-Shell removal, slimmed package, metadata URL) detailed in milestone 6.

## Verified upstream reference

Omarchy `quattro` source at commit `31bd80daa4613ffdee995ac27467fce5a2990806`. Its QML panel consumes JSON from separate provider collectors; default polling 900s. QML UI was reimplemented for GNOME; collector adapters were adapted (see THIRD_PARTY_NOTICES).

Sources pinned to that revision:

- [Agents panel and data contract](https://github.com/omacom/omarchy/blob/31bd80daa4613ffdee995ac27467fce5a2990806/shell/plugins/agents/README.md)
- [Claude collector](https://github.com/omacom/omarchy/blob/31bd80daa4613ffdee995ac27467fce5a2990806/bin/omarchy-agent-usage-claude)
- [Codex collector](https://github.com/omacom/omarchy/blob/31bd80daa4613ffdee995ac27467fce5a2990806/bin/omarchy-agent-usage-codex)
- [Fireworks collector](https://github.com/omacom/omarchy/blob/31bd80daa4613ffdee995ac27467fce5a2990806/bin/omarchy-agent-usage-fireworks)
- [MIT license](https://github.com/omacom/omarchy/blob/31bd80daa4613ffdee995ac27467fce5a2990806/LICENSE)

## Architecture (as built)

1. **Native interface:** JavaScript ES modules (GJS), `PanelMenu.Button`, `PopupMenu`, St widgets (`extension.js`, `ui/panel.js`). Preferences are a separate GTK4/libadwaita process (`prefs.js`) on GSettings. Follows the [GNOME extension development guide](https://gjs.guide/extensions/development/creating.html).
2. **Collection helper:** bundled Python 3 program (`collector/collect.py` + `collector/gnome_ai_tracker/`), one subprocess per provider via `Gio.Subprocess` with an argument array (`lib/refresh.js`). Transcript scanning and provider requests stay outside the Shell process. No daemon. Preferences shows a dependency check (python3, `claude`, `codex`).
3. **Versioned JSON boundary:** helper prints one normalized, display-safe snapshot per provider (schemaVersion 1, 512 KiB cap). UI never receives credentials or transcript text. Contract enforced both sides: `tests/test_schema.py` and `lib/snapshot.js`.
4. **Independent refresh:** cached snapshots render immediately. One subprocess per provider; one timeout cannot hide the other. 15-minute default polling (60–3600s configurable), Refresh button, h/l tab switching, r to refresh, refresh-on-open only if stale. Countdown labels tick locally every 30s while open. No overlapping jobs, 30s request timeout, retry-after/backoff (60s error / 30s advised), refresh after resume on clock jump.
5. **Lifecycle:** timers/monitors only when enabled; `disable()` cancels requests, kills/reaps children, disconnects signals, destroys actors. Async callbacks guard on stopped flag. Reviewed against [GNOME publication guidelines](https://gjs.guide/extensions/review-guidelines/review-guidelines.html).

Layout (present in repo):

```text
extension.js, metadata.json, stylesheet.css, prefs.js
schemas/                  # GSettings schema + compiled
ui/panel.js               # indicator popup content
lib/snapshot.js           # validation + formatting
lib/refresh.js            # subprocess orchestration, cache, backoff
collector/collect.py      # entry point (--provider claude|codex)
collector/gnome_ai_tracker/  # claude.py (830 lines), codex.py (580), common.py
tests/fixtures/           # claude_sample.jsonl, codex_sample.jsonl, snapshots/
tests/                    # test_claude_accounting.py, test_codex_accounting.py,
                          # test_schema.py, gjs/ (panel/snapshot/refresh smoke)
scripts/                  # package.sh, install-local.sh
README.md, THIRD_PARTY_NOTICES, PLAN.md
gnome-ai-tracker@50.zip   # built 12 Sep 2026 via scripts/package.sh
```

## Data acquisition and correctness (validated live 12 Sep 2026)

| Provider | Account information | Historical usage | Live result |
|---|---|---|---|
| Claude | OAuth usage endpoint with existing CLI credentials | Native Claude project JSONL logs (+ stats-cache/history fallback, pi/omp + opencode Anthropic sessions) | `Pro` plan, Session 0% / Weekly 4%; 3,683 prompts, 91 sessions, 29 active days; per-block re-emits deduped by message id. Expired login yields explicit `Sign-in expired`, missing login yields `Waiting for auth` — never silent zeros |
| Codex | Codex app-server RPC for account + rate limits | Native Codex session JSONL (`sessions/` + `archived_sessions/`, 30-day window; pi/omp openai-codex + opencode `openai` rows) | `plus` plan, 5h 16% / Weekly 2%; 1,350 prompts, 75 sessions, 13 active days |

Observed upstream techniques confirmed against installed CLIs (Python 3.14.7). CLI-owned auth flow only; no token refresh, no credential-file writes, no AI conversation initiated to measure usage.

Custom paths respected (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, prefs overrides `claude-dir`, `codex-home`, `codex-executable`); GUI sessions don't inherit shell vars, so overrides live in GSettings, never sourced from dotfiles. Snapshots carry schema version, provider id/name, plan, separate limits/history statuses + timestamps, limits with used percent + reset timestamp, daily/model token totals with date coverage. No credentials in snapshots, GSettings, argv, or logs.

Rules (all implemented + tested):

- Account limits vs machine-local history labeled as different measurements; no subscription % inferred from token totals.
- Provider buckets/names dynamic; no hard-coded model names.
- Per-provider input/output/cache-read/cache-write accounting; cached input not double-counted.
- Cumulative Codex counters via valid deltas; retries, repeats, resumed/forked sessions, model changes, counter resets tested.
- Local-timezone buckets; seven calendar days incl. today; model totals match the displayed period.
- Missing data = unknown, not zero; last-good snapshot kept visibly stale; limit values invalidated past reset boundary until refreshed.
- Incremental file index + aggregate cache under `$XDG_CACHE_HOME/gnome-ai-tracker` (aggregates + index metadata only, atomic writes, user-only access); append/truncation/replacement/deletion handled.
- History coverage documented; no unbounded "all-time" scan claims.

## User experience (as built)

Panel button: symbolic AI icon + selected provider's used percentage (icon-only / hidden modes available). Clicking opens:

1. Provider name, plan, freshness dot + "Updated … ago".
2. Claude/Codex tabs when both enabled (h/l keys switch).
3. Dynamic limit rows with percentages, progress meters, reset countdowns.
4. Seven daily horizontal token bars (Today highlighted).
5. Models sorted by tokens for the same period with in/out/cache split in text (no hover needed).
6. Refresh + Preferences actions; error cards with Retry for auth/limit failures; stale note when showing cache after failed refresh.

Bounded scroll area (max-height 560px) for small screens, accessible names on meters/tabs/buttons, keyboard focus + tab switching, light/dark contrast via theme classes. Setup state when no provider detected; optional auto-hide. Settings: enabled providers, shown-first provider, panel label, auto-hide, refresh interval, path overrides. No secrets in settings.

## Delivery milestones and acceptance gates

### 1. Prove collection before building the full UI — DONE

Adapters validated against installed CLIs; sample normalized snapshots compared with each client's usage view; token arithmetic verified on fixtures + local aggregates. Live limits confirmed for both providers; auth-failure states explicit.

### 2. Build the GNOME popup against fixtures — DONE

Indicator, provider switching, limits, daily/model bars, empty/error/stale states, preferences — all rendering from deterministic fixture snapshots (`tests/fixtures/snapshots/`), verified on GNOME 50 light/dark + fractional scales; tall content scrolls in-bounds.

### 3. Connect collection and harden lifecycle — DONE

Async subprocess orchestration, independent provider updates, atomic caching, incremental parsing, backoff, manual refresh, suspend/resume handling. History vs limit errors distinguished; old data kept visibly stale on transient failure. Popup opens instantly from cache; no sync scan/network in Shell; repeated enable/disable leaves no children, timers, or callbacks.

### 4. Validate and package the first release — DONE (12 Sep 2026)

- `python3 -m unittest discover -s tests`: 22 tests OK (Claude/Codex accounting, schema contract).
- `./tests/run_gjs_tests.sh`: panel + snapshot + live-refresh smoke suites pass (panel rendering, snapshot contract, live collector independence, cache round-trip, failure state).
- Live collector run: both snapshots well-formed with limits + history (numbers above).
- Nested GNOME 50 Wayland session: extension loads `ACTIVE`, zero JS errors.
- `scripts/package.sh` → `gnome-ai-tracker@50.zip` (67 KiB); local install documented in README; installed copy active at `~/.local/share/gnome-shell/extensions/ai-usage-tracker@gnome.local`; cache at `~/.cache/gnome-ai-tracker`.
- Malformed JSONL, missing/expired credentials, offline/rate-limit, schema change, oversized output, large history, timezone rollover, account change, dedup paths covered by tests/smoke checks.
- Clean-room validation 12 Sep 2026 in a fresh Arch Linux + GNOME 50.4 VM (libvirt `arch-gnome-test`): documented install flow (`package.sh` + `gnome-extensions install --force`) → `ACTIVE`, zero JS errors; panel icon, live popup (Codex `unavailable` / Claude `Waiting for auth` honest states), tab switching, and Preferences window all verified via screenshots; 22/22 Python tests and panel/snapshot GJS smokes pass (refresh-smoke live-data checks fail only for lack of CLI logins, as designed).

### 5. Extend provider coverage — OPEN (next)

Add Fireworks through the same adapter contract (balances separate from subscription limits, estimates labeled). Then consider opencode/pi integration and multi-device aggregation with explicit dedup + account-identity rules.

### 6. Publish on extensions.gnome.org — SUBMITTED 12 Sep 2026 (v1 `Unreviewed`, awaiting review)

Goal: a public listing installable via the Extensions app / website. Review gates from the [official review guidelines](https://gjs.guide/extensions/review-guidelines/review-guidelines.html), checked against this repo:

1. **Remove GTK from the Shell process (hard reject).** DONE: `extension.js` no longer imports `Gdk`/`Gtk`; icon-theme search-path setup removed, `_makeIcon()` is `Gio.FileIcon` PNG-first + stock symbolic fallback only. Verified: `rg` shows `Gtk`/`Gdk`/`Adw` only in `prefs.js` (separate process, allowed).
2. **Add a LICENSE file.** DONE: `LICENSE` added (GPL-2.0-or-later, copyright sinasappel 2026), `THIRD_PARTY_NOTICES` kept for the MIT Omarchy-derived collector code; README Provenance updated.
3. **Rename the UUID before first public install.** DONE: new UUID `ai-usage-tracker@sinasappel.github.io` (`metadata.json`, `scripts/package.sh`, `scripts/install-local.sh`, README). Schema id/path unchanged (`org.gnome.shell.extensions.ai-usage-tracker`), so settings survive upgrades.
4. **Justify the Python helper in the submission notes.** DONE (text below — reuse verbatim in the EGO reviewer thread and for any re-upload):
   > The bundled Python helper (collector/collect.py) is necessary because transcript scanning (large JSONL history) and provider network I/O must not run synchronously in the Shell process. The extension spawns one async Gio.Subprocess per provider with an argument array, enforces a 30 s timeout, and the helper prints a single validated, display-safe JSON snapshot (512 KiB cap, schemaVersion 1, no credentials or transcript text). Snapshots are the only data crossing into the Shell; see lib/snapshot.js + tests/test_schema.py for the contract enforced on both sides.
   > Note on shexli EGO-X-004: the single synchronous read (GLib.file_get_contents of two small JSON cache files in lib/refresh.js _loadCached) runs once per enable so the popup renders instantly from cache; all network and history scanning stays in the async Python subprocess. Actor lifecycle warnings (EGO-L-002/L-005) are addressed — disable() explicitly destroys every owned actor and releases all references.
   > Extra context if asked: extension.js uses no Gtk/Gdk (Gio + St only); Gtk/Adw live solely in prefs.js, which runs as a separate process.
5. **Slim the package + fix metadata.** DONE: `package.sh` ships only runtime files + `THIRD_PARTY_NOTICES` + `LICENSE` (dropped `tests/`, `scripts/`, `PLAN.md`, `README.md`); `metadata.json` `url` points at `https://github.com/sinasappel/gnome-ai-tracker`, deprecated `version` key replaced with `version-name: "1"`. Result: `gnome-ai-tracker@50.zip`, 27 files.
6. **Submit:** DONE 12 Sep 2026 by owner via the website (`Add yours` upload form — zip-only; screenshots/description/reviewer thread live on the extension page afterwards). Submitted `gnome-ai-tracker@50.zip` (51 KiB, `version-name: "1"`, Shell 50) → listing "AI Usage Tracker", status v1 `Unreviewed`. Screenshots staged at `/tmp/opencode/ego/` (`1-panel-desktop.png`, `2-popup-tabs.png`, `3-preferences.png`) for the "Upload screenshot" button on the extension page. Pre-upload verification: both test suites + `package.sh` on host (22/22 + all GJS smokes OK); clean-VM re-verification of the submitted bytes (new UUID `ACTIVE`, zero JS errors; FileIcon-only panel icon, live Codex popup, honest Claude `Waiting for auth`, both tabs, Preferences window; VM suites 22/22 + panel/snapshot smokes OK; disable/enable cycle clean after the destroy fix). Shexli on the final zip: 0 errors, 1 warning (EGO-X-004, by design — see item 4 text).
   Review-response loop (when the reviewer emails): fix in repo → re-run `python3 -m unittest discover -s tests` + `./tests/run_gjs_tests.sh` + `./scripts/package.sh` → re-run shexli (`/tmp/opencode/shexli-venv/bin/shexli <zip>`) → scp zip to `tester@192.168.122.202:/tmp/`, `gnome-extensions install --force` + disable/enable cycle + `journalctl --user -b | grep -ci "JS ERROR"` (expect 0) → commit + push → bump `version-name` in `metadata.json` for every re-upload (EGO requires a new version per zip) → owner re-uploads via `Add yours` (same form; system appends it as the next version).

Parallel unreviewed tracks (no gate, can ship anytime): GitHub releases (zip + `install-local.sh`) and an AUR package (fits the Arch+GNOME audience).

## Current next steps

1. Await EGO review (owner's email, days–weeks). When feedback arrives: follow the review-response loop at the end of item 6 above. If approved, the listing goes public for Shell 50.
2. Migrate the HOST desktop to the submitted build (still on old UUID): `./scripts/package.sh && gnome-extensions install --force gnome-ai-tracker@50.zip`, log out/in (Wayland discovers new UUIDs only at startup), then `gnome-extensions enable ai-usage-tracker@sinasappel.github.io && gnome-extensions uninstall ai-usage-tracker@gnome.local`. NOTE: same-schema-id settings carry over automatically.
3. Confirm screenshots landed on the EGO listing (`/tmp/opencode/ego/*.png` are the staged set; re-take from the VM via `virsh --connect qemu:///system screenshot` if the listing needs more).
4. Decide Fireworks scope (billing API access + balance-vs-estimate labeling) before adding a third adapter.
5. Parallel unreviewed tracks (no gate, can ship anytime): GitHub release (attach `gnome-ai-tracker@50.zip`) and an AUR package.
6. Optional hardening only if a failure is observed in daily use: backoff tuning, additional redacted fixtures.
7. Keep README's Troubleshooting in sync with any collector or Shell-version change; re-run both test suites + `scripts/package.sh` before any reinstall. (Owner trimmed README's intro/status on GitHub web 12 Sep 2026 — commit `9d6a847` — keep that lean style.)
