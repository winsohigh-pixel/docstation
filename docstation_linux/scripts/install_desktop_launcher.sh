#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
mkdir -p "$HOME/.local/share/applications" "$HOME/Desktop"
cat > "$HOME/.local/share/applications/docstation.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DocStation
Comment=DocStation native dock-station application
Exec=$APP_DIR/scripts/run_app.sh
Path=$APP_DIR
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
cp "$HOME/.local/share/applications/docstation.desktop" "$HOME/Desktop/DocStation.desktop" || true
chmod +x "$HOME/Desktop/DocStation.desktop" || true
echo "Launcher installed: $HOME/Desktop/DocStation.desktop"
