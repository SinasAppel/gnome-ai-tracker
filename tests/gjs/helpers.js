/* Shared helpers for the gjs smoke tests. */

import Gio from 'gi://Gio';

const _fixturesNote = 'runner mirrors tests/fixtures to ./fixtures';

export function readSnapshot(name) {
    // The runner mirrors tests/fixtures to <scratch>/fixtures next to this
    // module; resolve against the module URL so CWD does not matter.
    const here = Gio.File.new_for_uri(import.meta.url).get_parent().get_path();
    const path = `${here}/fixtures/snapshots/${name}.json`;
    const [ok, bytes] = Gio.File.new_for_path(path).load_contents(null);
    if (!ok)
        throw new Error(`missing fixture ${name}`);
    return JSON.parse(new TextDecoder().decode(bytes));
}

export function stateFor(fixtureName, overrides = {}) {
    return {
        id: fixtureName.split('-')[0],
        record: readSnapshot(fixtureName),
        status: 'ok',
        error: '',
        ...overrides,
    };
}

/** Fake Gio.Settings backed by a plain object (refresh manager only reads). */
export function fakeSettings(values = {}) {
    const store = {
        'enabled-providers': ['claude', 'codex'],
        'selected-provider': 'claude',
        'panel-label': 'icon-percent',
        'refresh-interval': 900,
        'auto-hide': false,
        'claude-dir': '',
        'codex-home': '',
        'codex-executable': '',
        ...values,
    };
    return {
        get_strv: k => [...store[k]],
        get_string: k => store[k],
        get_int: k => store[k],
        get_boolean: k => store[k],
        connect: () => 1,
        disconnect: () => {},
    };
}
