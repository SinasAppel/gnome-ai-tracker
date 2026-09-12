/* Popup content: provider tabs, limits, weekly bars, model breakdown.
 *
 * Rendered purely from validated collector snapshots (see lib/snapshot.js)
 * plus per-provider refresh state. Never touches the network or disk.
 * Tall content lives in a bounded scroll area owned by extension.js.
 */

import Clutter from 'gi://Clutter';
import St from 'gi://St';

import {
    bucketTotal,
    formatAge,
    formatCountdown,
    formatPercent,
    formatTokens,
    weekdayLabel,
} from '../lib/snapshot.js';

function el(cls, text = '') {
    const w = new St.Label({ text, style_class: cls, x_expand: true });
    w.clutter_text.line_wrap = false;
    w.clutter_text.ellipsize = 1; // Pango.EllipsizeMode.END
    return w;
}

/** Horizontal meter: a fill widget sized to a fraction of its track.
 *
 * Deliberately Cairo-free: Clutter's JS color helpers are not all exposed
 * in the Shell process, and widget layout handles scaling for free. The
 * fill width refreshes on every track allocation (popup open, resize,
 * scrollbar appearance).
 */
function makeMeter(fraction, small, accessibleName) {
    const frac = Math.min(1, Math.max(0, fraction || 0));
    const track = new St.BoxLayout({
        style_class: `ai-tracker-track${small ? ' ai-tracker-small' : ''}`,
        x_expand: true,
    });
    const fill = new St.Widget({
        style_class: `ai-tracker-fill${small ? ' ai-tracker-small' : ''}`,
        x_expand: false,
    });
    track.add_child(fill);
    track.set_accessible_name(accessibleName);
    const update = () => {
        // Off-stage actors have no allocation; measuring them warns in the
        // log. The first layout pass notifies and sizes the fill then.
        if (!track.get_stage())
            return;
        const avail = track.get_width();
        if (avail > 0)
            fill.set_width(Math.round(avail * frac));
    };
    track.connect('notify::allocation', update);
    // First layout may precede any allocation notification.
    update();
    return track;
}

export class TrackerPanel {
    /**
     * @param {object} args
     * @param {(id: string) => void} args.onSelect provider tab callback
     * @param {() => void} args.onRefresh refresh button callback
     * @param {() => void} args.onPrefs preferences button callback
     */
    constructor({ onSelect, onRefresh, onPrefs }) {
        this._onSelect = onSelect;
        this._onRefresh = onRefresh;
        this._onPrefs = onPrefs;
        this.actor = new St.BoxLayout({ vertical: true, style_class: 'ai-tracker-panel' });
    }

    destroy() {
        this.actor.destroy();
    }

    /**
     * @param {Array<{id: string, record: object|null, status: string, error: string}>} states
     * @param {string} selectedId
     */
    render(states, selectedId) {
        this.actor.destroy_all_children();
        if (!states.length) {
            this.actor.add_child(el('ai-tracker-dim', 'No providers enabled. Open Preferences to enable one.'));
            this._addFooter();
            return;
        }
        let selected = states.find(s => s.id === selectedId) ?? states[0];
        if (states.length > 1)
            this._addTabs(states, selected.id);
        this._addBody(selected);
        this._addFooter();
    }

    // ------------------------------------------------------------ sections

    _addTabs(states, selectedId) {
        const row = new St.BoxLayout({ style_class: 'ai-tracker-tabs' });
        for (const s of states) {
            const label = s.record?.name ?? (s.id === 'claude' ? 'Claude Code' : 'Codex');
            const btn = new St.Button({
                label,
                can_focus: true,
                toggle_mode: true,
                style_class: 'ai-tracker-tab',
            });
            btn.set_checked(s.id === selectedId);
            btn.set_accessible_name(`Show ${label} usage`);
            btn.connect('clicked', () => this._onSelect(s.id));
            row.add_child(btn);
        }
        this.actor.add_child(row);
    }

    _addBody(state) {
        const { record, status, error } = state;
        if (!record && (status === 'loading' || state.job)) {
            this.actor.add_child(el('ai-tracker-dim', 'Loading usage…'));
            return;
        }
        if (!record) {
            this._addErrorCard(
                status === 'error' ? 'Could not load usage' : 'No data yet',
                error || 'The collector has not produced a snapshot for this provider.',
                true
            );
            return;
        }
        this._addHeader(record, status);
        const statusText = record.usageStatusText?.trim();
        if (statusText) {
            this._addErrorCard(statusText, record.authHelpText?.trim() ?? '', Boolean(record.limits.length || record.totalPrompts));
        } else if (record.authHelpText && !record.limits.length && !record.totalPrompts) {
            this._addErrorCard('Setup needed', record.authHelpText, true);
        }
        if (record.limits.length)
            this._addLimits(record);
        this._addHistory(record);
        this._addModels(record);
        if (!record.limits.length && !record.totalPrompts && !statusText)
            this._addErrorCard('No usage recorded yet', 'Run the CLI once, then refresh. Local history appears automatically.', false);
        if (status === 'stale' && record)
            this.actor.add_child(el('ai-tracker-stale-note', 'Showing cached data — refresh failed.'));
        else if (status === 'error' && record)
            this.actor.add_child(el('ai-tracker-stale-note', `Refresh failed: ${error}`));
    }

