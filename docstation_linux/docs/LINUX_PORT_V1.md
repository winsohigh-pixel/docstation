# Linux Port v1

Это не пересборка WPF, а новая Linux-ветка.

Стек:

- Python 3;
- PyUSB/libusb для Ambarella vendor mode;
- udev для прав доступа;
- lsblk/udisksctl/mount для Mass Storage;
- SQLite для базы и журнала;
- встроенный HTTP touch UI без внешних Python web-фреймворков.

Почему не WPF/Avalonia сразу: сначала нужно доказать стабильность USB-слоя и импорта на 15–20 регистраторах. UI сделан простым, но рабочим. После подтверждения железа можно сделать второй слой: Avalonia или kiosk browser.

## Основная очередь

```text
Ambarella detected
read startup info
queue connection
sleep switch_to_disk_delay_ms
send command 0x23
detect new /dev/sdX
settle
mount readonly
copy files to archive
add media rows
audit actions
unmount
```

## Права

Оператор:

- просмотр;
- скачивание/копирование.

Администратор:

- просмотр;
- скачивание;
- защита/снятие защиты;
- удаление;
- журнал.

Администратор не видит пароль оператора. Сброс паролей делает отдельный скрипт.
