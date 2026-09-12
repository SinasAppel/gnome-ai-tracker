#!/usr/bin/env bash
# Package the extension into a distributable ZIP (GNOME 50, local first).
set -euo pipefail
cd "$(dirname "$0")/.."

glib-compile-schemas schemas/ 2>/dev/null || true

# Never ship bytecode caches.
find collector tests -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

UUID="ai-usage-tracker@sinasappel.github.io"
OUT="gnome-ai-tracker@50.zip"
rm -f "$OUT"

gnome-extensions pack \
  --extra-source=collector \
  --extra-source=lib \
  --extra-source=ui \
  --extra-source=icons \
  --extra-source=THIRD_PARTY_NOTICES \
  --extra-source=LICENSE \
  -o . \
  .

# gnome-extensions pack names the file <uuid>.shell-extension.zip; normalize.
for f in *.shell-extension.zip; do
  [ -e "$f" ] && mv -f "$f" "$OUT"
done

echo "Wrote $OUT"
unzip -l "$OUT" | head -30