    _addHeader(record, status) {
        const row = new St.BoxLayout({ style_class: 'ai-tracker-header' });
        const dot = new St.Label({ text: '●', style_class: `ai-tracker-dot ai-${status === 'ok' ? 'fresh' : status}` });
        row.add_child(dot);
        const name = el('ai-tracker-title', record.name);
        row.add_child(name);
        if (record.tierLabel) {
            const plan = new St.Label({ text: record.tierLabel, style_class: 'ai-tracker-plan' });
            plan.set_accessible_name(`Plan ${record.tierLabel}`);
            row.add_child(plan);
        }
        this.actor.add_child(row);
        const age = new St.Label({ text: formatAge(record.updatedAt), style_class: 'ai-tracker-dim' });
        this.actor.add_child(age);
    }

    _addErrorCard(title, body, showRetry) {
        const card = new St.BoxLayout({ vertical: true, style_class: 'ai-tracker-card' });
        card.add_child(el('ai-tracker-card-title', title));
        if (body)
            card.add_child(el('ai-tracker-card-body', body));
        if (showRetry) {
            const retry = new St.Button({ label: 'Retry', can_focus: true, style_class: 'ai-tracker-retry' });
            retry.connect('clicked', () => this._onRefresh());
            const row = new St.BoxLayout();
            row.add_child(retry);
            card.add_child(row);
        }
        this.actor.add_child(card);
    }

    _addLimits(record) {
        this.actor.add_child(el('ai-tracker-section', 'Limits'));
        for (const limit of record.limits) {
            const box = new St.BoxLayout({ vertical: true, style_class: 'ai-tracker-limit' });
            const row = new St.BoxLayout();
            row.add_child(el('ai-tracker-limit-label', limit.label ?? limit.title ?? 'Limit'));
            const right = new St.Label({
                text: `${formatPercent(limit.percent)} · resets ${formatCountdown(limit.resetsAt)}`,
                style_class: 'ai-tracker-dim',
                x_align: Clutter.ActorAlign.END,
            });
            row.add_child(right);
            box.add_child(row);
            box.add_child(makeMeter(limit.percent, false, `${limit.label} ${formatPercent(limit.percent)} used`));
            this.actor.add_child(box);
        }
    }

    _addHistory(record) {
        this.actor.add_child(el('ai-tracker-section', 'Tokens by day'));
        const days = record.recentDays;
        const peak = Math.max(1, ...days.map(d => d.messageCount));
        const todayStr = days[days.length - 1]?.date;
        days.forEach((day, i) => {
            const row = new St.BoxLayout({ style_class: 'ai-tracker-day' });
            const name = new St.Label({
                text: i === days.length - 1 && day.date === todayStr ? 'Today' : weekdayLabel(day.date),
                style_class: `ai-tracker-day-name${i === days.length - 1 ? ' ai-tracker-today' : ''}`,
            });
            name.set_width(64);
            row.add_child(name);
            row.add_child(makeMeter(day.messageCount / peak, true, `${day.date}: ${formatTokens(day.messageCount)} tokens`));
            const count = new St.Label({
                text: formatTokens(day.messageCount),
                style_class: `ai-tracker-day-count${i === days.length - 1 ? ' ai-tracker-today' : ''}`,
                x_align: Clutter.ActorAlign.END,
            });
            count.set_width(64);
            row.add_child(count);
            row.set_accessible_name(`${day.date}: ${formatTokens(day.messageCount)} tokens`);
            this.actor.add_child(row);
        });
        const total = new St.Label({
            text: `${formatTokens(days.reduce((s, d) => s + d.messageCount, 0))} total · ${record.totalPrompts} prompts · ${record.activeDays} active days`,
            style_class: 'ai-tracker-dim',
        });
        this.actor.add_child(total);
    }

    _addModels(record) {
        const entries = Object.entries(record.modelUsage ?? {})
            .map(([model, bucket]) => ({ model, bucket, total: bucketTotal(bucket) }))
            .filter(e => e.total > 0)
            .sort((a, b) => b.total - a.total);
        if (!entries.length)
            return;
        this.actor.add_child(el('ai-tracker-section', 'Tokens by model'));
        const peak = Math.max(1, ...entries.map(e => e.total));
        for (const { model, bucket, total } of entries) {
            const box = new St.BoxLayout({ vertical: true, style_class: 'ai-tracker-model' });
            const row = new St.BoxLayout();
            row.add_child(el('ai-tracker-model-name', model));
            const count = new St.Label({ text: formatTokens(total), style_class: 'ai-tracker-dim', x_align: Clutter.ActorAlign.END });
            row.add_child(count);
            box.add_child(row);
            box.add_child(makeMeter(total / peak, true, `${model}: ${formatTokens(total)} tokens`));
            const split = new St.Label({
                text: `in ${formatTokens(bucket.inputTokens)} · out ${formatTokens(bucket.outputTokens)} · cache ${formatTokens(bucket.cacheReadInputTokens)} read / ${formatTokens(bucket.cacheCreationInputTokens)} written`,
                style_class: 'ai-tracker-split',
            });
            box.add_child(split);
            this.actor.add_child(box);
        }
    }

    _addFooter() {
        const row = new St.BoxLayout({ style_class: 'ai-tracker-footer' });
        const refresh = new St.Button({ label: 'Refresh', can_focus: true, style_class: 'ai-tracker-action' });
        refresh.set_accessible_name('Refresh usage now');
        refresh.connect('clicked', () => this._onRefresh());
        const prefs = new St.Button({ label: 'Preferences', can_focus: true, style_class: 'ai-tracker-action' });
        prefs.connect('clicked', () => this._onPrefs());
        row.add_child(refresh);
        row.add_child(prefs);
        this.actor.add_child(row);
    }
}
