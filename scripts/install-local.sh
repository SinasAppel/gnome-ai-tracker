#!/usr/bin/env bash
# Install the extension into the local user's GNOME Shell and enable it.
set -euo pipefail
cd "$(dirname "$0")/.."

UUID="ai-usage-tracker@sinasappel.github.io"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

glib-compile-schemas schemas/

rm -rf "$DEST"
mkdir -p "$DEST"
# Docs/legal are optional at packaging time.
DOCS=""
for d in THIRD_PARTY_NOTICES LICENSE README.md; do
  [ -e "$d" ] && DOCS="$DOCS $d"
done
# shellcheck disable=SC2086
cp -r metadata.json extension.js prefs.js stylesheet.css schemas lib ui collector icons $DOCS "$DEST"/

# Also install the panel glyph into hicolor so themed lookup ('ai-tracker')
# works even without the runtime search-path (e.g. prefs, lock screen).
for size in 16 32 64; do
  dest="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
  mkdir -p "$dest"
  if [ -f "icons/ai-tracker-${size}.png" ]; then
    cp -f "icons/ai-tracker-${size}.png" "$dest/ai-tracker.png"
  fi
done
if [ -f "icons/ai-tracker.svg" ]; then
  mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
  cp -f "icons/ai-tracker.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/ai-tracker.svg"
fi
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed to $DEST"
echo "Enable with: gnome-extensions enable $UUID"
echo "Then restart GNOME Shell (Wayland: log out/in) or run a nested session:"
echo "  dbus-run-session -- gnome-shell --nested --wayland"
