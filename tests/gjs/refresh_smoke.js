/* Refresh manager smoke test: the real lib/refresh.js (pure Gio, no Shell
 * imports) drives the real collector against the live machine. Asserts
 * independent per-provider updates, instant cache load, and clean stop.
 * Run via tests/run_gjs_tests.sh, not directly.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import { RefreshManager } from './lib/refresh.js';
import { fakeSettings } from './helpers.js';

const COLLECTOR = GLib.getenv('AI_TRACKER_COLLECTOR');
const CACHE = GLib.getenv('AI_TRACKER_CACHE');

let failures = 0;
function check(name, cond, detail = '') {
    if (cond) {
        print(`ok - ${name}`);
    } else {
        failures++;
        print(`FAIL - ${name} ${detail}`);
    }
}

const loop = new GLib.MainLoop(null, false);
const updates = [];
const manager = new RefreshManager({
    collectorPath: COLLECTOR,
    settings: fakeSettings(),
    cacheDir: CACHE,
    onUpdate: id => updates.push(id),
});

manager.start();

// Instant cache on second start: stop, restart, records must be present
// immediately without waiting for subprocesses.
function snapshotStates() {
    return {
        claude: manager.getState('claude'),
        codex: manager.getState('codex'),
    };
}

GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
    const { claude, codex } = snapshotStates();
    if (claude.record && codex.record) {
        try {
            check('claude snapshot collected', claude.record.id === 'claude' && claude.record.ready === true);
            check('claude history present', claude.record.totalPrompts > 0, `${claude.record.totalPrompts}`);
            // Either live limits with no status, or zero limits with an
            // honest auth state — never silent zeros.
            check('claude limits live or honest auth state',
                (claude.record.limits.length > 0 && claude.record.usageStatusText === '') ||
                claude.record.usageStatusText !== '',
                `limits=${claude.record.limits.length} status=${claude.record.usageStatusText}`);
            check('codex snapshot collected', codex.record.id === 'codex');
            check('codex live limits present', codex.record.limits.length >= 1,
                JSON.stringify(codex.record.limits));
            check('both providers updated independently',
                updates.includes('claude') && updates.includes('codex'), updates.join(','));

            manager.stop();
            // Cache files written atomically for instant open.
            const claudeCache = Gio.File.new_for_path(`${CACHE}/snapshot-claude.json`);
            const codexCache = Gio.File.new_for_path(`${CACHE}/snapshot-codex.json`);
            check('cache files written', claudeCache.query_exists(null) && codexCache.query_exists(null));

            // Restart: cached records render immediately as stale.
            const seen = [];
            const manager2 = new RefreshManager({
                collectorPath: COLLECTOR,
                settings: fakeSettings(),
                cacheDir: CACHE,
                onUpdate: id => seen.push(id),
            });
            manager2.start();
            const s2 = manager2.getState('claude');
            check('cache loads instantly on start', s2.record !== null && s2.status !== 'loading');
            manager2.stop();

            // Broken collector path surfaces as error state, not a crash.
            const badSeen = [];
            const manager3 = new RefreshManager({
                collectorPath: '/nonexistent/collect.py',
                settings: fakeSettings(),
                cacheDir: `${CACHE}-bad`,
                onUpdate: id => badSeen.push(id),
            });
            manager3.start();
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => {
                const bad = manager3.getState('claude');
                check('collector failure is an error state, kept visible',
                    (bad.status === 'error' || bad.status === 'stale') && badSeen.length > 0,
                    `${bad.status} ${bad.error}`);
                manager3.stop();
                loop.quit();
                return GLib.SOURCE_REMOVE;
            });
        } catch (e) {
            printerr(`harness error: ${e.message}\n`);
            failures++;
            loop.quit();
        }
        return GLib.SOURCE_REMOVE;
    }
    return GLib.SOURCE_CONTINUE;
});

// Hard guard: never hang the suite.
GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 90, () => {
    printerr('refresh smoke timed out waiting for snapshots\n');
    failures++;
    try {
        manager.stop();
    } catch { /* ignore */ }
    loop.quit();
    return GLib.SOURCE_REMOVE;
});

loop.run();

if (failures)
    printerr(`${failures} refresh check(s) failed\n`);
else
    print('refresh smoke: all checks passed');
