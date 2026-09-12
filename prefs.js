/* Preferences: providers, panel label, refresh, path overrides, deps.
 *
 * Runs as a separate GTK4/libadwaita process. No secrets are stored here —
 * path overrides only; authentication always stays with the CLIs.
 */

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';

import { ExtensionPreferences } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

function findProgram(name) {
    return GLib.find_program_in_path(name) ?? '';
}

function programVersion(exe, args) {
    if (!exe)
        return 'not found';
    try {
        const [ok, stdout] = GLib.spawn_sync(null, [exe, ...args], null,
            GLib.SpawnFlags.SEARCH_PATH, null);
        if (!ok)
            return 'unknown';
        return new TextDecoder().decode(stdout).trim().split('\n')[0].slice(0, 60) || 'unknown';
    } catch {
        return 'unknown';
    }
}

export default class AiTrackerPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({ title: 'AI Usage Tracker', icon_name: 'preferences-system-symbolic' });
        window.add(page);

        // ------------------------------------------------------- providers
        const providers = new Adw.PreferencesGroup({ title: 'Providers' });
        page.add(providers);

        for (const [id, title, subtitle] of [
            ['claude', 'Claude Code', 'OAuth limits + local transcript history'],
            ['codex', 'Codex', 'App-server limits + local session history'],
        ]) {
            const row = new Adw.SwitchRow({ title, subtitle });
            const enabled = settings.get_strv('enabled-providers').includes(id);
            row.set_active(enabled);
            row.connect('notify::active', () => {
                const current = new Set(settings.get_strv('enabled-providers'));
                if (row.get_active())
                    current.add(id);
                else
                    current.delete(id);
                const next = ['claude', 'codex'].filter(p => current.has(p));
                settings.set_strv('enabled-providers', next.length ? next : [id]);
                if (row.get_active())
                    selectedRow.set_selected(next.indexOf(id));
            });
            providers.add(row);
        }

        const selectedRow = new Adw.ComboRow({
            title: 'Shown first',
            subtitle: 'Provider displayed when the popup opens and used for the panel percentage',
            model: new Gtk.StringList({ strings: ['Claude Code', 'Codex'] }),
        });
        const syncSelected = () => {
            const ids = ['claude', 'codex'];
            selectedRow.set_selected(Math.max(0, ids.indexOf(settings.get_string('selected-provider'))));
        };
        syncSelected();
        selectedRow.connect('notify::selected', () => {
            settings.set_string('selected-provider', ['claude', 'codex'][selectedRow.get_selected()] ?? 'claude');
        });
        providers.add(selectedRow);

        // ---------------------------------------------------------- panel
        const panel = new Adw.PreferencesGroup({ title: 'Top panel' });
        page.add(panel);

        const labelRow = new Adw.ComboRow({
            title: 'Panel label',
            subtitle: 'Percentage is the selected provider\u2019s highest used limit',
            model: new Gtk.StringList({ strings: ['Icon + percentage', 'Icon only', 'Hidden'] }),
        });
        const modes = ['icon-percent', 'icon', 'hidden'];
        labelRow.set_selected(Math.max(0, modes.indexOf(settings.get_string('panel-label'))));
        labelRow.connect('notify::selected', () => {
            settings.set_string('panel-label', modes[labelRow.get_selected()] ?? 'icon-percent');
        });
        panel.add(labelRow);

        const hideRow = new Adw.SwitchRow({
            title: 'Hide when there is no data',
            subtitle: 'Hide the indicator until a provider records usage or limits',
        });
        settings.bind('auto-hide', hideRow, 'active', Gio.SettingsBindFlags.DEFAULT);
        panel.add(hideRow);

        // --------------------------------------------------------- refresh
        const refresh = new Adw.PreferencesGroup({ title: 'Refresh' });
        page.add(refresh);

        const intervalRow = new Adw.SpinRow({
            title: 'Automatic refresh',
            subtitle: 'Seconds between updates (60\u20133600)',
            adjustment: new Gtk.Adjustment({ lower: 60, upper: 3600, step_increment: 60, page_increment: 300 }),
        });
        intervalRow.set_value(settings.get_int('refresh-interval'));
        intervalRow.connect('notify::value', () => {
            settings.set_int('refresh-interval', Math.round(intervalRow.get_value()));
        });
        refresh.add(intervalRow);

        // ----------------------------------------------------------- paths
        const paths = new Adw.PreferencesGroup({
            title: 'Path overrides',
            description: 'Only needed when the defaults are wrong. GUI sessions may not inherit shell variables, so set directories here instead of relying on dotfiles. Never enter secrets.',
        });
        page.add(paths);

        for (const [key, title, subtitle] of [
            ['claude-dir', 'Claude directory', 'Default: $CLAUDE_CONFIG_DIR or ~/.claude'],
            ['codex-home', 'Codex home', 'Default: $CODEX_HOME or ~/.codex'],
            ['codex-executable', 'Codex executable', 'Default: first codex on PATH'],
        ]) {
            const row = new Adw.EntryRow({ title });
            row.set_text(settings.get_string(key));
            row.set_show_apply_button(true);
            const hint = new Gtk.Label({ label: subtitle, css_classes: ['dim-label', 'caption'], xalign: 0 });
            row.add_suffix(hint);
            row.connect('apply', () => settings.set_string(key, row.get_text().trim()));
            settings.connect(`changed::${key}`, () => {
                if (row.get_text() !== settings.get_string(key))
                    row.set_text(settings.get_string(key));
            });
            paths.add(row);
        }

        // ---------------------------------------------------- dependencies
        const deps = new Adw.PreferencesGroup({ title: 'Dependencies' });
        page.add(deps);

        const python = findProgram('python3');
        const claudeBin = findProgram('claude');
        const codexBin = findProgram('codex');
        for (const [title, detail, ok] of [
            ['Python 3', python ? `${python} · ${programVersion(python, ['--version'])}` : 'Missing — the collector cannot run without it', Boolean(python)],
            ['Claude Code CLI', claudeBin ? `${claudeBin} · ${programVersion(claudeBin, ['--version'])}` : 'Not installed — Claude history and limits stay unavailable', Boolean(claudeBin)],
            ['Codex CLI', codexBin ? `${codexBin} · ${programVersion(codexBin, ['--version'])}` : 'Not installed — Codex history and limits stay unavailable', Boolean(codexBin)],
        ]) {
            const row = new Adw.ActionRow({ title, subtitle: detail });
            const icon = new Gtk.Image({
                icon_name: ok ? 'emblem-ok-symbolic' : 'dialog-warning-symbolic',
                css_classes: ok ? ['success'] : ['warning'],
            });
            row.add_suffix(icon);
            deps.add(row);
        }
    }
}
