# DocStation Linux v3 — native GTK app

В этой версии добавлено нативное Ubuntu-приложение вместо браузерного рабочего интерфейса.

Запуск:

```bash
./scripts/run_app.sh
```

Установка ярлыка:

```bash
./scripts/install_desktop_launcher.sh
```

Зависимости:

```bash
sudo apt install -y python3 python3-usb python3-gi gir1.2-gtk-3.0 vlc xdg-utils
```

Web UI сохранён только как резервный сервисный интерфейс.
