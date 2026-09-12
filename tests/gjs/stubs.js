/* Minimal St/Clutter stubs so ui/panel.js render paths execute under plain
 * `gjs -m` against fixture snapshots. The runner (tests/run_gjs_tests.sh)
 * copies the real panel.js into a scratch tree with its gi:// imports
 * rewritten to this module. Cairo calls are absorbed by a Proxy; what we
 * assert is structure: labels, tabs, meters, cards, and callbacks.
 */

class Node {
    constructor(kind, props = {}) {
        this.kind = kind;
        this.props = props;
        this.children = [];
        this.destroyed = false;
        this.checked = false;
        this.visible = true;
        // St.Button carries its caption in `label`; St.Label in `text`.
        this.text = props.text ?? props.label ?? '';
        this.width = 0;
        this.height = 0;
        this.callbacks = {};
        this.clutter_text = {};
        if (kind === 'DrawingArea') {
            this.context = absorbent();
            this.surfaceSize = [120, 8];
        }
    }

    add_child(c) {
        this.children.push(c);
    }

    destroy_all_children() {
        for (const c of this.children)
            c.destroy?.();
        this.children = [];
    }

    destroy() {
        this.destroyed = true;
        this.destroy_all_children();
    }

    connect(name, fn) {
        (this.callbacks[name] ??= []).push(fn);
        return 1;
    }

    emit(name, ...args) {
        for (const fn of this.callbacks[name] ?? [])
            fn(this, ...args);
    }

    set_checked(v) {
        this.checked = v;
    }

    set_width(w) {
        this.width = w;
    }

    /** Allocated width; the stub has no layout engine, so this is the
     * fixed width when set, otherwise a representative popup width. */
    get_width() {
        return this.width || 200;
    }

    /** Stubs are always "on stage" so allocation updates run. */
    get_stage() {
        return {};
    }

    set_height(h) {
        this.height = h;
    }

    set_accessible_name(_n) {}

    get_context() {
        return this.context;
    }

    get_surface_size() {
        return this.surfaceSize;
    }

    queue_repaint() {
        this.emit('repaint');
    }

    click() {
        this.emit('clicked');
    }
}

function absorbent() {
    const fn = () => proxy;
    const proxy = new Proxy(fn, {
        get: (_t, prop) => {
            if (prop === '$dispose' || prop === 'then')
                return () => undefined;
            return proxy;
        },
        set: () => true,
        apply: () => proxy,
    });
    return proxy;
}

function widget(kind) {
    return class extends Node {
        constructor(props = {}) {
            super(kind, props);
            Object.assign(this, props);
        }
    };
}

export function makeSt() {
    const Button = widget('Button');
    const Label = widget('Label');
    // Like the real St.Button, expose the caption as a child label so
    // tree walks observe the same structure as in the Shell.
    const PatchedButton = class extends Button {
        constructor(props = {}) {
            super(props);
            if (props.label)
                this.add_child(new Label({ text: props.label }));
        }
    };
    return {
        BoxLayout: widget('BoxLayout'),
        Bin: widget('Bin'),
        Widget: widget('Widget'),
        Label,
        Button: PatchedButton,
        DrawingArea: widget('DrawingArea'),
        ScrollView: widget('ScrollView'),
        PolicyType: { NEVER: 0, AUTOMATIC: 1 },
    };
}

export function makeClutter() {
    return {
        ActorAlign: { END: 1 },
        EVENT_STOP: 1,
        EVENT_PROPAGATE: 0,
        KEY_h: 104,
        KEY_H: 72,
        KEY_l: 108,
        KEY_L: 76,
        KEY_r: 114,
        KEY_R: 82,
        color_from_string: _s => [true, { red: 53, green: 132, blue: 233, alpha: 255 }],
    };
}

/** Depth-first label texts, in render order. */
export function labelTexts(root) {
    const out = [];
    const walk = node => {
        if (node.kind === 'Label' && node.text)
            out.push(node.text);
        for (const c of node.children)
            walk(c);
    };
    walk(root);
    return out;
}

/** All nodes of a kind, depth-first. */
export function findAll(root, kind) {
    const out = [];
    const walk = node => {
        if (node.kind === kind)
            out.push(node);
        for (const c of node.children)
            walk(c);
    };
    walk(root);
    return out;
}
