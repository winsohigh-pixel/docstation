#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"
echo "=== Python syntax check ==="
python3 -m compileall -q docstation
echo "OK: python files compile"
echo "=== GTK availability check ==="
python3 - <<'PY'
import gi
gi.require_version('Gdk','3.0')
gi.require_version('Gtk','3.0')
from gi.repository import Gdk, Gtk, Pango, GLib
print('OK: GTK3 namespaces load')
PY
echo "=== Config/database init check ==="
python3 -m docstation.main init
echo "OK: init finished"
