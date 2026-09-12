# GNOME AI Tracker

Top-panel GNOME Shell extension for local AI coding subscription usage:
plan name, rate limits with reset countdowns, seven days of token history,
and a per-model breakdown for Claude Code and Codex.

## Install (local)

```sh
./scripts/package.sh
gnome-extensions install --force gnome-ai-tracker@50.zip
```

The running Shell only discovers new extensions at startup, so log out
and back in (Wayland has no shell restart), then:

```sh
gnome-extensions enable ai-usage-tracker@sinasappel.github.io
```

To trial without touching the live desktop first:

```sh
dbus-run-session -- gnome-shell --wayland --headless
```

Preferences expose providers, panel label, auto-hide, refresh interval
(60–3600s), path overrides (`claude-dir`, `codex-home`,
`codex-executable`), and a dependency check (python3, `claude`, `codex`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Claude shows `Sign-in expired` | Saved CLI login lapsed; history still renders. Start Claude Code or run `claude auth login`. |
| Claude shows `Waiting for auth` | No CLI login found; local history still renders. |
| Codex shows `Codex unavailable` | `codex` not on PATH (or override it in Preferences). Sessions still render. |
| Stale data note | Last refresh failed; previous numbers kept visibly stale. Check network / CLIs, or press Refresh. |
| No indicator after install | Log out/in so the Shell picks up the new extension directory. |

## Uninstall and data cleanup

```sh
gnome-extensions disable ai-usage-tracker@sinasappel.github.io
gnome-extensions uninstall ai-usage-tracker@sinasappel.github.io
rm -rf ~/.cache/gnome-ai-tracker   # aggregate cache (never prompts or credentials)
```

Uninstall removes code only; CLI transcripts, logins, and settings under
`~/.claude` / `~/.codex` are untouched. `dconf reset -f
/org/gnome/shell/extensions/ai-usage-tracker/` clears preferences.

## Data scope

- Claude: `<claude-dir>/projects` JSONL transcripts (+ `stats-cache.json` /
  `history.jsonl` fallback, pi/omp and opencode Anthropic sessions),
  OAuth usage endpoint for limits.
- Codex: `sessions/` + `archived_sessions/` JSONL (last 30 days), pi/omp
  openai-codex sessions, opencode `openai` rows, app-server RPC for limits.
- Cache: `$XDG_CACHE_HOME/gnome-ai-tracker` (aggregates only, never
  prompts or credentials).

## Provenance

Collector adapters derive from Omarchy (MIT, commit `31bd80d`). See
`THIRD_PARTY_NOTICES`. Own code is GPL-2.0-or-later; see `LICENSE`.
Source: <https://github.com/sinasappel/gnome-ai-tracker>.
