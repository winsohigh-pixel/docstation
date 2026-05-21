#!/usr/bin/env bash
# DocStation — установка на Ubuntu
# Запуск: chmod +x install.sh && ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

# Защита от запуска из Корзины
if [[ "$(pwd)" == *"/.local/share/Trash/"* ]]; then
  echo "ОШИБКА: запуск из Корзины. Распакуйте архив в нормальную папку." >&2
  exit 2
fi

APP_DIR="$(pwd)"
echo "=== DocStation установка === (папка: $APP_DIR)"

# ── Системные пакеты ──────────────────────────────────────────────────────
echo
echo "[1/5] Системные пакеты..."
sudo apt-get update -q
sudo apt-get install -y \
  python3 python3-pip python3-usb \
  python3-gi gir1.2-gtk-3.0 gir1.2-pango-1.0 \
  usbutils util-linux udev \
  exfatprogs ntfs-3g udisks2 \
  smartmontools lsscsi \
  ffmpeg vlc xdg-utils \
  gnome-terminal || true

# ── pip install -e . ──────────────────────────────────────────────────────
echo
echo "[2/5] Установка Python-пакета..."
pip3 install --break-system-packages -e "$APP_DIR" --quiet
echo "  Команды доступны: docstation, docstation-app, docstation-web"
echo "  Проверка: $(which docstation-app)"

# ── Udev rule ─────────────────────────────────────────────────────────────
echo
echo "[3/5] Udev правило для Ambarella..."
sudo cp "$APP_DIR/udev/99-docstation-ambarella.rules" \
        /etc/udev/rules.d/99-docstation-ambarella.rules
sudo udevadm control --reload-rules
sudo udevadm trigger || true

# ── USB autosuspend ───────────────────────────────────────────────────────
echo "[4/5] Отключение USB autosuspend..."
echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend >/dev/null || true
echo 'options usbcore autosuspend=-1' \
  | sudo tee /etc/modprobe.d/docstation-usbcore.conf >/dev/null

# Запретить автомонтирование — регистраторы монтирует сама программа
if command -v gsettings >/dev/null 2>&1; then
  gsettings set org.gnome.desktop.media-handling automount false      || true
  gsettings set org.gnome.desktop.media-handling automount-open false || true
fi

# ── Папки и инициализация БД ──────────────────────────────────────────────
echo "[5/5] Инициализация..."
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
sudo mkdir -p /mnt/docstation /var/lib/docstation/archive
sudo chown -R "$USER:$USER" /var/lib/docstation || true
DOCSTATION_DIR="$APP_DIR" docstation --config "$APP_DIR/StationConfig.linux.json" init

# ── Ярлык на рабочем столе ────────────────────────────────────────────────
DESKTOP_FILE="$HOME/.local/share/applications/docstation.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")" "$HOME/Desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=DocStation
Comment=DocStation — система скачивания видеорегистраторов
Exec=env DOCSTATION_DIR=$APP_DIR docstation-app
Path=$APP_DIR
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
cp "$DESKTOP_FILE" "$HOME/Desktop/DocStation.desktop" 2>/dev/null || true
chmod +x "$HOME/Desktop/DocStation.desktop" 2>/dev/null || true

echo
echo "══════════════════════════════════════════════"
echo "  Установка завершена."
echo ""
echo "  Запуск:"
echo "    docstation-app                  ← нативное GTK-приложение"
echo "    docstation-web                  ← web UI (http://localhost:8765)"
echo "    docstation once                 ← один цикл импорта"
echo "    docstation dry-run              ← проверка без скачивания"
echo "    docstation calibrate-slots      ← калибровка стаканов"
echo ""
echo "  Конфиг: $APP_DIR/StationConfig.linux.json"
echo "══════════════════════════════════════════════"
