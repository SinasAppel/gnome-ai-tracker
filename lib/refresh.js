/* Asynchronous collector orchestration.
 *
 * One subprocess per provider so a slow or failing provider can never hide
 * another provider's results. No synchronous scan or network operation ever
 * runs in the Shell process: the bundled Python collector does that work
 * and prints a single validated JSON snapshot per provider.
 *
 * Lifecycle: all timers, cancellables and child processes are owned here
 * and torn down by stop(). Callbacks check this._stopped before touching
 * any actor. Last-good snapshots persist in the cache directory so the
 * popup opens immediately, visibly stale when refresh fails.
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import { parseSnapshotStdout } from './snapshot.js';

const REQUEST_TIMEOUT_SECONDS = 30;
const ERROR_RETRY_SECONDS = 60;
const ADVISED_RETRY_SECONDS = 30;

const PROVIDERS = ['claude', 'codex'];

export class RefreshManager {
    /**
     * @param {object} args
     * @param {string} args.collectorPath absolute path to collector/collect.py
     * @param {Gio.Settings} args.settings
     * @param {string} args.cacheDir absolute cache directory
     * @param {(id: string) => void} args.onUpdate called after any state change
     */
    constructor({ collectorPath, settings, cacheDir, onUpdate }) {
        this._collectorPath = collectorPath;
        this._settings = settings;
        this._cacheDir = cacheDir;
        this._onUpdate = onUpdate;
        this._stopped = true;
        this._states = new Map();
        for (const id of PROVIDERS)
            this._states.set(id, { id, record: null, status: 'loading', error: '', job: null });
        this._pollTimer = 0;
        this._retryTimers = new Map();
        this._lastPollMonotonic = 0;
        this._python = GLib.find_program_in_path('python3') ?? '';
        this._settingsChangedId = 0;
    }

    getState(id) {
        return this._states.get(id);
    }

    providerIds() {
        return [...this._states.keys()].filter(id => this._enabledProviders().includes(id));
    }

    start() {
        this._stopped = false;
        for (const id of PROVIDERS)
            this._loadCached(id);
        this._settingsChangedId = this._settings.connect('changed', (_s, key) => {
            if (this._stopped)
                return;
            if (key === 'refresh-interval') {
                this._schedulePoll();
            } else if (key === 'enabled-providers') {
                this.refreshAll({ force: false });
            }
        });
        this.refreshAll({ force: false });
        this._schedulePoll();
    }

    stop() {
        this._stopped = true;
        if (this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = 0;
        }
        if (this._pollTimer) {
            GLib.source_remove(this._pollTimer);
            this._pollTimer = 0;
        }
        for (const timer of this._retryTimers.values())
            GLib.source_remove(timer);
        this._retryTimers.clear();
        for (const state of this._states.values())
            this._cancelJob(state);
    }

    /** Manual refresh entry point (button, keyboard). */
    refreshAll({ force }) {
        if (this._stopped)
            return;
        for (const id of this.providerIds())
            this.refreshProvider(id, { force, limitsOnly: false });
    }

    /** Refresh-on-open entry point: only providers whose data is stale. */
    refreshStale() {
        if (this._stopped)
            return;
        const interval = this._interval();
        const now = Date.now();
        for (const id of this.providerIds()) {
            const state = this._states.get(id);
            const ageMs = state.record ? now - Date.parse(state.record.updatedAt) : Infinity;
            if (!state.record || !(ageMs >= 0) || ageMs > interval * 1000)
                this.refreshProvider(id, { force: false, limitsOnly: false });
        }
    }

    refreshProvider(id, { force = false, limitsOnly = false } = {}) {
        const state = this._states.get(id);
        if (!state || this._stopped)
            return;
        if (state.job)
            return; // never overlap jobs for one provider
        if (!this._python) {
            this._fail(state, 'python3 not found in PATH');
            return;
        }
        const argv = [this._python, this._collectorPath, '--provider', id];
        if (force)
            argv.push('--force');
        else if (limitsOnly)
            argv.push('--limits-only');
        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        });
        this._applyEnv(launcher, id);
        let proc;
        try {
            proc = launcher.spawnv(argv);
        } catch (e) {
            this._fail(state, `cannot launch collector: ${e.message}`);
            return;
        }
        const cancellable = new Gio.Cancellable();
        state.job = { proc, cancellable, timedOut: false, timeoutId: 0 };
        if (state.record)
            state.status = 'stale';
        else
            state.status = 'loading';
        this._emit(id);

        state.job.timeoutId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, REQUEST_TIMEOUT_SECONDS, () => {
                if (this._stopped || !state.job)
                    return GLib.SOURCE_REMOVE;
                state.job.timedOut = true;
                try {
                    state.job.proc.force_exit();
                } catch { /* already gone */ }
                return GLib.SOURCE_REMOVE;
            }
        );
        proc.communicate_utf8_async(null, cancellable, (p, res) => {
            if (this._stopped)
                return;
            if (state.job)
                GLib.source_remove(state.job.timeoutId);
            const wasJob = state.job;
            state.job = null;
            if (!wasJob)
                return;
            let stdout = '';
            let stderr = '';
            let ok = false;
            try {
                const [success, out, err] = p.communicate_utf8_finish(res);
                ok = success;
                stdout = out ?? '';
                stderr = err ?? '';
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                this._fail(state, this._shortError(`collector error: ${e.message}`));
                return;
            }
            if (wasJob.timedOut) {
                this._fail(state, `collector timed out after ${REQUEST_TIMEOUT_SECONDS}s`);
                return;
            }
            let exitOk = false;
            try {
                exitOk = p.get_if_exited() && p.get_exit_status() === 0;
            } catch { /* treat as failure below */ }
            if (!ok || !exitOk) {
                this._fail(state, this._shortError(`collector failed: ${stderr.trim() || 'unknown error'}`));
                return;
            }
            const parsed = parseSnapshotStdout(stdout);
            if (!parsed.ok) {
                this._fail(state, `bad snapshot: ${parsed.error}`);
                return;
            }
            this._succeed(state, parsed.record);
        });
    }

    // ------------------------------------------------------------ internals

    _enabledProviders() {
        try {
            const list = this._settings.get_strv('enabled-providers');
            const known = list.filter(id => PROVIDERS.includes(id));
            return known.length ? known : [...PROVIDERS];
        } catch {
            return [...PROVIDERS];
        }
    }

    _interval() {
        try {
            const v = this._settings.get_int('refresh-interval');
            return Math.min(3600, Math.max(60, v || 900));
        } catch {
            return 900;
        }
    }

    _applyEnv(launcher, id) {
        try {
            if (id === 'claude') {
                const dir = this._settings.get_string('claude-dir').trim();
                if (dir)
                    launcher.setenv('CLAUDE_CONFIG_DIR', dir, true);
            } else if (id === 'codex') {
                const home = this._settings.get_string('codex-home').trim();
                if (home)
                    launcher.setenv('CODEX_HOME', home, true);
                const bin = this._settings.get_string('codex-executable').trim();
                if (bin)
                    launcher.setenv('GNOME_AI_TRACKER_CODEX_BIN', bin, true);
            }
        } catch { /* defaults apply */ }
    }

    _succeed(state, record) {
        state.record = record;
        state.status = 'ok';
        state.error = '';
        this._storeCached(state.id, record);
        this._emit(state.id);
    }

    _fail(state, message) {
        // Keep the last good record visibly stale; only go to error state
        // when nothing was ever collected.
        if (state.record)
            state.status = 'stale';
        else
            state.status = 'error';
        state.error = message;
        this._emit(state.id);
        // The collector explicitly asks for a sooner retry after transport
        // failures (retryAdvised); anything else waits out the error window.
        this._scheduleRetry(state.id, state.record?.retryAdvised ? ADVISED_RETRY_SECONDS : ERROR_RETRY_SECONDS);
    }

    _scheduleRetry(id, delaySeconds) {
        if (this._stopped)
            return;
        if (this._retryTimers.has(id))
            return;
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, delaySeconds, () => {
            this._retryTimers.delete(id);
            if (!this._stopped)
                this.refreshProvider(id, { force: false, limitsOnly: false });
            return GLib.SOURCE_REMOVE;
        });
        this._retryTimers.set(id, timer);
    }

    _schedulePoll() {
        if (this._pollTimer)
            GLib.source_remove(this._pollTimer);
        const interval = this._interval();
        this._lastPollMonotonic = GLib.get_monotonic_time();
        this._pollTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, interval, () => {
            if (this._stopped)
                return GLib.SOURCE_REMOVE;
            // Clock jumps (suspend/resume) surface as an overlong gap: the
            // cached limits may have crossed a reset boundary, so refresh.
            const elapsed = (GLib.get_monotonic_time() - this._lastPollMonotonic) / 1_000_000;
            this._lastPollMonotonic = GLib.get_monotonic_time();
            const force = elapsed > interval * 2 + 60;
            this.refreshAll({ force });
            return GLib.SOURCE_CONTINUE;
        });
    }

    _cacheFile(id) {
        return GLib.build_filenamev([this._cacheDir, `snapshot-${id}.json`]);
    }

    _loadCached(id) {
        const state = this._states.get(id);
        try {
            const [, bytes] = GLib.file_get_contents(this._cacheFile(id));
            const text = new TextDecoder().decode(bytes);
            const parsed = parseSnapshotStdout(text);
            if (parsed.ok) {
                state.record = parsed.record;
                state.status = 'stale';
            }
        } catch { /* first run: no cache yet */ }
    }

    _storeCached(id, record) {
        try {
            GLib.mkdir_with_parents(this._cacheDir, 0o755);
            const file = Gio.File.new_for_path(this._cacheFile(id));
            const text = JSON.stringify(record);
            // Atomic replace; snapshots carry no secrets.
            file.replace_contents(
                new TextEncoder().encode(text), null, false,
                Gio.FileCreateFlags.REPLACE_DESTINATION, null
            );
        } catch { /* caching is best-effort only */ }
    }

    _cancelJob(state) {
        if (!state.job)
            return;
        try {
            GLib.source_remove(state.job.timeoutId);
        } catch { /* already fired */ }
        try {
            state.job.cancellable.cancel();
        } catch { /* ignore */ }
        try {
            state.job.proc.force_exit();
        } catch { /* already gone */ }
        state.job = null;
    }

    _shortError(message) {
        // One line, bounded: stderr can contain file paths on failure.
        const line = message.split('\n')[0].trim();
        return line.length > 200 ? `${line.slice(0, 197)}…` : line;
    }

    _emit(id) {
        if (!this._stopped) {
            try {
                this._onUpdate(id);
            } catch { /* rendering must never break collection */ }
        }
    }
}
