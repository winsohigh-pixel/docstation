#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Install packages ==="
sudo apt update
sudo apt install -y python3 python3-usb python3-gi gir1.2-gtk-3.0 gir1.2-pango-1.0 usbutils util-linux udev exfatprogs ntfs-3g smartmontools lsscsi udisks2 ffmpeg vlc xdg-utils gnome-terminal || true

echo "=== UDEV rule ==="
sudo cp udev/99-docstation-ambarella.rules /etc/udev/rules.d/99-docstation-ambarella.rules
sudo udevadm control --reload-rules
sudo udevadm trigger || true

echo "=== Disable USB autosuspend ==="
echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend >/dev/null || true
echo 'options usbcore autosuspend=-1' | sudo tee /etc/modprobe.d/docstation-usbcore.conf >/dev/null

if command -v gsettings >/dev/null 2>&1; then
  gsettings set org.gnome.desktop.media-handling automount false || true
  gsettings set org.gnome.desktop.media-handling automount-open false || true
fi

mkdir -p data logs
sudo mkdir -p /mnt/docstation /var/lib/docstation/archive
sudo chown -R "$USER:$USER" /var/lib/docstation || true
python3 -m docstation.main init

echo "=== Done ==="
echo "Run native app: ./scripts/run_app.sh"
echo "Run one import cycle: sudo ./scripts/run_once.sh"
./scripts/install_desktop_launcher.sh || true
