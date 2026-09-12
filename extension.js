/* GNOME AI Tracker — top-panel indicator for local AI coding usage.
 *
 * Read-only display: the bundled Python collector builds validated JSON
 * snapshots (see lib/snapshot.js); this file owns the panel button, the
 * popup lifecycle, and nothing else. All timers, signals and child
 * processes are torn down in disable().
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import { RefreshManager } from './lib/refresh.js';
import { formatPercent, topLimitPercent } from './lib/snapshot.js';
import { TrackerPanel } from './ui/panel.js';

const TICK_SECONDS = 30;

export default class AiTrackerExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._cacheDir = GLib.build_filenamev([GLib.get_user_cache_dir(), 'gnome-ai-tracker']);

        this._button = new PanelMenu.Button(0.0, 'AI Usage Tracker', false);
        this._button.set_accessible_name('AI coding usage');

        const box = new St.BoxLayout({ style_class: 'ai-tracker-button' });
        this._icon = this._makeIcon();
        this._pctLabel = new St.Label({
            text: '',
            style_class: 'ai-tracker-pct',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._icon);
        box.add_child(this._pctLabel);
        this._button.add_child(box);

        this._scroll = new St.ScrollView({
            style_class: 'ai-tracker-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
        });
        // Bounded popup: scrolls on small screens instead of running off-screen.
        this._scroll.style = 'max-height: 560px; min-width: 340px;';
        this._panel = new TrackerPanel({
            onSelect: id => this._selectProvider(id),
            onRefresh: () => this._manager?.refreshAll({ force: true }),
            onPrefs: () => this.openPreferences(),
        });
        this._scroll.add_child(this._panel.actor);
        const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        item.add_child(this._scroll);
        this._button.menu.addMenuItem(item);

        this._manager = new RefreshManager({
            collectorPath: GLib.build_filenamev([this.path, 'collector', 'collect.py']),
            settings: this._settings,
            cacheDir: this._cacheDir,
            onUpdate: () => this._render(),
        });

        this._menuOpenId = this._button.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._manager.refreshStale();
                this._render();
                this._startTick();
            } else {
                this._stopTick();
            }
        });
        this._keyPressId = this._button.menu.actor.connect('key-press-event', (_a, event) => {
            const symbol = event.get_key_symbol();
            const ids = this._manager.providerIds();
            const current = ids.indexOf(this._selected());
            if (symbol === Clutter.KEY_h || symbol === Clutter.KEY_H) {
                if (ids.length > 1)
                    this._selectProvider(ids[(current - 1 + ids.length) % ids.length]);
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_l || symbol === Clutter.KEY_L) {
                if (ids.length > 1)
                    this._selectProvider(ids[(current + 1) % ids.length]);
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_r || symbol === Clutter.KEY_R) {
                this._manager.refreshAll({ force: true });
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this._settingsChangedId = this._settings.connect('changed', () => this._render());

        this._manager.start();
        Main.panel.addToStatusArea(this.uuid, this._button);
        this._render();
    }

    disable() {
        this._stopTick();
        if (this._keyPressId) {
            try {
                this._button.menu.actor.disconnect(this._keyPressId);
            } catch { /* already gone */ }
            this._keyPressId = 0;
        }
        if (this._menuOpenId) {
            try {
                this._button.menu.disconnect(this._menuOpenId);
            } catch { /* already gone */ }
            this._menuOpenId = 0;
        }
        if (this._settingsChangedId) {
            try {
                this._settings.disconnect(this._settingsChangedId);
            } catch { /* already gone */ }
            this._settingsChangedId = 0;
        }
        // Guard: manager callbacks check their own stopped flag, then the
        // button is destroyed so no actor can be touched afterwards.
        this._manager?.stop();
        this._manager = null;
        this._panel?.destroy();
        this._panel = null;
        // Explicitly destroy every owned actor (children would also go away
        // with the button, but the review guidelines require explicit
        // cleanup) and release all references.
        this._scroll?.destroy();
        this._scroll = null;
        this._icon?.destroy();
        this._icon = null;
        this._pctLabel?.destroy();
        this._pctLabel = null;
        this._button?.destroy();
        this._button = null;
        this._settings = null;
    }

    // ------------------------------------------------------------ rendering

    _makeIcon() {
        // Shell-process safe: Gio FileIcon only, no Gtk/Gdk (forbidden by
        // the EGO review guidelines in extension.js). PNG first: pixbuf has
        // built-in PNG support, so FileIcon PNGs render even when the
        // svg loader cache is empty; SVGs are a fallback, then a stock
        // symbolic icon, then a themed 'ai-tracker' if the user installed
        // it into hicolor via install-local.sh. icon_size is explicit:
        // custom glyphs can otherwise measure 0px wide, which leaves
        // percentage-only in the top bar.
        for (const name of ['ai-tracker-32.png', 'ai-tracker.png', 'ai-tracker-16.png', 'ai-tracker-64.png', 'ai-tracker.svg', 'ai-tracker-symbolic.svg']) {
            const path = GLib.build_filenamev([this.path, 'icons', name]);
            try {
                const file = Gio.File.new_for_path(path);
                if (file.query_exists(null)) {
                    console.log(`[ai-tracker] using panel icon ${name} at ${path}`);
                    return new St.Icon({
                        gicon: Gio.FileIcon.new(file),
                        icon_size: 16,
                        style_class: 'system-status-icon',
                        y_align: Clutter.ActorAlign.CENTER,
                    });
                } else {
                    console.log(`[ai-tracker] icon candidate missing: ${path}`);
                }
            } catch (e) { console.log(`[ai-tracker] icon candidate failed ${name}: ${e}`); }
        }
        return new St.Icon({
            icon_name: 'applications-science-symbolic',
            icon_size: 16,
            style_class: 'system-status-icon',
            y_align: Clutter.ActorAlign.CENTER,
        });
    }

    _selected() {
        try {
            const saved = this._settings.get_string('selected-provider');
            if (this._manager.providerIds().includes(saved))
                return saved;
        } catch { /* fall through */ }
        return this._manager.providerIds()[0] ?? 'claude';
    }

    _selectProvider(id) {
        try {
            this._settings.set_string('selected-provider', id);
        } catch { /* selection still applies for this session */ }
        this._render();
    }

    _states() {
        return this._manager.providerIds().map(id => ({ id, ...this._manager.getState(id) }));
    }

    _render() {
        if (!this._button || !this._manager)
            return;
        const states = this._states();
        const selected = this._selected();
        try {
            this._panel.render(states, selected);
        } catch { /* never let rendering break the indicator */ }
        this._renderButton(states, selected);
    }

    _renderButton(states, selectedId) {
        let mode = 'icon-percent';
        let autoHide = false;
        try {
            mode = this._settings.get_string('panel-label');
            autoHide = this._settings.get_boolean('auto-hide');
        } catch { /* defaults apply */ }

        const selected = states.find(s => s.id === selectedId);
        const pct = selected?.record ? topLimitPercent(selected.record) : null;
        this._pctLabel.visible = mode === 'icon-percent' && pct !== null;
        if (pct !== null)
            this._pctLabel.text = formatPercent(pct);
        this._icon.visible = mode !== 'hidden';

        const hasData = states.some(s => s.record && (s.record.limits.length || s.record.totalPrompts));
        const anyLoading = states.some(s => s.status === 'loading' && !s.record);
        this._button.visible = !autoHide || hasData || anyLoading || !states.length;

        const parts = states.map(s => {
            const p = s.record ? topLimitPercent(s.record) : null;
            const name = s.record?.name ?? s.id;
            return p === null ? `${name}: no limits` : `${name} ${formatPercent(p)}`;
        });
        this._button.set_accessible_name(`AI coding usage. ${parts.join(', ') || 'no providers'}`);
    }

    _startTick() {
        this._stopTick();
        // Countdown labels update locally while the popup is open.
        this._tickTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._render();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopTick() {
        if (this._tickTimer) {
            GLib.source_remove(this._tickTimer);
            this._tickTimer = 0;
        }
    }
}
