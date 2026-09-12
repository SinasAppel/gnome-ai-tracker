/* Snapshot validation/formatting checks: the JS mirror of the Python
 * contract tests. Runs unmodified under gjs (pure logic, no Shell imports).
 */

import {
    bucketTotal,
    formatCountdown,
    formatPercent,
    formatTokens,
    limitsExpired,
    parseSnapshotStdout,
    topLimitPercent,
    validateSnapshot,
    weekdayLabel,
} from './lib/snapshot.js';
import { readSnapshot } from './helpers.js';

let failures = 0;
function check(name, cond, detail = '') {
    if (cond) {
        print(`ok - ${name}`);
    } else {
        failures++;
        print(`FAIL - ${name} ${detail}`);
    }
}

const good = readSnapshot('claude-ok');
check('fixture validates', validateSnapshot(good).ok);

// Schema evolution is rejected, never partially rendered.
check('wrong schemaVersion rejected',
    !validateSnapshot({ ...good, schemaVersion: 999 }).ok);
check('seven-day coverage enforced',
    !validateSnapshot({ ...good, recentDays: good.recentDays.slice(0, 6) }).ok);
check('fraction range enforced',
    !validateSnapshot({ ...good, limits: [{ label: 'X', percent: 37, resetsAt: '' }] }).ok);
check('negative buckets rejected',
    !validateSnapshot({ ...good, modelUsage: { m: { inputTokens: -1, outputTokens: 0, cacheReadInputTokens: 0, cacheCreationInputTokens: 0 } } }).ok);
check('malformed JSON rejected', !parseSnapshotStdout('{nope').ok);
check('oversized output rejected', !parseSnapshotStdout(`{"a":"${'x'.repeat(600 * 1024)}"}`).ok);
check('empty object rejected', !validateSnapshot({}).ok);

// Formatting.
check('tokens format', formatTokens(950) === '950' && formatTokens(132500) === '133k' && formatTokens(400070851) === '400M',
    `${formatTokens(950)} ${formatTokens(132500)} ${formatTokens(400070851)}`);
check('percent is fraction-based', formatPercent(0.37) === '37%');
check('top limit picks max', topLimitPercent(good) === 0.37);
check('no limits -> null top', topLimitPercent(readSnapshot('codex-empty')) === null);
const now = Date.parse('2026-09-12T12:00:00+00:00');
check('countdown hours', formatCountdown('2026-09-12T16:00:00+00:00', now) === '4h 0m',
    formatCountdown('2026-09-12T16:00:00+00:00', now));
check('countdown missing', formatCountdown('', now) === '—');
check('weekday label', weekdayLabel('2026-09-12').length > 0);
check('limitsExpired detects rollover',
    limitsExpired({ limits: [{ label: 'S', percent: 0.9, resetsAt: '2000-01-01T00:00:00+00:00' }] }, now) === true);
check('limitsExpired keeps open windows',
    limitsExpired(good, now) === false);
check('bucket total', bucketTotal(good.modelUsage['claude-opus-4']) ===
    42300 + 61800 + 491200 + 182300);

if (failures)
    printerr(`${failures} snapshot check(s) failed\n`);
else
    print('snapshot smoke: all checks passed');
