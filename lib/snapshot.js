/* Snapshot validation and formatting.
 *
 * Mirrors the contract asserted in tests/test_schema.py — both must stay in
 * sync. The collector prints JSON; this module decides whether it is safe
 * to render. Anything unexpected is rejected, never partially rendered.
 */

export const SCHEMA_VERSION = 1;
export const MAX_SNAPSHOT_BYTES = 512 * 1024;

const REQUIRED_INTS = [
    'todayPrompts',
    'todaySessions',
    'todayTotalTokens',
    'totalPrompts',
    'totalSessions',
    'activeDays',
];

const BUCKET_FIELDS = [
    'inputTokens',
    'outputTokens',
    'cacheReadInputTokens',
    'cacheCreationInputTokens',
];

/** @returns {{ok: true, record: object} | {ok: false, error: string}} */
export function validateSnapshot(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value))
        return { ok: false, error: 'snapshot is not an object' };
    if (value.schemaVersion !== SCHEMA_VERSION)
        return { ok: false, error: `unsupported schemaVersion ${value.schemaVersion}` };
    for (const key of ['id', 'name', 'updatedAt']) {
        if (typeof value[key] !== 'string' || value[key] === '')
            return { ok: false, error: `missing ${key}` };
    }
    if (typeof value.ready !== 'boolean' || typeof value.hasLocalStats !== 'boolean')
        return { ok: false, error: 'missing ready/hasLocalStats' };
    for (const key of REQUIRED_INTS) {
        if (!Number.isInteger(value[key]) || value[key] < 0)
            return { ok: false, error: `bad ${key}` };
    }
    if (!Array.isArray(value.recentDays) || value.recentDays.length !== 7)
        return { ok: false, error: 'recentDays must cover seven days' };
    for (const day of value.recentDays) {
        if (!day || typeof day.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day.date))
            return { ok: false, error: 'bad recentDays date' };
        if (!Number.isInteger(day.messageCount) || day.messageCount < 0)
            return { ok: false, error: 'bad recentDays count' };
    }
    if (!Array.isArray(value.activeDates) || value.activeDates.length !== value.activeDays)
        return { ok: false, error: 'activeDates/activeDays mismatch' };
    if (value.modelUsage === null || typeof value.modelUsage !== 'object' || Array.isArray(value.modelUsage))
        return { ok: false, error: 'bad modelUsage' };
    for (const [model, bucket] of Object.entries(value.modelUsage)) {
        if (typeof model !== 'string' || model === '')
            return { ok: false, error: 'bad model name' };
        for (const field of BUCKET_FIELDS) {
            if (!bucket || !Number.isInteger(bucket[field]) || bucket[field] < 0)
                return { ok: false, error: `bad ${model}.${field}` };
        }
    }
    if (!Array.isArray(value.limits))
        return { ok: false, error: 'bad limits' };
    for (const limit of value.limits) {
        if (!limit || typeof limit.label !== 'string' || limit.label === '')
            return { ok: false, error: 'bad limit label' };
        if (typeof limit.percent !== 'number' || !(limit.percent >= 0) || limit.percent > 1)
            return { ok: false, error: `bad percent for ${limit.label}` };
        if (typeof limit.resetsAt !== 'string')
            return { ok: false, error: `bad resetsAt for ${limit.label}` };
    }
    for (const key of ['tierLabel', 'usageStatusText', 'authHelpText']) {
        if (value[key] !== undefined && typeof value[key] !== 'string')
            return { ok: false, error: `bad ${key}` };
    }
    return { ok: true, record: value };
}

/** Parse stdout into a validated snapshot. */
export function parseSnapshotStdout(stdout) {
    if (stdout.length > MAX_SNAPSHOT_BYTES)
        return { ok: false, error: `snapshot too large (${stdout.length} bytes)` };
    let parsed;
    try {
        parsed = JSON.parse(stdout);
    } catch (e) {
        return { ok: false, error: `invalid JSON: ${e.message}` };
    }
    return validateSnapshot(parsed);
}

export function bucketTotal(bucket) {
    return BUCKET_FIELDS.reduce((sum, f) => sum + (bucket[f] || 0), 0);
}

/** Compact token count: 950 -> "950", 132500 -> "132.5k", 400070851 -> "400.1M". */
export function formatTokens(n) {
    if (!Number.isFinite(n) || n < 0)
        return '—';
    if (n < 1000)
        return `${Math.round(n)}`;
    if (n < 1_000_000) {
        const v = n / 1000;
        return `${v >= 100 ? Math.round(v) : v.toFixed(1)}k`;
    }
    const v = n / 1_000_000;
    return `${v >= 100 ? Math.round(v) : v.toFixed(1)}M`;
}

/** "0.37" -> "37%". Input is a used fraction, never provider percent. */
export function formatPercent(fraction) {
    return `${Math.round(fraction * 100)}%`;
}

/** Highest limit fraction, for the panel label. Null when no limits. */
export function topLimitPercent(record) {
    if (!record.limits.length)
        return null;
    return Math.max(...record.limits.map(l => l.percent));
}

/** Human countdown to a reset timestamp: "3h 12m", "25m", "in 40s", "—". */
export function formatCountdown(resetsAt, nowMs = Date.now()) {
    if (!resetsAt)
        return '—';
    const target = Date.parse(resetsAt);
    if (Number.isNaN(target))
        return '—';
    let secs = Math.max(0, Math.round((target - nowMs) / 1000));
    if (secs < 60)
        return `in ${secs}s`;
    const mins = Math.floor(secs / 60);
    if (mins < 60)
        return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 48)
        return `${hours}h ${mins % 60}m`;
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h`;
}

/** "Updated 3m ago" / "Updated just now" / "Updated ..." fallback. */
export function formatAge(updatedAt, nowMs = Date.now()) {
    const ts = Date.parse(updatedAt);
    if (Number.isNaN(ts))
        return 'Updated: unknown';
    const secs = Math.max(0, Math.round((nowMs - ts) / 1000));
    if (secs < 45)
        return 'Updated just now';
    if (secs < 3600)
        return `Updated ${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) {
        const h = Math.floor(secs / 3600);
        return `Updated ${h}h ${Math.floor((secs % 3600) / 60)}m ago`;
    }
    return `Updated ${Math.floor(secs / 86400)}d ago`;
}

/** Short weekday for a YYYY-MM-DD date in the local timezone. */
export function weekdayLabel(dateStr) {
    const [y, m, d] = dateStr.split('-').map(Number);
    const dt = new Date(y, m - 1, d);
    if (Number.isNaN(dt.getTime()))
        return dateStr.slice(5);
    return dt.toLocaleDateString(undefined, { weekday: 'short' });
}

/** True when the record still describes the current subscription window. */
export function limitsExpired(record, nowMs = Date.now()) {
    if (!record.limits.length)
        return false;
    return record.limits.every(l => {
        if (!l.resetsAt)
            return false;
        const target = Date.parse(l.resetsAt);
        return !Number.isNaN(target) && target <= nowMs;
    });
}
