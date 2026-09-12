/* Panel smoke test: render every fixture snapshot through the real
 * ui/panel.js (gi imports rewritten to stubs by the runner) and assert the
 * structure a user would see. Run via tests/run_gjs_tests.sh, not directly.
 */

import { TrackerPanel } from './ui/panel.js';
import { findAll, labelTexts } from './stubs.js';
import { readSnapshot, stateFor } from './helpers.js';

let failures = 0;
function check(name, cond, detail = '') {
    if (cond) {
        print(`ok - ${name}`);
    } else {
        failures++;
        print(`FAIL - ${name} ${detail}`);
    }
}

function tracksIn(panel) {
    return findAll(panel.actor, 'BoxLayout')
        .filter(n => (n.props.style_class || '').includes('ai-tracker-track'));
}

function selectedName(panel) {
    const tabs = findAll(panel.actor, 'Button').filter(b => b.props.style_class === 'ai-tracker-tab');
    const checked = tabs.filter(b => b.checked);
    return { tabs: tabs.map(b => b.text), checked: checked.map(b => b.text) };
}

// --- 1. healthy claude snapshot -------------------------------------------
{
    const panel = new TrackerPanel({ onSelect: () => {}, onRefresh: () => {}, onPrefs: () => {} });
    const states = [stateFor('claude-ok'), stateFor('codex-ok')];
    panel.render(states, 'claude');
    const labels = labelTexts(panel.actor);
    check('claude header shows name+plan', labels.includes('Claude Code') && labels.includes('Pro'));
    check('claude limits listed', labels.some(t => t.includes('Session (5-hour)')) && labels.some(t => t.includes('Weekly (7-day)')));
    check('limit percent+countdown', labels.some(t => t.includes('37%') && t.includes('resets')));
    check('weekly section', labels.includes('Tokens by day') && labels.includes('Today'));
    check('model section with breakdown', labels.includes('Tokens by model') && labels.some(t => t.includes('cache')));
    // 2 limits + 7 days + 2 models; stub allocation is 200px wide, so the
    // 37%/12% limits must fill exactly 74/24px (regression: fills invisible).
    const tracks = tracksIn(panel);
    check('meters drawn', tracks.length === 2 + 7 + 2, `${tracks.length}`);
    const fills = tracks.map(t => t.children[0].width);
    check('limit fills proportional', JSON.stringify(fills.slice(0, 2)) === '[74,24]', JSON.stringify(fills.slice(0, 2)));
    check('peak day fills track', Math.max(...fills.slice(2, 9)) === 200, JSON.stringify(fills.slice(2, 9)));
    check('peak model fills track', fills[9] === 200 && fills[10] === 15, JSON.stringify(fills.slice(9, 11)));
    check('footer actions', labels.includes('Refresh') && labels.includes('Preferences'));
    const tabs = selectedName(panel);
    check('two tabs, claude checked', tabs.tabs.length === 2 && tabs.checked.length === 1 && tabs.checked[0] === 'Claude Code',
        JSON.stringify(tabs));

    // tab click calls back with the other provider id
    let picked = '';
    const panel2 = new TrackerPanel({ onSelect: id => { picked = id; }, onRefresh: () => {}, onPrefs: () => {} });
    panel2.render(states, 'claude');
    findAll(panel2.actor, 'Button').find(b => b.text === 'Codex').click();
    check('tab click selects codex', picked === 'codex', picked);
}

// --- 2. expired-auth claude: stale limits + visible help -------------------
{
    let refreshed = 0;
    const panel = new TrackerPanel({ onSelect: () => {}, onRefresh: () => { refreshed++; }, onPrefs: () => {} });
    panel.render([stateFor('claude-expired')], 'claude');
    const labels = labelTexts(panel.actor);
    check('expired status visible', labels.includes('Sign-in expired'));
    check('expired help visible', labels.some(t => t.includes('auth login')));
    check('stale weekly limit kept', labels.some(t => t.includes('Weekly (7-day)')));
    findAll(panel.actor, 'Button').find(b => b.text === 'Retry').click();
    check('retry button refreshes', refreshed === 1);
}

// --- 3. empty codex: setup state, no crash on zero data ---------------------
{
    const panel = new TrackerPanel({ onSelect: () => {}, onRefresh: () => {}, onPrefs: () => {} });
    panel.render([stateFor('codex-empty')], 'codex');
    const labels = labelTexts(panel.actor);
    check('unavailable status visible', labels.includes('Codex unavailable'));
    check('no model section when empty', !labels.includes('Tokens by model'));
    // All-zero week: grey tracks render, every fill stays at zero width.
    const emptyTracks = tracksIn(panel);
    check('empty week renders grey tracks', emptyTracks.length === 7 && emptyTracks.every(t => t.children[0].width === 0),
        `${emptyTracks.length} tracks`);
}

// --- 4. error with no record at all ------------------------------------------
{
    const panel = new TrackerPanel({ onSelect: () => {}, onRefresh: () => {}, onPrefs: () => {} });
    panel.render([{ id: 'codex', record: null, status: 'error', error: 'collector timed out after 30s' }], 'codex');
    check('error card shows message', labelTexts(panel.actor).some(t => t.includes('timed out')));
}

// --- 5. fills follow allocation (popup open, resize, scrollbar) ------------
{
    const panel = new TrackerPanel({ onSelect: () => {}, onRefresh: () => {}, onPrefs: () => {} });
    panel.render([stateFor('claude-ok')], 'claude');
    const track = tracksIn(panel)[0]; // 37% session limit
    const fill = track.children[0];
    check('fill follows initial allocation', fill.width === 74, `${fill.width}`);
    track.width = 300;
    track.emit('notify::allocation');
    check('fill follows reallocation', fill.width === 111, `${fill.width}`);
    // Off-stage measurement is skipped (no log spam, no zeroing).
    track.get_stage = () => null;
    track.width = 500;
    track.emit('notify::allocation');
    check('off-stage allocation ignored', fill.width === 111, `${fill.width}`);
}

if (failures) {
    printerr(`${failures} panel check(s) failed\n`);
} else {
    print('panel smoke: all checks passed');
}
