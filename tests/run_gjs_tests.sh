#!/usr/bin/env bash
# Run the gjs smoke tests: panel rendering (stubbed St/Clutter) and the real
# refresh manager against the real collector.
#
# ui/panel.js imports gi:// modules that only exist inside GNOME Shell, so
# the runner copies the tree to scratch and rewrites those two import lines
# to the stubs. lib/*.js run unmodified.
set -euo pipefail
cd "$(dirname "$0")/.."

SCRATCH=$(mktemp -d /tmp/aitrack-gjs-XXXXXX)
trap 'rm -rf "$SCRATCH"' EXIT

mkdir -p "$SCRATCH/lib" "$SCRATCH/ui" "$SCRATCH/fixtures"
cp lib/snapshot.js lib/refresh.js "$SCRATCH/lib/"
cp tests/gjs/stubs.js tests/gjs/helpers.js tests/gjs/panel_smoke.js tests/gjs/snapshot_smoke.js tests/gjs/refresh_smoke.js "$SCRATCH/"
cp -r tests/fixtures/snapshots "$SCRATCH/fixtures/"

# Rewrite only the Shell-only imports of panel.js to the stubs.
python3 - ui/panel.js "$SCRATCH/ui/panel.js" <<'EOF'
import sys
with open(sys.argv[1], encoding='utf-8') as fh:
    src = fh.read()
src = src.replace("import Clutter from 'gi://Clutter';",
                  "import { makeClutter } from '../stubs.js';\nconst Clutter = makeClutter();")
src = src.replace("import St from 'gi://St';",
                  "import { makeSt } from '../stubs.js';\nconst St = makeSt();")
with open(sys.argv[2], 'w', encoding='utf-8') as fh:
    fh.write(src)
EOF

export AI_TRACKER_COLLECTOR="$PWD/collector/collect.py"
export AI_TRACKER_CACHE="$SCRATCH/cache"

echo "--- panel smoke (gjs) ---"
gjs -m "$SCRATCH/panel_smoke.js"
echo "--- snapshot smoke (gjs) ---"
gjs -m "$SCRATCH/snapshot_smoke.js"
echo "--- refresh smoke (gjs, live collector) ---"
gjs -m "$SCRATCH/refresh_smoke.js"
